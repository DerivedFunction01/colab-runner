"""Launch-channel health: the adoption pool's memory of the
funnel's recent launch outcomes, per session.

The runner's launch path (uploads and their verify probes, the
seed exec, the launch exec) appends one journal line per channel
operation to var/launch-health.jsonl. Adoption reads the journal
back as a health map: a session whose recent launch operations
failed — the post-job exec-stall shape, where the session that
just finished a heavy job times out at the next launch's probe
for minutes at a time — ranks below healthy sessions within its
caller tier, so the funnel stops feeding runs into the one
wedged launcher while the fleet has a working one. Health never
reorders the caller tiers and never blocks adoption outright:
when every candidate is degraded, the pool still adopts (a
degraded session beats no session, and the stall is
self-clearing). The journal is evidence, not state: a missing
or corrupt journal reads as no evidence, never as failure.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation

WINDOW_MINUTES = 30
FAILURE_CLUSTER = 2


def launch_health_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "launch-health.jsonl"


def append_launch_outcome(repo: Path, session: str, stage: str, ok: bool, detail: str = "") -> None:
    """Append one launch-channel outcome. Best-effort: a failed
    append must never fail the launch it describes."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "stage": stage,
        "ok": bool(ok),
        "detail": detail[:200],
    }
    try:
        path = launch_health_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def launch_health(repo: Path, now_dt: datetime | None = None) -> dict:
    """{session: {"degraded": bool, "last_ok": ts|None,
    "last_fail": ts|None, "fails": int}} over the trailing
    window. A session is degraded when its most recent outcome
    in the window is a failure, or when FAILURE_CLUSTER or more
    failures follow its last success in the window. Sessions with
    no outcomes in the window are absent from the map (no
    evidence reads as neutral, never as degraded)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(minutes=WINDOW_MINUTES)
    health: dict = {}
    try:
        lines = launch_health_path(repo).read_text().splitlines()
    except OSError:
        return health
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            at = foundation.parse_dt(entry["ts"])
            session = entry["session"]
        except (ValueError, KeyError, TypeError):
            continue
        if at < cutoff:
            continue
        slot = health.setdefault(session, {"degraded": False, "last_ok": None, "last_fail": None, "fails": 0, "_fails_since_ok": 0})
        if entry.get("ok"):
            slot["last_ok"] = entry["ts"]
            slot["_fails_since_ok"] = 0
        else:
            slot["last_fail"] = entry["ts"]
            slot["fails"] += 1
            slot["_fails_since_ok"] += 1
    for slot in health.values():
        slot["degraded"] = bool(
            (slot["last_fail"] and (slot["last_ok"] is None or slot["last_fail"] > slot["last_ok"]))
            or slot["_fails_since_ok"] >= FAILURE_CLUSTER
        )
        del slot["_fails_since_ok"]
    return health
