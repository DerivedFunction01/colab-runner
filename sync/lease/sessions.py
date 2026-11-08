import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from . import foundation, callers, records

# --- Generated session names ----------------------------------------------------
# Local why: names are never reused, so a claims-journal attribution
# can never silently come to name a different lease.


def _taken_session_names(repo: Path, colab=None) -> set:
    taken: set = set()
    for rec in records.all_records(repo):
        taken.add(rec["session"])
    taken |= set(callers.load_caller_claims(repo).keys())
    try:
        lines = foundation.spend_journal_path(repo).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            seg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(seg, dict) and seg.get("session"):
            taken.add(seg["session"])
    if colab is not None:
        live = live_sessions(colab, repo)
        if live:
            taken |= {e["session"] for e in live}
    return taken


def generate_session_name(repo: Path, colab=None) -> str:
    """A fresh generated session name, checked against every name
    ever taken (see the section note)."""
    return records.naming.generate_name(_taken_session_names(repo, colab))


_SESSION_READ_ATTEMPTS = 3
_SESSION_READ_BACKOFF_S = (2, 5)

# The reap pass runs on the hook's fixed budget (420 s) and repeats
# every couple of minutes, so its classifications take a smaller
# read budget than the interactive paths: a stall classifies
# unknown sooner — unknown is never read as dead — and the next
# pass retries. Two attempts at 45 s keeps one record's worst case
# near 90 s instead of the standard ~370 s.
_REAP_READ_ATTEMPTS = 2
_REAP_READ_TIMEOUT_S = 45


def session_state(colab, session: str, diag: dict | None = None, attempts: int | None = None, timeout: int = 120) -> str:
    """Tri-state: "alive" (listed), "absent" (list obtained, name not in
    it), "unknown" (the CLI call failed or returned nothing). Unknown must
    never be treated as dead: on 2026-10-29 the lease record vanished
    while its session lived, and a hiccup-reads-as-death path is the
    standing suspect class. Callers reconcile only on "absent".

    The read is retried (bounded, with backoff): a single transient
    failure — a subprocess timeout under the account's parallel load
    is the observed class — otherwise poisons a whole guard or
    reaper cycle with an unknown it did not earn (2026-11-07: a
    healthy, pulsing session classified extend-unknown and twice
    kept-unknown overnight). Only exhausted retries classify
    unknown, and `diag`, when passed, records why: the attempt
    count and the last failure's shape, so an unknown is never
    silent again. The auth prompt is definitive, never retried."""
    last_error = "no-attempt"
    tries = attempts if attempts is not None else _SESSION_READ_ATTEMPTS
    for attempt in range(tries):
        if attempt:
            time.sleep(_SESSION_READ_BACKOFF_S[min(attempt - 1, len(_SESSION_READ_BACKOFF_S) - 1)])
        try:
            out = colab("sessions", timeout=timeout).stdout
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:120] if str(exc) else type(exc).__name__
            continue
        if out and "Enter the authorization code" in out:
            # The CLI's interactive login prompt is not a session list: on
            # 2026-11-01 it read as "absent" here and a live lease's record
            # was deleted under it. The bin/colab-run guard now stops the
            # prompt from ever reaching callers; this is the same rule at
            # the seam, for any path that bypasses the wrapper.
            if diag is not None:
                diag.update({"attempts": attempt + 1, "read_error": "auth-prompt"})
            return "unknown"
        if not out or not out.strip():
            last_error = "empty-output"
            continue
        return "alive" if session in out else "absent"
    if diag is not None:
        diag.update({"attempts": tries, "read_error": last_error})
    return "unknown"


def session_alive(colab, session: str) -> bool:
    return session_state(colab, session) == "alive"


def local_sessions_path() -> Path:
    """The local CLI's session-bindings file. Its contents are never
    printed or logged: the release fallback reads one endpoint from it,
    and a driver-1 handover merges a child binding into it."""
    return Path.home() / ".config" / "colab-cli" / "sessions.json"


