from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, sessions, records, grants, lifecycle

def standing_duty(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty for the two-tier grant. While a grant is active, keep
    the standing lease live: acquire it when absent or dead, with a
    session deadline of min(grant end, now + standing
    .max_session_hours); a created standing lease is marked
    "standing": true on its record, plus "warm": "pending" when
    standing.warm_on_renew is set (the warm run itself is a later
    step). When the grant has expired, release the standing lease,
    clear the grant, and report standing-expired. With no grant on
    file the duty is inert — an orphaned standing record is the
    reaper's to enforce."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    standing_cfg = cfg.get("standing", {})
    grant = grants.load_grant(repo)
    if grant is None:
        return {"action": "no-grant"}
    rec = grants.standing_record(repo)
    if not grants.grant_active(grant, now_dt):
        out: dict = {"action": "standing-expired", "standing": {"action": "no-lease"}}
        if rec is not None and rec.get("standing"):
            out["standing"] = lifecycle.release(colab, repo, reason="grant-expired", session=rec["session"])
        elif rec is not None:
            out["standing"] = {"action": "not-standing", "session": rec["session"]}
        grants.clear_grant(repo)
        return out
    shape_id = catalog.standing_shape(repo)
    if rec is None:
        # A live lease at the standing shape held by another caller
        # already satisfies "up"; it is not adopted into standing
        # semantics (the pre-retirement single-slot behaviour, kept
        # for the standing shape only).
        others = [r for r in records.all_records(repo) if r.get("shape") == shape_id]
        other = max(others, key=lambda r: r.get("deadline") or "", default=None)
        if other is not None and now_dt < foundation.parse_dt(other["deadline"]) and sessions.session_alive(colab, other["session"]):
            return {"action": "satisfied-live", "session": other["session"], "standing": False}
    if rec is not None and rec.get("standing"):
        if now_dt < foundation.parse_dt(rec["deadline"]) and sessions.session_alive(colab, rec["session"]):
            return {"action": "standing-kept", "session": rec["session"], "deadline": rec["deadline"], "grant_until": grant["until"]}
    max_session_hours = float(standing_cfg.get("max_session_hours", 12))
    remaining_hours = (foundation.parse_dt(grant["until"]) - now_dt).total_seconds() / 3600.0
    result = lifecycle.acquire(colab, repo, shape_id, now_dt=now_dt, hours=min(remaining_hours, max_session_hours), max_hours_cap=max_session_hours, via=foundation.GATE_DEFAULT_INFRA_ACTOR, caller=catalog.standing_caller(repo), standing=True)
    action = result.get("action")
    if action == "created":
        new_rec = result["record"]
        new_rec["standing"] = True
        if standing_cfg.get("warm_on_renew"):
            new_rec["warm"] = "pending"
        records.save_record(repo, new_rec)
        return {"action": "standing-acquired", "record": new_rec, "warm": new_rec.get("warm"), "grant_until": grant["until"]}
    if action == "adopted":
        return {"action": "standing-kept", "session": result["record"]["session"], "deadline": result["record"]["deadline"], "grant_until": grant["until"]}
    if action == "refused-budget":
        return {"action": "standing-refused-budget", "budget": result.get("budget")}
    return {"action": "standing-failed", "error": result.get("error", action)}
