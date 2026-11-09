"""Harness connector venue support (connector plan, Step 5):
the home-side ferry relay and the session bootstrap driver.

The connector service lives in the scheduler repo and binds
loopback/tailnet only; a Colab session cannot reach it. The
venue client (the scheduler repo's bin/harness, shipped as-is)
therefore speaks a ferry transport: each call is a request file
{"id", "method", "path", "token", "body"} in the session's
ferry-out dir, and the answer is a {"status", "body"} file at
the same id in ferry-in. This module is the home side of that
ferry: a relay cycle reads the session's pending requests over
the colab channel (one exec), performs each against the
loopback service with the envelope's token as the Bearer
credential, and delivers the responses in a second exec, which
also deletes the served request files (the client deletes each
response once read). Only /tools paths are relayed; anything
else is refused locally by the relay and never reaches the
service.

The relay is runner-internal channel use, like the bridge duty:
its exec calls ride the funnel directly and take no sync claim.
Its cadence is a poll; bridged tools additionally wait on the
home bridge agent's own cycle, so a bridged call's latency is
cycles, not milliseconds — the client's waits are sized for it.

The bootstrap driver is the one command that prepares a
session: it mints (or reuses) the consumer's connector token
through the scheduler's connector-token verb — the token is
captured from the verb's stdout straight into an untracked
staging file at 0600 and never printed or logged — stages the
client, launches the harness-bootstrap job (which installs the
client and token in-session, shreds the upload copy, and runs
one smoke call), relays until the run is terminal, and reports
the verdict. Runner machinery runs under the caller's own
registry identity (panel); the token planted in the session
belongs to the connector's own registry (the consumer's caller
there, sweeper-venue by default) — one caller per consumer
role, never per session.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE_URL = "http://127.0.0.1:8796"
SCHEDULER_REPO = Path("/home/hatch/workspace/scheduler")
FERRY_OUT_REMOTE = "/content/harness-ferry/out"
FERRY_IN_REMOTE = "/content/harness-ferry/in"
CLIENT_DIR_REMOTE = "/content/harness-client"
RELAY_TIMEOUT_S = 120


# --- request execution (home side, loopback) ----------------------------


def execute_request(envelope: dict, service_url: str = SERVICE_URL) -> dict:
    """Perform one ferried request against the connector service
    and shape the ferry response {"status", "body"}. Only /tools
    paths are relayed; anything else is refused here and never
    reaches the service."""
    path = str(envelope.get("path") or "")
    if not path.startswith("/tools"):
        return {"status": 403, "body": {"ok": False, "error": "relay_refused"}}
    body = envelope.get("body")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(service_url.rstrip("/") + path, data=data, method=str(envelope.get("method") or "GET"))
    req.add_header("Authorization", f"Bearer {envelope.get('token') or ''}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            parsed = {"ok": False, "error": f"http {exc.code}"}
        return {"status": exc.code, "body": parsed}
    except (urllib.error.URLError, OSError):
        return {"status": 503, "body": {"ok": False, "error": "service_unreachable"}}


# --- ferry channel scripts ------------------------------------------------
#
# The channel side of a cycle is two generated exec payloads,
# the bridge ferry's one-call-per-duty shape: the first lists
# the pending request envelopes; the second writes the response
# files and deletes the served request files. The ferry dirs
# are parameters so the tests can run the same scripts against
# a local tree.


def read_requests_script(ferry_out: str = FERRY_OUT_REMOTE) -> str:
    return (
        "import json, os\n"
        f"out_dir = {ferry_out!r}\n"
        "requests = {}\n"
        "if os.path.isdir(out_dir):\n"
        "    for name in sorted(os.listdir(out_dir)):\n"
        "        if not name.endswith('.json'):\n"
        "            continue\n"
        "        try:\n"
        "            with open(os.path.join(out_dir, name)) as fh:\n"
        "                requests[name[:-5]] = json.load(fh)\n"
        "        except (OSError, ValueError):\n"
        "            pass\n"
        "print(json.dumps({'requests': requests}))\n"
    )


def deliver_script(responses: dict, ferry_out: str = FERRY_OUT_REMOTE, ferry_in: str = FERRY_IN_REMOTE) -> str:
    return (
        "import json, os\n"
        f"out_dir = {ferry_out!r}\n"
        f"in_dir = {ferry_in!r}\n"
        f"responses = {json.dumps(responses)!r}\n"
        "os.makedirs(in_dir, exist_ok=True)\n"
        "for rid, response in json.loads(responses).items():\n"
        "    dest = os.path.join(in_dir, rid + '.json')\n"
        "    tmp = dest + '.tmp'\n"
        "    with open(tmp, 'w') as fh:\n"
        "        json.dump(response, fh)\n"
        "    os.replace(tmp, dest)\n"
        "    try:\n"
        "        os.unlink(os.path.join(out_dir, rid + '.json'))\n"
        "    except OSError:\n"
        "        pass\n"
        "print(json.dumps({'delivered': sorted(json.loads(responses))}))\n"
    )


def _exec_script(colab, repo: Path, session: str, script_text: str, tag: str) -> dict:
    script = repo / "var" / f"harness-relay-{tag}.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(script_text)
    r = colab("exec", "-s", session, "--timeout", str(RELAY_TIMEOUT_S), "-f", str(script), timeout=RELAY_TIMEOUT_S + 30)
    if r.returncode != 0:
        raise RuntimeError(f"relay exec ({tag}) failed rc={r.returncode}: {(r.stderr or r.stdout or '')[-200:]}")
    return json.loads((r.stdout or "").strip().splitlines()[-1])


def relay_cycle(colab, repo: Path, session: str, service_url: str = SERVICE_URL) -> dict:
    """One ferry cycle for one session: read pending requests,
    execute them against the service, deliver the responses and
    clear the served requests. Returns the cycle's accounting."""
    listing = _exec_script(colab, repo, session, read_requests_script(), "read")
    requests = listing.get("requests") or {}
    if not requests:
        return {"requests": 0, "served": []}
    responses = {}
    for rid, envelope in requests.items():
        if isinstance(envelope, dict):
            responses[rid] = execute_request(envelope, service_url)
    if responses:
        _exec_script(colab, repo, session, deliver_script(responses), "deliver")
    return {"requests": len(requests), "served": sorted(responses)}


