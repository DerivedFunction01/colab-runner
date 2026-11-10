#!/usr/bin/env python3
"""Lease control UI server — the operator's manual control surface for the
colab-runner lease.

Standard library only (repo rule). Binds 127.0.0.1 (default port 8793,
see ui/config.json). The server contains no lease logic: every read and
mutation shells out to bin/colab-lease, whose JSON is passed through to
the page — all derivation stays core-side; the page's only client-side
derivation is countdown arithmetic on served timestamps.

Mutations append events to var/ui-outbox.jsonl (single writer: this
server). The notify hook (colab-lease-notify) tails that file and wakes
the main chat; GET /api/outbox reads never acknowledge anything.

Env overrides (used by the tests):
  COLAB_LEASE_BIN   lease CLI path (default <repo>/bin/colab-lease)
  COLAB_LEASE_UI_SH UI control script path (default <repo>/ui/ui.sh)
  COLAB_RUNNER_VAR  var dir (default <repo>/var)
  COLAB_LEASE_UI_HOST / COLAB_LEASE_UI_PORT
  COLAB_LEASE_UI_STACKDUMP  "0" disables the stack-dump watchdog
"""

import json
import math
import mimetypes
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from stackdump import StackDumpWatchdog

UI_DIR = Path(__file__).resolve().parent
REPO = UI_DIR.parent
STATIC_DIR = UI_DIR / "static"
CONFIG_PATH = UI_DIR / "config.json"

FAILURE_ACTIONS = {"refused-budget", "create-failed", "lease-disabled", "error", "extend-no-lease", "extend-unknown", "extend-standing-managed", "grant-inactive", "caps-invalid", "unknown-shape", "caller-start-refused"}
# The panel's budget-cap editor fields, in body-name → core-flag order.
BUDGET_CAP_FIELDS = {"daily_cu_budget": "--daily", "monthly_cu_budget": "--monthly", "standing_daily_cu_cap": "--standing-daily"}
CORE_TIMEOUT_S = 1800

STATE_PARTS = ("records", "specs", "budget", "grant", "meta", "orphans")


def lease_bin() -> str:
    return os.environ.get("COLAB_LEASE_BIN", str(REPO / "bin" / "colab-lease"))


def ui_sh() -> str:
    return os.environ.get("COLAB_LEASE_UI_SH", str(UI_DIR / "ui.sh"))


def var_dir() -> Path:
    return Path(os.environ.get("COLAB_RUNNER_VAR", REPO / "var"))


def outbox_path() -> Path:
    return var_dir() / "ui-outbox.jsonl"


def parse_core_json(stdout: str):
    """bin/colab-lease prints one JSON result; tolerate any log lines
    around it by falling back to the last decodable JSON object."""
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for idx in range(len(text) - 1, -1, -1):
        if text[idx] == "{":
            try:
                obj, _ = decoder.raw_decode(text[idx:])
                return obj
            except json.JSONDecodeError:
                continue
    return None


