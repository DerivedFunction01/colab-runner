import importlib.util as _ilu
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, gate as _mod_gate, sessions, records as _mod_records, spend as _mod_spend, access, keepalive, grants, orphans, bridge, syncclaims

_extend_guard_spec = _ilu.spec_from_file_location("colab_extend_guard", Path(__file__).resolve().parent.parent / "extend_guard.py")
extend_guard = _ilu.module_from_spec(_extend_guard_spec)
_extend_guard_spec.loader.exec_module(extend_guard)


def status(colab, repo: Path, session: str | None = None) -> dict:
    rec = _mod_records.load_record(repo, session)
    if not rec:
        return {"action": "no-lease"}
    out = {"action": "active" if sessions.session_alive(colab, rec["session"]) else "recorded-but-dead", "record": rec}
    # The session's live sync claim rides the status read (the
    # panel renders holder + expiry from here); a lapsed claim
    # serves as None — honest absence, never a stale holder.
    out["sync_claim"] = syncclaims.read_sync_claim(repo, rec["session"])
    if out["action"] == "active" and foundation.load_config(repo).get("keepalive", {}).get("enabled"):
        age = keepalive.heartbeat_age(colab, repo, rec["session"])
        out["keepalive_heartbeat_age_s"] = round(age, 1) if age is not None else None
    return out


def specs(repo: Path, colab=None) -> list[dict]:
    """The shape catalog for the UI's picker: every shape with the
    fields the UI renders (identity, label, descriptor, rate). The
    UI serves costs from this list only. The label is the display
    name from the catalog (a shape id is an internal name; the UI
    shows labels); a shape without one falls back to its id."""
    gate = _mod_gate.session_gate_policy(repo)
    out = []
    for shape_id, entry in catalog.catalog_shapes(repo, colab).items():
        out.append({
            "id": shape_id,
            "label": entry.get("label") or shape_id,
            "gpu": entry.get("gpu"),
            "tpu": entry.get("tpu"),
            "high_mem": bool(entry.get("high_mem", False)),
            "rate_cu_per_hour": entry.get("rate_cu_per_hour"),
            # Session shape gate, served ready: whether an agent
            # actor may start this shape (the panel itself is a
            # bypass actor and can start any of them).
            "agent_startable": not gate["enabled"] or _mod_gate.shape_class(entry) in gate["agent_classes"],
        })
    return out


# --- State dial cache (E14) ---------------------------------------------------
# State composition dialed the account on every read: the
# budget portion's usage call and the orphan census's live
# list. Under account load those reads took 41-63 s, so the
# panel's data plane went stale-slow exactly when the account
# was busiest. Both dials cache in var/ on a short TTL — the
# mechanism's home is foundation (dial_cache_read /
# dial_cache_write), shared with access.access_list, which
# rides the same live-list cache file. Only these two dials
# cache — a session's tri-state classification is never
# served from cache.

STATE_DIAL_CACHE_TTL_S = foundation.DIAL_CACHE_TTL_S
_dial_cache_read = foundation.dial_cache_read
_dial_cache_write = foundation.dial_cache_write


