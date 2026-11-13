"""Per-session listener census: the panel's standing hygiene
for leaked servers.

A leaked listener — a gate's dev serve whose job ended,
parented to init, cwd deleted — outlives every run record and
holds its port against the session's next tenant (2026-11-12:
two leaked next-servers on one session, one of which voided a
consumer's draw by answering its health check). Collection
rides the reap pass, throttled per session: the probe snippet
(snippets/listener_census.py) execs in-session and its JSON is
stamped to var/listener-census-<session>.json. The state
portion reads only the stamps — the panel renders served
state, never probes. A failed collection stamps its failure
and backs off the same interval, so a dead channel is not
retried every pass. Nothing here kills: a census that cannot
tell a leak from a deliberate serve names suspects and the
operator disposes.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from . import foundation

CENSUS_INTERVAL_S = 600
CENSUS_EXEC_TIMEOUT_S = 90
# A listener starting within this skew before the lease's own
# acquisition is the lease's own provisioning era, not a
# predecessor's leftover.
PREDATE_SKEW_S = 120
# The runtime's own platform cohort (kernel manager, jupyter,
# the ssh and fileshim bridges, node) starts within seconds of
# the runtime's boot — which predates the lease record whenever
# the record was written by a renewal or adoption after boot.
# A start inside this window after boot is the platform, never
# a predecessor's leftover.
BOOT_COHORT_S = 300


def census_path(repo: Path, session: str) -> Path:
    return foundation.var_dir(repo) / f"listener-census-{session}.json"


def _save_stamp(repo: Path, session: str, stamp: dict) -> None:
    path = census_path(repo, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(stamp))
    os.replace(tmp, path)


def read_census(repo: Path, session: str) -> dict | None:
    """The session's census stamp, or None when never taken
    (or unreadable — a torn stamp reads as no census, and the
    next collection rewrites it)."""
    try:
        stamp = json.loads(census_path(repo, session).read_text())
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


def _parse_probe(stdout: str) -> dict | None:
    """The probe's JSON is the last stdout line that parses as
    an object carrying listeners — the CLI's own notice lines
    ride the same stream (the preamble lesson, 2026-11-11)."""
    found = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("listeners"), list):
            found = obj
    return found


def collect_census(colab, repo: Path, session: str, now_dt: datetime | None = None) -> dict:
    """Exec the census probe on the session and stamp the
    result. Bounded and non-raising, like every standing read:
    a failed probe stamps the failure instead of raising."""
    now_dt = now_dt or datetime.now(timezone.utc)
    stamp: dict = {"session": session, "at": now_dt.isoformat()}
    try:
        probe = foundation.var_dir(repo) / "listener-census-probe.py"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text((repo / "snippets" / "listener_census.py").read_text())
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(probe), timeout=CENSUS_EXEC_TIMEOUT_S)
        result = _parse_probe(r.stdout or "") if r.returncode == 0 else None
        if result is None:
            stamp["error"] = "probe-failed"
        else:
            stamp["listeners"] = result["listeners"]
            if isinstance(result.get("boot_epoch"), (int, float)):
                stamp["boot_epoch"] = result["boot_epoch"]
    except Exception as exc:  # noqa: BLE001
        stamp["error"] = f"probe-error: {type(exc).__name__}"
    try:
        _save_stamp(repo, session, stamp)
    except OSError:
        pass
    return stamp


def maybe_collect(colab, repo: Path, rec: dict, now_dt: datetime | None = None) -> dict | None:
    """The reap pass's hook: collect at most once per interval
    per live lease. Returns the fresh stamp, or None when the
    standing stamp is still inside its interval (or the lease
    is past its deadline — a draining record's census is its
    successor's concern)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    session = rec.get("session")
    if not session:
        return None
    deadline = rec.get("deadline")
    if deadline:
        try:
            if foundation.parse_dt(deadline) <= now_dt:
                return None
        except (ValueError, TypeError):
            pass
    stamp = read_census(repo, session)
    if stamp is not None:
        try:
            age = (now_dt - foundation.parse_dt(stamp["at"])).total_seconds()
            if age < CENSUS_INTERVAL_S:
                return None
        except (KeyError, ValueError, TypeError):
            pass
    return collect_census(colab, repo, session, now_dt)


def _suspect_reasons(entry: dict, acquired_epoch: float | None, boot_epoch: float | None = None) -> list:
    reasons = []
    cwd = entry.get("cwd") or ""
    if cwd.endswith("(deleted)"):
        reasons.append("deleted-cwd")
    started = entry.get("started_epoch")
    if acquired_epoch is not None and isinstance(started, (int, float)) and started < acquired_epoch - PREDATE_SKEW_S:
        # The platform cohort starts with the runtime itself and
        # predates any record a renewal wrote; it is not a
        # leftover, so the reason stands only for a start clear
        # of the boot window.
        if boot_epoch is None or started > boot_epoch + BOOT_COHORT_S:
            reasons.append("predates-lease")
    return reasons


def listeners_state(repo: Path, rec: dict, now_dt: datetime | None = None) -> dict:
    """One session's served census: the stamp's listeners with
    their suspect derivation, ready for the panel to render.
    No stamp yet reads as an honest empty (at: None), never as
    zero listeners."""
    now_dt = now_dt or datetime.now(timezone.utc)
    session = rec.get("session") or ""
    stamp = read_census(repo, session) if session else None
    if stamp is None:
        return {"at": None, "age_s": None, "error": None, "listeners": []}
    acquired_epoch = None
    if rec.get("acquired_at"):
        try:
            acquired_epoch = foundation.parse_dt(rec["acquired_at"]).timestamp()
        except (ValueError, TypeError):
            acquired_epoch = None
    age_s = None
    try:
        age_s = round(max(0.0, (now_dt - foundation.parse_dt(stamp["at"])).total_seconds()))
    except (KeyError, ValueError, TypeError):
        pass
    listeners = []
    for entry in stamp.get("listeners") or []:
        if not isinstance(entry, dict):
            continue
        started = entry.get("started_epoch")
        listeners.append(
            {
                "port": entry.get("port"),
                "pid": entry.get("pid"),
                "cwd": entry.get("cwd") or "",
                "cmd": entry.get("cmd") or "",
                "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat() if isinstance(started, (int, float)) else None,
                "suspect": _suspect_reasons(entry, acquired_epoch, stamp.get("boot_epoch")),
            }
        )
    return {"at": stamp.get("at"), "age_s": age_s, "error": stamp.get("error"), "listeners": listeners}
