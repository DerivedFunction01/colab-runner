import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, records

def grant_ledger_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "grant-ledger.jsonl"


def append_grant_ledger(repo: Path, event: dict) -> bool:
    """Append one envelope lifecycle event to var/grant-ledger.jsonl
    (two-tier envelope plan): grant started, lease attributed,
    renewed, released, grant ended. Best-effort like the reaper
    spool — a failed ledger write never breaks the lifecycle action
    it records."""
    try:
        p = grant_ledger_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
        return True
    except OSError:
        return False


def load_grant_ledger(repo: Path) -> list[dict]:
    out = []
    try:
        lines = grant_ledger_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _note_envelope_lease_started(repo: Path, rec: dict) -> None:
    """Record an envelope attribution in the ledger, and spool the
    envelope-lease-started event the companion drains into the UI
    outbox (the drain is kind-generic; the panel renders this kind).
    The record's caller rides both events when it carries one."""
    ledger = {"kind": "lease-attributed", "envelope_id": rec.get("envelope_id"), "shape": rec.get("shape"), "via": rec.get("via"), "session": rec.get("session"), "deadline": rec.get("deadline")}
    spool = {"kind": "envelope-lease-started", "shape": rec.get("shape"), "envelope_id": rec.get("envelope_id"), "via": rec.get("via")}
    if rec.get("caller"):
        ledger["caller"] = rec["caller"]
        spool["caller"] = rec["caller"]
    append_grant_ledger(repo, ledger)
    foundation.append_reaper_event(repo, spool)


def _note_envelope_lease_released(repo: Path, rec: dict, reason: str) -> None:
    if not rec.get("envelope_id"):
        return
    ledger = {"kind": "lease-released", "envelope_id": rec.get("envelope_id"), "shape": rec.get("shape"), "via": rec.get("via"), "reason": reason}
    spool = {"kind": "envelope-lease-released", "shape": rec.get("shape"), "envelope_id": rec.get("envelope_id"), "reason": reason}
    if rec.get("caller"):
        ledger["caller"] = rec["caller"]
        spool["caller"] = rec["caller"]
    append_grant_ledger(repo, ledger)
    foundation.append_reaper_event(repo, spool)


def grant_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "grant.json"


def load_grant(repo: Path) -> dict | None:
    grant = foundation.load_json(grant_path(repo), None)
    return grant if isinstance(grant, dict) and grant.get("kind") == "two-tier" and grant.get("until") else None


def grant_active(grant: dict | None, now_dt: datetime) -> bool:
    return grant is not None and now_dt < foundation.parse_dt(grant["until"])


def grant_scope(cfg: dict, standing: str, burst: str) -> dict:
    """The scope a grant sanctions (two-tier envelope plan): the
    shapes it covers and a snapshot of the budget caps in force at
    grant time, in the shape state serves caps in. The snapshot is
    the authorization the operator's tap granted — later config
    edits move future grants and ordinary leases, never this one."""
    shapes = [standing] if standing == burst else [standing, burst]
    return {"caps": _served_caps(cfg), "standing_shape": standing, "burst_shape": burst, "shapes": shapes}


def grant_shapes(repo: Path, grant: dict) -> list:
    """The shapes inside a grant's scope: the scope's own shapes
    list, exactly as written. A scope that names no shapes
    sanctions none."""
    scope = grant.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("shapes"), list) and scope["shapes"]:
        return list(scope["shapes"])
    return []


def grant_caps(grant: dict) -> dict | None:
    """The grant's snapshot caps, or None for a pre-envelope grant
    (no snapshot was taken, so live config caps still price it)."""
    scope = grant.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("caps"), dict):
        return scope["caps"]
    return None


def active_grant(repo: Path, now_dt: datetime) -> dict | None:
    grant = load_grant(repo)
    return grant if grant_active(grant, now_dt) else None


def shape_in_grant(repo: Path, grant: dict | None, shape_id: str | None) -> bool:
    return grant is not None and shape_id is not None and shape_id in grant_shapes(repo, grant)


def write_grant(repo: Path, hours: float, burst_shape: str | None = None, now_dt: datetime | None = None) -> dict:
    """Record a two-tier grant: for the next `hours`, the standing duty
    keeps the standing tier up alongside the burst tier. The burst is
    the default shape unless --burst names another catalog shape. The
    grant is an authorization envelope: its `granted_at` is its id and
    its `scope` snapshots the shapes and budget caps the grant tap
    sanctioned."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    standing = catalog.standing_shape(repo)
    burst = burst_shape or catalog.default_shape(repo) or standing
    grant = {
        "kind": "two-tier",
        "granted_at": now_dt.isoformat(),
        "until": (now_dt + timedelta(hours=float(hours))).isoformat(),
        "standing_shape": standing,
        "burst_shape": burst,
        "scope": grant_scope(cfg, standing, burst),
    }
    p = grant_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=2))
    append_grant_ledger(repo, {"kind": "grant-started", "envelope_id": grant["granted_at"], "until": grant["until"], "scope": grant["scope"]})
    return {"action": "granted", "grant": grant}


def clear_grant(repo: Path) -> None:
    grant = load_grant(repo)
    if grant is not None:
        append_grant_ledger(repo, {"kind": "grant-ended", "envelope_id": grant.get("granted_at")})
    grant_path(repo).unlink(missing_ok=True)


def standing_record(repo: Path) -> dict | None:
    """The standing duty's lease record: stamped standing:true at
    creation; identified by its caller for a record the duty wrote
    before caller stamps existed. The standing duty holds one
    lease; when flag and caller disagree, the flag wins."""
    recs = records.all_records(repo)
    for rec in recs:
        if rec.get("standing"):
            return rec
    caller = catalog.standing_caller(repo)
    for rec in recs:
        if rec.get("caller") == caller:
            return rec
    return None


def _served_caps(cfg: dict) -> dict:
    """The caps in the shape state serves: config's daily/monthly
    budgets plus the standing caller's daily sub-cap."""
    standing = cfg.get("standing")
    return {
        "daily": cfg.get("daily_cu_budget"),
        "monthly": cfg.get("monthly_cu_budget"),
        "standing_daily": standing.get("daily_cu_cap") if isinstance(standing, dict) else None,
    }