def budget_snapshot(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Read-only budget numbers for `state`: the live balance and
    spend from the attributed accounting (segment journal + live
    records), never from balance deltas. The balance dial is
    TTL-cached (E14, above); the spend figures are local reads
    and are composed fresh on every call."""
    cfg = foundation.load_config(repo)
    now_dt = now_dt or datetime.now(timezone.utc)
    balance = _dial_cache_read(repo, "state-cache-usage.json", now_dt)
    if balance is None:
        try:
            balance = foundation.parse_balance(colab("usage", timeout=120).stdout)
        except Exception:  # noqa: BLE001
            balance = None
        if balance is not None:
            _dial_cache_write(repo, "state-cache-usage.json", balance, now_dt)
    day_spent = month_spent = standing_day_spent = None
    if balance is not None:
        spend = _mod_spend.attributed_spend(repo)
        day_spent = spend["day_spent"]
        month_spent = spend["month_spent"]
        # The standing caller's day spend from the same derivation
        # the guard enforces against — the number served is the
        # number enforced.
        standing_day_spent = spend["callers"].get(catalog.standing_caller(repo), {}).get("day_spent", 0.0)
    return {
        "balance": balance,
        "day_spent": day_spent,
        "month_spent": month_spent,
        "standing_day_spent": standing_day_spent,
        "caps": grants._served_caps(cfg),
    }


_UNSET = object()


def set_budget_caps(repo: Path, daily=_UNSET, monthly=_UNSET, standing_daily=_UNSET) -> dict:
    """Update the budget caps in config/lease.json — the operator's
    throttle, set from the panel (2026-11-01: the caps are the operator's to
    set, not a fixed system wall; he holds 700+ CU banked with no
    competing use for it). Each cap is a non-negative number or None
    (no cap — the budget guard's own semantics); an omitted argument
    leaves its cap untouched. Every other config key is preserved,
    and the write is atomic (tmp sibling + rename) in the file's own
    compact sorted style, so no reader sees a half-written config."""
    path = repo / "config" / "lease.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"action": "caps-invalid", "error": "config/lease.json is unreadable"}
    if not isinstance(cfg, dict):
        return {"action": "caps-invalid", "error": "config/lease.json is not a JSON object"}
    for name, value in (("daily", daily), ("monthly", monthly), ("standing_daily", standing_daily)):
        if value is _UNSET or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return {"action": "caps-invalid", "error": f"{name} cap must be a non-negative number or null"}
    standing = cfg.get("standing")
    if standing_daily is not _UNSET and standing is not None and not isinstance(standing, dict):
        return {"action": "caps-invalid", "error": "config standing section is not an object"}
    previous = grants._served_caps(cfg)

    def clean(v):
        return int(v) if isinstance(v, float) and v.is_integer() else v

    if daily is not _UNSET:
        cfg["daily_cu_budget"] = None if daily is None else clean(daily)
    if monthly is not _UNSET:
        cfg["monthly_cu_budget"] = None if monthly is None else clean(monthly)
    if standing_daily is not _UNSET:
        if not isinstance(standing, dict):
            standing = cfg["standing"] = {}
        standing["daily_cu_cap"] = None if standing_daily is None else clean(standing_daily)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, path)
    return {"action": "caps-updated", "caps": grants._served_caps(cfg), "previous": previous}


def auth_required(repo: Path) -> dict | None:
    """The wrapper guard's stop-and-surface marker
    (var/auth-required.json): present while the stored Colab
    credential is dead and a human re-authorization is the only way
    forward. Written and cleared by bin/colab-run's guard."""
    data = foundation.load_json(foundation.var_dir(repo) / "auth-required.json", None)
    return data if isinstance(data, dict) else None


def _envelope_block(colab, repo: Path, grant: dict | None, budget: dict) -> dict | None:
    """The envelope view state serves with an active grant (two-tier
    envelope plan): the grant, its scope, the live records attributed
    to it (envelope_id = the grant's granted_at), and spend versus
    the snapshot caps.

    Spend: per-lease figures come from the attributed accounting
    (segment journal + live records) — the lease's own burn in the
    period, shared between envelope and non-envelope work on that
    lease rather than metered per envelope. The totals are the
    budget snapshot's own numbers, the same ones the guard enforces
    against."""
    if grant is None:
        return None
    scope = grant.get("scope")
    if not isinstance(scope, dict):
        # Pre-envelope grant: synthesize the view from the grant's
        # own fields; its caps are the live ones (no snapshot was
        # taken), which grant_caps reports as None.
        scope = {"caps": grants._served_caps(foundation.load_config(repo)), "standing_shape": catalog.standing_shape(repo), "shapes": grants.grant_shapes(repo, grant)}
    caps = grants.grant_caps(grant) or scope["caps"]
    attributed = [rec for rec in _mod_records.all_records(repo) if rec.get("envelope_id") == grant.get("granted_at")]
    balance = budget.get("balance")
    spend = _mod_spend.attributed_spend(repo) if balance is not None else None
    session_spend = []
    for rec in attributed:
        rate = rec.get("rate_cu_per_hour")
        if rate is None and rec.get("shape"):
            rate = catalog.shape_rate(repo, rec["shape"])
        if spend is None or rate is None:
            day = month = None
        else:
            entry = spend["sessions"].get(rec["session"], {})
            day = entry.get("day_spent", 0.0)
            month = entry.get("month_spent", 0.0)
        session_spend.append({"session": rec["session"], "day_spent": day, "month_spent": month})
    return {
        "id": grant.get("granted_at"),
        "grant": grant,
        "scope": scope,
        "caps": caps,
        "records": attributed,
        "spend": {"balance": balance, "day_spent": budget.get("day_spent"), "month_spent": budget.get("month_spent"), "standing_day_spent": budget.get("standing_day_spent")},
        "session_spend": session_spend,
    }


def _bridge_block(colab, repo: Path) -> dict | None:
    """The served bridge state for the panel/artifact: armed, the
    heartbeat's age, live children, and the queue depth — all read
    from the agent's own status file, nothing derived client-side.
    None when no grant is active (the bridge has no home then)."""
    now_dt = datetime.now(timezone.utc)
    grant = grants.active_grant(repo, now_dt)
    if grant is None:
        return None
    cfg = foundation.load_config(repo)
    rec = grants.standing_record(repo)
    if not rec:
        return {"armed": False, "reason": "no-standing"}
    status = bridge.bridge_status_read(colab, repo, rec["session"])
    if status is None:
        return {"armed": False, "reason": "no-status"}
    age = None
    try:
        age = round(max(0.0, now_dt.timestamp() - foundation.parse_dt(status["ts"]).timestamp()))
    except Exception:  # noqa: BLE001
        pass
    return {
        "armed": age is not None and age <= bridge.BRIDGE_HEARTBEAT_STALE_S,
        "generation": str(status.get("gen", ""))[:8],
        "heartbeat_age_s": age,
        "auth_down": bool(status.get("auth_down")),
        "children": status.get("children", []),
        "queue": status.get("queue", 0),
        "processed": status.get("processed", 0),
    }


def state_records(colab, repo: Path) -> dict:
    """Portion: every lease record's status. The per-record reads
    are independent and I/O-bound (session probes, heartbeat
    reads), so they run threaded — the portion's wall time is its
    slowest record, not their sum."""
    recs = _mod_records.all_records(repo)

    def entry(rec):
        e = status(colab, repo, session=rec["session"])
        e["session"] = rec["session"]
        e["warm"] = rec.get("warm")
        e["caller"] = rec.get("caller")
        return e

    if len(recs) <= 1:
        records = [entry(rec) for rec in recs]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(recs))) as pool:
            records = list(pool.map(entry, recs))
    # The access list rides this portion: the panel's records
    # section is where live-but-unrecorded sessions surface.
    return {"records": records, "access": access.access_list(colab, repo)}