def relay_until(colab, repo: Path, session: str, done, service_url: str = SERVICE_URL, interval_s: float = 5.0, max_s: float = 1800.0) -> dict:
    """Cycle the relay until done() reports true (checked between
    cycles) or the bound passes. Returns the totals."""
    totals = {"cycles": 0, "served": 0}
    deadline = time.time() + max_s
    while True:
        cycle = relay_cycle(colab, repo, session, service_url)
        totals["cycles"] += 1
        totals["served"] += len(cycle["served"])
        if done():
            return totals
        if time.time() > deadline:
            totals["timed_out"] = True
            return totals
        time.sleep(interval_s)


# --- bootstrap driver ------------------------------------------------------


def stage_dir(repo: Path) -> Path:
    return repo / "var" / "harness"


def mint_token(repo: Path, consumer: str, scopes: str, scheduler_repo: Path = SCHEDULER_REPO) -> Path:
    """Run the scheduler's connector-token verb with its stdout
    captured, extract the one-showing token line, and stage it
    at 0600 under var/harness/. The verb's stdout is never
    printed or logged by this driver — the capture is the one
    showing, and the staged file is the delivery artifact."""
    proc = subprocess.run(
        [str(scheduler_repo / "bin" / "sched"), "connector-token", consumer, "--scopes", scopes],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"connector-token mint failed rc={proc.returncode}: {(proc.stderr or '')[-200:]}")
    lines = [ln.strip() for ln in proc.stdout.splitlines()]
    token = None
    for i, ln in enumerate(lines):
        if "shown once" in ln and i + 1 < len(lines):
            token = lines[i + 1]
            break
    if not token:
        raise RuntimeError("connector-token output carried no token line")
    dest_dir = stage_dir(repo)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{consumer}.token"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(dest, 0o600)
    return dest


def stage_client(repo: Path, scheduler_repo: Path = SCHEDULER_REPO) -> Path:
    """Copy the scheduler repo's bin/harness client into the
    staging dir, byte-for-byte (the bootstrap ships it as-is)."""
    src = scheduler_repo / "bin" / "harness"
    dest_dir = stage_dir(repo)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "harness"
    dest.write_bytes(src.read_bytes())
    dest.chmod(0o755)
    return dest


def bootstrap(repo: Path, consumer: str = "sweeper-venue", scopes: str = "read,write-state,send",
              scheduler_repo: Path = SCHEDULER_REPO, remint: bool = False,
              run_mod=None, colab=None) -> dict:
    """The full bootstrap: token staged, client staged, the
    harness-bootstrap job launched detached, the relay cycled
    until the run is terminal, and the smoke verdict read back
    from the run's pulled outputs. Returns the verdict dict;
    raises nothing — failures land in {"ok": False, "stage"}."""
    if run_mod is None or colab is None:
        sys.path.insert(0, str(repo / "bin"))
        import _load
        run_mod = run_mod or _load.colab_run()
        colab = colab or _load.sync("cli").colab
    try:
        token_path = stage_dir(repo) / f"{consumer}.token"
        if remint or not token_path.exists():
            mint_token(repo, consumer, scopes, scheduler_repo)
        stage_client(repo, scheduler_repo)
    except Exception as exc:  # noqa: BLE001 - staged as a verdict, not a traceback
        return {"ok": False, "stage": "staging", "error": f"{type(exc).__name__}: {exc}"}
    launch = subprocess.run(
        [str(repo / "bin" / "colab-run"), "harness-bootstrap", "--detach", "--caller", "panel"],
        capture_output=True, text=True, timeout=600,
    )
    if launch.returncode != 0:
        return {"ok": False, "stage": "launch", "error": (launch.stderr or launch.stdout or "")[-300:]}
    try:
        started = json.loads(launch.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "stage": "launch", "error": f"unparsable launch output: {launch.stdout[-200:]}"}
    run_id, session = started["run_id"], started["session"]

    def _terminal() -> bool:
        rec = run_mod.load_run_record(run_id)
        return bool(rec and rec.get("state") in run_mod.TERMINAL_STATES)

    totals = relay_until(colab, repo, session, _terminal)
    wait = subprocess.run(
        [str(repo / "bin" / "colab-run"), "--wait", run_id],
        capture_output=True, text=True, timeout=300,
    )
    rec = run_mod.load_run_record(run_id) or {}
    smoke = None
    smoke_path = repo / "results" / run_id / "smoke-result.json"
    if smoke_path.exists():
        try:
            smoke = json.loads(smoke_path.read_text())
        except ValueError:
            smoke = None
    return {
        "ok": wait.returncode == 0 and bool(smoke and smoke.get("ok")),
        "run_id": run_id,
        "session": session,
        "state": rec.get("state"),
        "relay": totals,
        "smoke": smoke,
    }