def session_host(session: str) -> str | None:
    """The tunnel host a session is reached at, read from the local
    binding — no network call, so no approval is minted by reading it.
    The binding's `url` carries the host; a binding whose endpoint is
    itself a URL (older shapes, test fakes) parses the same way. None
    when the binding or a parseable host is absent: absence is not an
    error and never blocks the lifecycle."""
    binding = foundation.load_json(local_sessions_path(), {})
    if not isinstance(binding, dict):
        return None
    entry = binding.get(session)
    if not isinstance(entry, dict):
        return None
    for key in ("url", "endpoint"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw:
            host = urlparse(raw).hostname
            if host:
                return host
    return None


def created_shape_mismatch(session: str, shape: dict) -> dict | None:
    """Post-start shape assertion (session shape gate): the shape
    the server actually assigned, read from the local binding's
    endpoint host (an m-hm- host is high-RAM, an m-s- host is
    standard), against the shape's declared RAM class — the RAM
    dimension only, the one the incident proved can silently differ
    from the name the session was created under. A dict naming the
    mismatch, or None — including when the evidence is unreadable
    or unparseable: the assertion never acts on evidence it cannot
    read."""
    host = session_host(session)
    if not host:
        return None
    actual = _endpoint_shape(host)
    if actual is None:
        return None
    actual_high = actual == "high_ram"
    if actual_high == bool(shape.get("high_mem")):
        return None
    return {"action": "shape-mismatch", "session": session, "declared_high_mem": bool(shape.get("high_mem")), "actual_class": actual, "host": host}


# The fallback's unassign replay, run under the CLI's own interpreter.
# Local why: the stock unassign POST is bodiless and the tunnel
# endpoint answers 411; replaying the client's two steps with an
# explicit empty-JSON body (data=b"{}") is the form that succeeds.
UNASSIGN_FALLBACK_SCRIPT = """
import os
from urllib.parse import urljoin

from pydantic import BaseModel

from colab_cli.client import COLAB_XSRF_TOKEN_HEADER, TUN_ENDPOINT, GetUnassignRequest
from colab_cli.common import state

endpoint = os.environ["UNASSIGN_ENDPOINT"]
client = state.client
url = urljoin(client.colab_domain, f"{TUN_ENDPOINT}/unassign/{endpoint}")
resp = client._issue_request(url, schema=GetUnassignRequest)
headers = {COLAB_XSRF_TOKEN_HEADER["key"]: resp.token}
client._issue_request(url, method="POST", headers=headers, schema=BaseModel, data=b"{}")
"""


def stop_session_safe(colab, session: str) -> str:
    """Stop a session; never raise. `colab stop` hangs on a wedged kernel
    (measured) and its own unassign POST is bodiless (411, above), so
    fall back to a direct server-side unassign through the CLI's own
    library using the endpoint in the local binding. The fallback's
    verdict is verified against the server-side sessions read, never
    assumed from the call: "unassigned-fallback" is returned only once
    the session no longer reads alive."""
    try:
        r = colab("stop", "-s", session, timeout=120)
        if r.returncode == 0 and not session_alive(colab, session):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        binding = foundation.load_json(local_sessions_path(), {})
        endpoint = binding[session]["endpoint"]
        venv_py = Path.home() / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "python"
        env = dict(os.environ)
        env["UNASSIGN_ENDPOINT"] = endpoint
        for key in ("NO_PROXY", "no_proxy"):
            suffix = "google.com,googleapis.com,googleusercontent.com,colab.dev"
            env[key] = f"{env[key]},{suffix}" if env.get(key) else suffix
        r = subprocess.run(
            [str(venv_py), "-c", UNASSIGN_FALLBACK_SCRIPT],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if r.returncode == 0:
            # A landed unassign can take a few seconds to disappear
            # from the assignments read; confirm over a short window
            # before reporting either way.
            for attempt in range(3):
                if not session_alive(colab, session):
                    return "unassigned-fallback"
                if attempt < 2:
                    time.sleep(2)
    except Exception:  # noqa: BLE001
        pass
    return "stop-failed"


# --- Session access list & unattributed sessions -------------------------------
#
# Local why: unattributed is a detection state, never a claim
# value. Mechanics: docs/lease-workflow.md.


def session_endpoint(session: str) -> str | None:
    """The raw binding endpoint/url for a session (the value
    session_host parses the host out of), or None when the binding
    or the entry is absent."""
    binding = foundation.load_json(local_sessions_path(), {})
    if not isinstance(binding, dict):
        return None
    entry = binding.get(session)
    if not isinstance(entry, dict):
        return None
    for key in ("url", "endpoint"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


def _endpoint_shape(endpoint: str | None) -> str | None:
    """The shape class an endpoint's host names (m-hm- high-RAM,
    m-s- standard — the same evidence the post-start assertion
    reads), or None when the endpoint names neither."""
    if not endpoint:
        return None
    host = urlparse(endpoint).hostname or endpoint
    m = re.match(r"m-(hm|s)-", host)
    if not m:
        return None
    return "high_ram" if m.group(1) == "hm" else "standard"


def live_sessions(colab, repo: Path) -> list[dict] | None:
    """The server's live sessions: [{session, endpoint,
    shape_class}], endpoint/shape filled from the listing line when
    it carries them, else from the local binding. None when the
    listing is unreadable — a failed call, an empty answer, an auth
    prompt: the session_state rule, that no caller may act on a
    list it could not read."""
    try:
        out = colab("sessions", timeout=120).stdout
    except Exception:  # noqa: BLE001
        return None
    if not out or not out.strip() or "Enter the authorization code" in out:
        return None
    found: dict[str, dict] = {}
    text = out.strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        for name in parsed:
            if isinstance(name, str) and name:
                found[name] = {"session": name, "endpoint": None, "shape_class": None}
    if not found:
        for line in text.splitlines():
            m = re.match(r"\[([^\]]+)\]\s*(.*)$", line.strip())
            if not m or not m.group(1).strip():
                continue
            if m.group(1).strip() == "colab":
                # The CLI prefixes its own chatter with [colab]
                # (e.g. "[colab] No active sessions.") — that is
                # not a session name, and reading it as one invents
                # a phantom session on an empty account.
                continue
            entry = {"session": m.group(1).strip(), "endpoint": None, "shape_class": None}
            rest = m.group(2)
            for tok in rest.split():
                if tok.startswith("http") or tok.startswith("m-"):
                    entry["endpoint"] = tok
                    break
            if "High-RAM" in rest:
                entry["shape_class"] = "high_ram"
            elif "Standard" in rest:
                entry["shape_class"] = "standard"
            found[entry["session"]] = entry
    if not found:
        # Substring fallback over the names this workspace knows
        # (records, binding, claims) — the way session_state reads
        # a listing whose format carries no structure at all.
        known = {rec.get("session") for rec in records.all_records(repo)}
        binding = foundation.load_json(local_sessions_path(), {})
        if isinstance(binding, dict):
            known |= set(binding.keys())
        known |= set(callers.load_caller_claims(repo).keys())
        for name in sorted(n for n in known if n):
            if name in text:
                found[name] = {"session": name, "endpoint": None, "shape_class": None}
    for entry in found.values():
        if not entry["endpoint"]:
            entry["endpoint"] = session_endpoint(entry["session"])
        if not entry["shape_class"]:
            entry["shape_class"] = _endpoint_shape(entry["endpoint"])
    return list(found.values())