def state_specs(colab, repo: Path) -> dict:
    """Portion: the spec catalog (CLI shapes read from the day
    cache; a portion read never triggers a fresh catalog parse
    beyond what specs() itself already does)."""
    _shapes, catalog_error = catalog._cli_shapes(repo, colab)
    out = {"specs": specs(repo, colab), "session_gate": _mod_gate.session_gate_policy(repo)}
    if catalog_error:
        out["catalog_error"] = catalog_error
    return out


def state_budget(colab, repo: Path) -> dict:
    """Portion: the budget snapshot, including the balance read."""
    return {"budget": budget_snapshot(colab, repo)}


def state_grant(colab, repo: Path, budget: dict | None = None) -> dict:
    """Portion: the two-tier grant and its envelope block. The
    envelope prices on a budget snapshot; composed reads pass
    theirs in so the balance is read once."""
    grant = grants.load_grant(repo)
    if budget is None:
        budget = budget_snapshot(colab, repo)
    return {"grant": grant, "envelope": _envelope_block(colab, repo, grant, budget)}


def state_meta(colab, repo: Path) -> dict:
    """Portion: the bridge block and any outstanding auth need."""
    out = {"bridge": _bridge_block(colab, repo)}
    auth = auth_required(repo)
    if auth:
        out["auth_required"] = auth
    return out


def state_extend_guard(repo: Path) -> dict:
    """Portion: the auto-extension guard's resolved policy and
    its latest verdict per lease, from the guard's own state
    file. Before the guard's first pass the policy resolves
    from config and the verdicts are empty."""
    policy, _findings = extend_guard.load_policy(foundation.load_config(repo).get("extend_guard"))
    verdicts = extend_guard.guard_state(repo).get("verdicts", {})
    return {"extend_guard": {"policy": policy, "verdicts": verdicts}}


def state_orphans(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Portion: the orphan census — live sessions with no lease
    record, each entry derive_orphan's composition (the derived
    deadline and its evidence), ready for the panel to offer an
    adoption. Bridge-prefixed sessions are the bridge's, never
    the lease system's, and stay out; a handover child stays in,
    flagged, because its refusal reason is exactly what the
    operator needs to see. An unreadable live list serves an
    empty census flagged unreadable — no orphans rather than
    invented ones. The live list itself is TTL-cached (E14):
    inside the TTL the census composes from the cached list;
    a stale cache re-dials, and an unreadable dial still
    serves the flagged empty census (a cached list is only
    ever a successful dial's result)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    live = _dial_cache_read(repo, "state-cache-live.json", now_dt)
    if live is None:
        live = sessions.live_sessions(colab, repo)
        if live is not None:
            _dial_cache_write(repo, "state-cache-live.json", live, now_dt)
    if live is None:
        return {"orphans": [], "unreadable": True}
    recorded = {rec.get("session") for rec in _mod_records.all_records(repo)}
    out = []
    for entry in live:
        name = entry.get("session")
        if not name or name in recorded or name.startswith("bridge-"):
            continue
        out.append(orphans.derive_orphan(repo, entry))
    return {"orphans": out}


def state_torn_records(repo: Path) -> dict:
    """Portion: the torn-record findings (E1) — lease-record
    files whose contents cannot yield a record, served by
    filename-derived session instead of vanishing from every
    reader. A pure local read: the census never renames;
    quarantine is a writer's act (records.quarantine_torn,
    fired by save_record and the adoption path)."""
    return {"torn_records": _mod_records.torn_records(repo)}


def state(colab, repo: Path) -> dict:
    """The UI's composed read: every lease record's status, the
    two-tier grant, the spec catalog, and the budget snapshot,
    assembled from the portion reads above. All derivation lives
    here in the core; the UI only renders."""
    budget = budget_snapshot(colab, repo)
    out = {}
    out.update(state_records(colab, repo))
    out.update(state_specs(colab, repo))
    out["budget"] = budget
    out.update(state_grant(colab, repo, budget))
    out.update(state_meta(colab, repo))
    out.update(state_orphans(colab, repo))
    out.update(state_torn_records(repo))
    out.update(state_extend_guard(repo))
    return out
