#!/usr/bin/env python3
"""The reader path: the per-run reader payload, one bounded
reader exec, and the poll-failure classification (E11) that
decides whether a failing poll means a flaky reader, a busy
kernel, or a session that died with its payload.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .foundation import REPO, colab, lease, now
from .records import mark_terminal, save_record

# E11: consecutive poll failures before the poller classifies the
# session. The classification itself uses the reap pass's reduced
# read budget (2 attempts x 45 s), so one classification costs a
# bounded ~90 s worst case, never the standard ~370 s.
POLL_FAILURE_BOUND = 3
# A launched job's footprint (its launcher log, opened by the
# stamp wrapper at spawn) exists from the launch instant; total
# absence of footprint this long after the launch exec is a
# launch that never landed, not a slow start.
LAUNCH_ABSENCE_GRACE_S = 600


def _absence_grace_s() -> int:
    """The launch-absence grace: env-tunable for the reason the
    poll timeout is (tests shrink it), defaulting to the
    constant."""
    return int(os.environ.get("COLAB_RUN_ABSENCE_GRACE_S", str(LAUNCH_ABSENCE_GRACE_S)))


def _launch_age_s(rec: dict) -> float:
    """Seconds since the launch exec fired (launch_at), falling
    back to the run's start for records that predate the stamp.
    Unparseable stamps read as 0 — an absence verdict never
    fires on a clock it cannot read."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(rec.get("launch_at") or rec.get("started_at"))).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def write_reader(run_id: str, remote_out: str, outputs: list) -> Path:
    """The per-run reader payload: checkpoint, heartbeat tail,
    launcher log tail, payload pids — plus each declared output's
    size/mtime, the change evidence pull_outputs skips on (E6)."""
    reader = REPO / "var" / f"read-{run_id}.py"
    reader.parent.mkdir(parents=True, exist_ok=True)
    reader.write_text(
        "import json, subprocess\n"
        "from pathlib import Path\n"
        f"d = Path({remote_out!r})\n"
        "ck = d / 'checkpoint.json'\n"
        "log = d / 'heartbeat.log'\n"
        f"jlog = Path({remote_out + '.log'!r})\n"
        f"names = {json.dumps(outputs)}\n"
        "def _stat(p):\n"
        "    try:\n"
        "        s = p.stat()\n"
        "        return {'size': s.st_size, 'mtime': s.st_mtime}\n"
        "    except OSError:\n"
        "        return None\n"
        "def _ecode(p):\n"
        "    try:\n"
        "        return int(p.read_text().strip())\n"
        "    except (OSError, ValueError):\n"
        "        return None\n"
        f"ecode = Path({remote_out + '.exit-code'!r})\n"
        "out = {'checkpoint': json.loads(ck.read_text()) if ck.exists() else None,\n"
        "       'heartbeat_tail': log.read_text().strip().split('\\n')[-3:] if log.exists() else [],\n"
        "       'job_log_tail': jlog.read_text(errors='replace').strip().split('\\n')[-5:] if jlog.exists() else [],\n"
        "       'exit_stamp': _ecode(ecode),\n"
        f"       'job_pids': subprocess.run(['pgrep','-f',{remote_out!r}], capture_output=True, text=True).stdout.split(),\n"
        "       'heartbeat_pids': subprocess.run(['pgrep','-f','heartbeat_job'], capture_output=True, text=True).stdout.split(),\n"
        "       'outputs': {n: _stat(d / n) for n in names}}\n"
        "print(json.dumps(out))\n"
    )
    return reader


def read_reader(session: str, reader: Path, poll_timeout: int) -> tuple[dict | None, str | None]:
    """One bounded reader exec. Returns (state, None) on a
    readable poll, (None, error) on a failed one — a subprocess
    timeout and an unparseable answer are the same class here:
    this poll produced no evidence (E11 counts both)."""
    try:
        chk = colab("exec", "-s", session, "--timeout", "120", "-f", str(reader), timeout=poll_timeout)
    except subprocess.TimeoutExpired:
        return None, f"checkpoint poll timed out at {now()}"
    try:
        return json.loads(chk.stdout[chk.stdout.index("{"):]), None
    except (ValueError, json.JSONDecodeError):
        return None, f"checkpoint poll returned no readable state at {now()}"


def register_poll_failure(rec: dict, session: str, error: str) -> str:
    """Count one consecutive poll failure (E11) and, at the
    bound, classify the session at the reap pass's reduced read
    budget. Confirmed absent fails the run fast — the payload
    died with its session, and the record carries the resume
    note. Alive means the reader, not the session, is flaky: the
    count resets. Unknown never fails a run; the count stands,
    so the next failure classifies again. Returns "failed" when
    this failure ended the run, else "continue"."""
    rec["last_poll_error"] = error
    rec["poll_failures"] = int(rec.get("poll_failures") or 0) + 1
    save_record(rec)
    if rec["poll_failures"] < POLL_FAILURE_BOUND:
        return "continue"
    diag: dict = {}
    verdict = lease.session_state(colab, session, diag, attempts=lease._REAP_READ_ATTEMPTS, timeout=lease._REAP_READ_TIMEOUT_S)
    rec["last_classification"] = {"verdict": verdict, "at": now(), **diag}
    if verdict == "absent":
        mark_terminal(
            rec,
            "failed",
            error=(
                f"session {session} confirmed absent after {rec['poll_failures']} consecutive poll failures; "
                f"the payload died with its session — relaunch with --resume {rec['run_id']} to continue from its remote run-id"
            ),
        )
        return "failed"
    if verdict == "alive":
        rec["poll_failures"] = 0
    save_record(rec)
    return "continue"