def run_core(*args: str) -> tuple[int, dict | None, str]:
    """Run the lease CLI; return (returncode, parsed_json, stderr_tail)."""
    try:
        proc = subprocess.run(
            [lease_bin(), *args],
            capture_output=True,
            text=True,
            timeout=CORE_TIMEOUT_S,
            # The panel's actor identity for the core's session
            # shape gate: claimed in the child env on every call
            # (unconditionally — a stale inherited value must never
            # speak for the panel), paired with the --via panel tag
            # on the action calls. The core's bypass requires both.
            env={**os.environ, "COLAB_LEASE_ACTOR": "panel"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, None, f"{type(exc).__name__}: {exc}"
    return proc.returncode, parse_core_json(proc.stdout), (proc.stderr or "").strip()[-500:]


def is_success(rc: int, result: dict | None) -> bool:
    if rc != 0 or not isinstance(result, dict):
        return False
    action = result.get("action")
    if not action or action in FAILURE_ACTIONS or str(action).endswith("-failed"):
        return False
    return True


def fmt_hours(hours) -> str:
    value = float(hours)
    return str(int(value)) if value == int(value) else str(value)


class Outbox:
    """Append-only event log; this server is the only writer."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0
        for event in self._read_all():
            self.seq = max(self.seq, int(event.get("seq", 0)))

    def _read_all(self) -> list[dict]:
        events = []
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return events
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def append(self, kind: str, detail: dict) -> dict:
        with self.lock:
            self.seq += 1
            event = {
                "seq": self.seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "detail": detail,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(event) + "\n")
            return event

    def after(self, seq: int) -> tuple[list[dict], int]:
        events = [e for e in self._read_all() if int(e.get("seq", 0)) > seq]
        last = max([seq] + [int(e.get("seq", 0)) for e in events])
        return events, last


OUTBOX: Outbox | None = None

STACKDUMP: StackDumpWatchdog | None = None


def stackdump_health() -> bool:
    """The watchdog's internal check (stackdump-contract: v1): the
    state computation this server's own reads serve — run_core's
    state path, called directly, never an HTTP self-call. Answers
    (a parsed state document) is healthy; anything else is a
    non-completion the lag clock accrues against."""
    _rc, result, _err = run_core("state")
    return isinstance(result, dict)


def load_stackdump_config() -> dict:
    """Watchdog knobs from ui/config.json, contract defaults as
    the shipped values (stackdump-contract: v1, clause 2)."""
    knobs = {"cadence_s": 5.0, "threshold_s": 15.0, "repeat_s": 60.0, "max_bytes": 16 * 1024 * 1024}
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return knobs
    for key, field, cast in (
        ("stackdump_cadence_s", "cadence_s", float),
        ("stackdump_threshold_s", "threshold_s", float),
        ("stackdump_repeat_s", "repeat_s", float),
        ("stackdump_max_bytes", "max_bytes", int),
    ):
        try:
            if cfg.get(key) is not None:
                knobs[field] = cast(cfg[key])
        except (TypeError, ValueError):
            pass
    return knobs


def stackdump_last_at():
    return STACKDUMP.last_dump_at if STACKDUMP is not None else None


def reaper_events_path() -> Path:
    return var_dir() / "reaper-events.jsonl"


def reaper_drain_path() -> Path:
    return var_dir() / "reaper-events.drained"


DRAIN_LOCK = threading.Lock()


def drain_reaper_events() -> None:
    """Fold core-spooled events (var/reaper-events.jsonl — written by
    reap(), e.g. lease-renewed, which happens outside this server)
    into the outbox, keeping this server the outbox's only writer.
    The marker is a byte offset and only complete lines are consumed,
    so a writer mid-append is never half-read. Runs before state and
    outbox reads so a renewal shows in the first poll that could
    carry it."""
    if OUTBOX is None:
        return
    with DRAIN_LOCK:
        try:
            data = reaper_events_path().read_bytes()
        except OSError:
            return
        try:
            offset = int(reaper_drain_path().read_text().strip())
        except (OSError, ValueError):
            offset = 0
        if offset > len(data):
            offset = 0  # spool replaced/rotated; start over
        chunk = data[offset:]
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return
        complete = chunk[: last_nl + 1]
        for raw in complete.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or not event.get("kind"):
                continue
            detail = {k: v for k, v in event.items() if k != "kind"}
            OUTBOX.append(event["kind"], detail)
        try:
            reaper_drain_path().write_text(str(offset + len(complete)))
        except OSError:
            pass


def load_ui_config() -> tuple[str, int]:
    host, port = "127.0.0.1", 8793
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        host = cfg.get("host", host)
        port = int(cfg.get("port", port))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    host = os.environ.get("COLAB_LEASE_UI_HOST", host)
    if os.environ.get("COLAB_LEASE_UI_PORT"):
        port = int(os.environ["COLAB_LEASE_UI_PORT"])
    return host, port


def repo_config() -> dict:
    """Budget caps from the repo config, served alongside the core
    state so the page never hardcodes them. (The shape catalog itself
    comes from the core's state, not from a config file.)"""
    caps: dict = {}
    try:
        lease_cfg = json.loads((REPO / "config" / "lease.json").read_text())
        caps = {k: lease_cfg.get(k) for k in ("daily_cu_budget", "monthly_cu_budget", "max_hours", "rate_cu_per_hour")}
    except (OSError, json.JSONDecodeError):
        pass
    return caps


class Handler(BaseHTTPRequestHandler):
    server_version = "LeaseUI/1.0"

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt, *args):  # keep stdout clean; log to stderr
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- reads ---------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            return self.api_state()
        if parsed.path.startswith("/api/state/"):
            part = parsed.path[len("/api/state/"):]
            if part in STATE_PARTS:
                return self.api_state_part(part)
            return self.send_json(404, {"error": f"unknown state part {part!r}"})
        if parsed.path == "/api/outbox":
            return self.api_outbox(parsed)
        return self.serve_static(parsed.path)

    def api_state(self) -> None:
        drain_reaper_events()
        rc, result, err = run_core("state")
        if not isinstance(result, dict):
            return self.send_json(502, {"error": "lease core state unavailable", "detail": err, "rc": rc})
        payload = dict(result)
        payload["caps"] = repo_config()
        payload["stackdump_last_at"] = stackdump_last_at()
        return self.send_json(200, payload)

    def api_state_part(self, part: str) -> None:
        """One portion of the state read. Portions fail alone: a
        502 here names only its own portion, and no other portion's
        endpoint is affected."""
        drain_reaper_events()
        rc, result, err = run_core("state", "--part", part)
        if not isinstance(result, dict):
            return self.send_json(502, {"error": f"lease core state {part} unavailable", "detail": err, "rc": rc})
        payload = dict(result)
        if part in ("specs", "budget"):
            # The repo-file caps ride the portions whose sections
            # render them — an instant local read.
            payload["caps"] = repo_config()
        payload["stackdump_last_at"] = stackdump_last_at()
        return self.send_json(200, payload)

    def api_outbox(self, parsed) -> None:
        drain_reaper_events()
        try:
            after = int(parse_qs(parsed.query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        events, last = OUTBOX.after(after)
        return self.send_json(200, {"events": events, "last_seq": last})

    def serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self.send_json(404, {"error": "not found"})
        if not target.is_file():
            return self.send_json(404, {"error": "not found"})
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- mutations -----------------------------------------------------
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self.read_json_body()
        if parsed.path == "/api/lease/start":
            return self.api_lease_start(body)
        if parsed.path == "/api/lease/stop":
            return self.api_lease_stop(body)
        if parsed.path == "/api/lease/extend":
            return self.api_lease_extend(body)
        if parsed.path == "/api/lease/adopt":
            return self.api_lease_adopt(body)
        if parsed.path == "/api/lease/shorten":
            return self.api_lease_shorten(body)
        if parsed.path == "/api/access/release":
            return self.api_access_release(body)
        if parsed.path == "/api/grant":
            return self.api_grant(body)
        if parsed.path == "/api/grant/extend":
            return self.api_grant_extend(body)
        if parsed.path == "/api/ungrant":
            return self.api_ungrant()
        if parsed.path == "/api/config/budget":
            return self.api_config_budget(body)
        if parsed.path == "/api/server/restart":
            return self.api_server_restart()
        return self.send_json(404, {"error": "not found"})

    def api_server_restart(self) -> None:
        """Ask ui.sh to restart this server (the panel's degraded-state
        recovery). The restart is spawned detached — it stops this very
        process — and the answer goes out immediately; ui.sh's restart
        pauses briefly so this response lands before the stop does."""
        try:
            subprocess.Popen(
                [ui_sh(), "restart"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return self.send_json(502, {"error": "restart spawn failed", "detail": str(exc)})
        return self.send_json(200, {"action": "restart-requested"})

    def api_lease_start(self, body: dict) -> None:
        shape = body.get("shape")
        if not shape or not isinstance(shape, str):
            return self.send_json(400, {"error": "shape is required"})
        args = ["acquire", "--shape", shape, "--create"]
        hours = body.get("hours")
        if hours is not None:
            try:
                if float(hours) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return self.send_json(400, {"error": "hours must be a positive number"})
            args += ["--hours", fmt_hours(hours)]
        # Panel-started leases carry the panel's requester tag, so a
        # lease record shows who started it (envelope attribution),
        # and its caller claim — the machinery caller "panel", which
        # is also the creation authority the gate's bypass rides.
        # Start is a creation, never a ride (--create): clicking
        # start mints a parallel lease; using the current lease at
        # the shape is the extend endpoint's job.
        args += ["--via", "panel", "--caller", "panel"]
        rc, result, err = run_core(*args)
        if result is None:
            OUTBOX.append("lease-start-failed", {"shape": shape, "hours": hours, "error": err, "rc": rc})
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        kind = "lease-started" if is_success(rc, result) else "lease-start-failed"
        OUTBOX.append(kind, {"shape": shape, "hours": hours, "result": result})
        return self.send_json(200, result)

    def api_access_release(self, body: dict) -> None:
        """One-tap release of an unattributed session from the access
        list: the core's release-session verb (a record-backed
        session is refused there — it releases per session). The
        outcome is appended to the outbox under a server-side kind
        the notify hook counts like any lease event; the UI log's
        kind vocabulary is untouched."""
        session = body.get("session")
        if not session or not isinstance(session, str):
            return self.send_json(400, {"error": "session is required"})
        rc, result, err = run_core("release-session", session)
        if result is None:
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if result.get("action") == "session-released":
            OUTBOX.append("session-released", {"session": session, "result": result})
            return self.send_json(200, result)
        return self.send_json(409, result)

    def api_lease_stop(self, body: dict) -> None:
        session = body.get("session")
        args = ["release"] + ([session] if session else [])
        rc, result, err = run_core(*args)
        if result is None:
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        # Only a real release is announced: the core's release guard
        # can abort a stop whose deadline an extension just moved
        # (release-aborted), and that is not a stop.
        if is_success(rc, result) and result.get("action") == "released":
            OUTBOX.append("lease-stopped", {"session": session, "result": result})
        return self.send_json(200, result)

    def api_lease_extend(self, body: dict) -> None:
        session = body.get("session")
        if not session or not isinstance(session, str):
            return self.send_json(400, {"error": "session is required"})
        hours = body.get("hours")
        if hours is None:
            return self.send_json(400, {"error": "hours is required"})
        try:
            if float(hours) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "hours must be a positive number"})
        rc, result, err = run_core("extend", session, "--hours", fmt_hours(hours))
        if result is None:
            OUTBOX.append("lease-extend-failed", {"session": session, "hours": hours, "error": err, "rc": rc})
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if is_success(rc, result):
            OUTBOX.append("lease-extended", {"session": session, "hours": hours, "old_deadline": result.get("old_deadline"), "new_deadline": result.get("new_deadline")})
        else:
            OUTBOX.append("lease-extend-failed", {"session": session, "hours": hours, "result": result})
        return self.send_json(200, result)

    def api_lease_adopt(self, body: dict) -> None:
        """Adopt one orphaned session from the census: the core
        adopt verb with the panel caller. The body names the
        session and optionally plants a deadline (an ISO
        timestamp, or a positive hours count — with neither, the
        derived evidence plants itself) and a shape for a
        class-ambiguous orphan. A core refusal is a 409 with the
        verdict (the access-release mapping); an unreachable
        core is a 502. The outcome is appended to the outbox
        under server-side kinds, as the other one-tap actions
        do; the UI log vocabulary is untouched."""
        session = body.get("session")
        if not session or not isinstance(session, str):
            return self.send_json(400, {"error": "session is required"})
        deadline = body.get("deadline")
        hours = body.get("hours")
        if deadline is not None and hours is not None:
            return self.send_json(400, {"error": "deadline and hours are alternatives — give one"})
        args = ["adopt", session]
        if deadline is not None:
            if not isinstance(deadline, str):
                return self.send_json(400, {"error": "deadline must be an ISO timestamp"})
            try:
                datetime.fromisoformat(deadline)
            except ValueError:
                return self.send_json(400, {"error": "deadline must be an ISO timestamp"})
            args += ["--deadline", deadline]
        elif hours is not None:
            try:
                if float(hours) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return self.send_json(400, {"error": "hours must be a positive number"})
            args += ["--hours", fmt_hours(hours)]
        shape = body.get("shape")
        if shape is not None:
            if not isinstance(shape, str) or not shape:
                return self.send_json(400, {"error": "shape must be a shape id"})
            args += ["--shape", shape]
        args += ["--caller", "panel"]
        rc, result, err = run_core(*args)
        if result is None:
            OUTBOX.append("lease-adopt-failed", {"session": session, "error": err, "rc": rc})
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if result.get("action") == "adopted":
            OUTBOX.append("lease-adopted", {"session": session, "result": result})
            return self.send_json(200, result)
        OUTBOX.append("lease-adopt-failed", {"session": session, "result": result})
        return self.send_json(409, result)

    def api_lease_shorten(self, body: dict) -> None:
        """Shorten one lease from the panel: the core shorten
        verb with the panel caller. The body names the session
        and exactly one target — an ISO deadline or a positive
        hours count to pull in by. The outcome mapping mirrors
        the adopt endpoint beside it: 200 shortened, 409 core
        refusal, 502 core unreachable, with exactly one outbox
        event per call (lease-shortened / lease-shorten-failed)."""
        session = body.get("session")
        if not session or not isinstance(session, str):
            return self.send_json(400, {"error": "session is required"})
        deadline = body.get("deadline")
        hours = body.get("hours")
        if (deadline is None) == (hours is None):
            return self.send_json(400, {"error": "deadline or hours is required — give one"})
        args = ["shorten", session]
        if deadline is not None:
            if not isinstance(deadline, str):
                return self.send_json(400, {"error": "deadline must be an ISO timestamp"})
            try:
                datetime.fromisoformat(deadline)
            except ValueError:
                return self.send_json(400, {"error": "deadline must be an ISO timestamp"})
            args += ["--deadline", deadline]
        else:
            try:
                if float(hours) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return self.send_json(400, {"error": "hours must be a positive number"})
            args += ["--hours", fmt_hours(hours)]
        args += ["--caller", "panel"]
        rc, result, err = run_core(*args)
        if result is None:
            OUTBOX.append("lease-shorten-failed", {"session": session, "error": err, "rc": rc})
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if result.get("action") == "shortened":
            OUTBOX.append("lease-shortened", {"session": session, "result": result})
            return self.send_json(200, result)
        OUTBOX.append("lease-shorten-failed", {"session": session, "result": result})
        return self.send_json(409, result)

    def api_grant_extend(self, body: dict) -> None:
        hours = body.get("hours")
        if hours is None:
            return self.send_json(400, {"error": "hours is required"})
        try:
            if float(hours) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "hours must be a positive number"})
        rc, result, err = run_core("extend-grant", "--hours", fmt_hours(hours))
        if result is None:
            OUTBOX.append("grant-extend-failed", {"hours": hours, "error": err, "rc": rc})
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if is_success(rc, result):
            OUTBOX.append("grant-extended", {"hours": hours, "old_until": result.get("old_until"), "new_until": result.get("new_until")})
        else:
            OUTBOX.append("grant-extend-failed", {"hours": hours, "result": result})
        return self.send_json(200, result)

    def api_grant(self, body: dict) -> None:
        hours = body.get("hours")
        if hours is None:
            return self.send_json(400, {"error": "hours is required"})
        try:
            if float(hours) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "hours must be a positive number"})
        args = ["grant", "--hours", fmt_hours(hours)]
        burst = body.get("burst")
        if burst:
            if not isinstance(burst, str):
                return self.send_json(400, {"error": "burst must be a shape id"})
            args += ["--burst", burst]
        rc, result, err = run_core(*args)
        if result is None:
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if is_success(rc, result):
            OUTBOX.append("grant-started", {"hours": hours, "burst": burst, "result": result})
        return self.send_json(200, result)

    def api_ungrant(self) -> None:
        rc, result, err = run_core("ungrant")
        if result is None:
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if is_success(rc, result):
            OUTBOX.append("grant-ended", {"result": result})
        return self.send_json(200, result)

    def api_config_budget(self, body: dict) -> None:
        """Update the budget caps (the operator's throttle, editable
        from the panel): the core validates and persists them to
        config/lease.json. Only the caps present in the body change;
        a null clears its cap (no cap)."""
        args = ["budget-caps"]
        provided = False
        for field, flag in BUDGET_CAP_FIELDS.items():
            if field not in body:
                continue
            provided = True
            value = body[field]
            if value is None:
                args += [flag, "none"]
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                return self.send_json(400, {"error": f"{field} must be a non-negative number or null"})
            else:
                args += [flag, fmt_hours(value)]
        if not provided:
            return self.send_json(400, {"error": "no budget caps provided"})
        rc, result, err = run_core(*args)
        if result is None:
            return self.send_json(502, {"error": "lease core failed", "detail": err, "rc": rc})
        if is_success(rc, result):
            OUTBOX.append("budget-caps-changed", {"caps": result.get("caps"), "previous": result.get("previous")})
            return self.send_json(200, result)
        return self.send_json(400, result)

def main(argv: list[str]) -> int:
    global OUTBOX, STACKDUMP
    host, port = load_ui_config()
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        if arg == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
    OUTBOX = Outbox(outbox_path())
    if os.environ.get("COLAB_LEASE_UI_STACKDUMP", "1") != "0":
        STACKDUMP = StackDumpWatchdog(
            "colab-lease-panel",
            stackdump_health,
            var_dir() / "stackdumps.log",
            **load_stackdump_config(),
        )
        STACKDUMP.start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"lease-ui listening on http://{host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
