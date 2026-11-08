import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, sessions

# The heartbeat read is deliberately short-bounded: the file
# channel can stall indefinitely, so a stall degrades the age to
# unknown (None) instead of wedging status/state reads behind it.
HEARTBEAT_READ_TIMEOUT_S = 10


def heartbeat_age(colab, repo: Path, session: str) -> float | None:
    """Seconds since the keepalive's last heartbeat, via the file channel.
    None when there is no readable heartbeat (loop never ran, log gone,
    or the download failed) — including a download that stalls past
    HEARTBEAT_READ_TIMEOUT_S, which reads as unknown, not as an error."""
    dest = foundation.var_dir(repo) / "keepalive-tail.log"
    try:
        r = colab("download", "-s", session, "/content/keepalive.log", str(dest), timeout=HEARTBEAT_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return None
        lines = dest.read_text(errors="replace").strip().split("\n")
        ts = json.loads(lines[-1])["ts"]
        return max(0.0, datetime.now(timezone.utc).timestamp() - float(ts))
    except Exception:  # noqa: BLE001
        return None


def prime_session_host(colab, repo: Path, session: str) -> dict:
    """One cheap session-targeting call through the funnel, made by an
    initiating entry point right after an acquire changed the host
    (initiation-time approval plan, Step 2): the attempt itself is
    the product — it surfaces this caller scope's approval card for
    the new tunnel host in the same minute as the initiation, while
    the operator is present, instead of the scope discovering the
    host on its next background call. The probe is the heartbeat
    download (bounded, already the lease's liveness read); its
    outcome is reported, never raised, and never blocks the acquire
    result it rides on."""
    try:
        age = heartbeat_age(colab, repo, session)
    except Exception:  # noqa: BLE001
        return {"primed": False}
    return {"primed": True, "heartbeat_age_s": round(age, 1) if age is not None else None}


# The keepalive launch is bounded and non-raising for the same
# reason: its upload rides the stall-prone file channel. A failed
# launch is a result ("launch-failed"), never an exception — the
# lease stands and the next reap retries.
KEEPALIVE_UPLOAD_TIMEOUT_S = 30
KEEPALIVE_LAUNCH_TIMEOUT_S = 90


def ensure_keepalive(colab, repo: Path, session: str) -> dict:
    """Launch the detached keepalive loop on the session (single-instance
    on the VM side; safe to call repeatedly). The loop exists because an
    idle Colab runtime is reaped — the first away lease died at ~3 h —
    so a leased session must never sit truly idle (operator ruling, 2026-10-30).
    Never raises: a stalled or failed launch returns launch-failed and
    the caller carries on (the reaper retries on its next cycle)."""
    cfg = foundation.load_config(repo).get("keepalive", {})
    if not cfg.get("enabled"):
        return {"keepalive": "disabled"}
    try:
        colab("upload", "-s", session, str(repo / "snippets" / "keepalive.py"), "/content/keepalive.py", timeout=KEEPALIVE_UPLOAD_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"keepalive": "launch-failed", "stage": "upload"}
    launcher = foundation.var_dir(repo) / "launch-keepalive.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    max_cycles = os.environ.get("KEEPALIVE_MAX_CYCLES", "0")
    interval = os.environ.get("KEEPALIVE_INTERVAL_SECONDS", str(cfg.get("interval_seconds", 60)))
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        f"env.update({{'COLAB_RELEASE_TAG': 'keepalive', 'KEEPALIVE_MAX_CYCLES': '{max_cycles}', 'KEEPALIVE_INTERVAL_SECONDS': '{interval}'}})\n"
        "subprocess.Popen(['nohup', 'python3', 'keepalive.py'], stdout=open('keepalive.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('keepalive launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KEEPALIVE_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"keepalive": "launch-failed", "stage": "launch"}
    return {"keepalive": "launched" if r.returncode == 0 else "launch-failed", "stdout_tail": r.stdout.strip()[-120:]}


def keepalive_fresh(colab, repo: Path, session: str) -> tuple[bool, float | None]:
    cfg = foundation.load_config(repo).get("keepalive", {})
    age = heartbeat_age(colab, repo, session)
    return (age is not None and age <= float(cfg.get("stale_seconds", 600))), age


def kernel_pulse(colab, repo: Path, session: str) -> str:
    """One trivial execution in the session's kernel. The detached
    keepalive loop cannot produce kernel activity, and lease #2 died at
    ~6 h with the loop beating perfectly — so the reaper pulses the
    kernel itself (operator ruling, 2026-10-30). Returns ok | busy | failed; a
    local timeout reads as busy, because a kernel that cannot take the
    pulse is a kernel already executing, which is the activity being
    proven. The pulse body is an inert assignment."""
    pulse = foundation.var_dir(repo) / "lease-pulse.py"
    pulse.parent.mkdir(parents=True, exist_ok=True)
    pulse.write_text("__lease_pulse__ = 'ok'\nprint('lease pulse')\n")
    try:
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(pulse), timeout=90)
        return "ok" if r.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        return "busy"
    except Exception:  # noqa: BLE001
        return "failed"


def _capture_session_host(repo: Path, rec: dict, origin: str, spool_on_first: bool) -> bool:
    """Record the session's current tunnel host on the lease record
    (initiation-time approval plan, .plans/2026-11-03-initiation-time-
    approval.md). A host is born at exactly three moments — a fresh
    acquire, a renewal swap, an adoption — and network approvals are
    granted per exact host per caller scope, so each birth must
    surface at initiation instead of being discovered by a background
    caller's retries. When the host differs from the one on the
    record, spool an approval-needed event through the reaper-events
    spool (the companion drains it; the notify hook announces it).

    A record with no host yet baselines silently unless
    spool_on_first: a fresh acquire's host is a birth, while an
    existing lease met by the adopted path was approved in its own
    time and must not mint a false batch. Returns True when the host
    changed — the caller saves the record."""
    host = sessions.session_host(rec.get("session") or "")
    if not host:
        return False
    previous = rec.get("host")
    if previous == host:
        return False
    rec["host"] = host
    if previous is not None or spool_on_first:
        event = {"kind": "approval-needed", "shape": rec.get("shape"), "session": rec.get("session"), "host": host, "previous_host": previous, "origin": origin}
        if rec.get("caller"):
            # The approval announcement names whose work the host
            # serves (caller-attribution plan) — present only when
            # the record carries a caller, so unattributed births
            # announce as such by absence.
            event["caller"] = rec["caller"]
        foundation.append_reaper_event(repo, event)
    return True
