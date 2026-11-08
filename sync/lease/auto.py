import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, gate, sessions, records, spend, lifecycle

def request_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "lease-request.json"


def write_request(repo: Path, reason: str = "", shape: str | None = None, now_dt: datetime | None = None, caller: str | None = None, via: str | None = None, not_before: datetime | str | None = None) -> dict:
    """Record a demand for a lease (var/lease-request.json). Any driver
    may write it (bin/colab-lease request); the auto-acquire duty is the
    only consumer. `caller` is the requester: auto_acquire copies it
    onto the lease record it creates, so a lease started by demand is
    attributable to the thread that asked (caller-attribution plan —
    the demand's old `via` tag generalized; `via` remains accepted as
    the same claim's legacy spelling). `not_before` makes the demand a
    timed start: the duty leaves it untouched until its time comes
    (two-tier envelope plan — a timed start is data the hook consumes,
    never an agent-fired command)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    if caller is None:
        caller = via
    # Session shape gate, early refusal: a demand whose requester
    # is identified (a caller claim) and whose shape is catalogued
    # is checked now, so a refused demand never sits in the file for
    # the duty to retry. An unattributed demand (no caller) defers
    # to the consumption check in auto_acquire, which re-checks
    # before acquiring. The check is shape-only (creation=False):
    # a demand is an ask, never a creation.
    if caller is not None and shape is not None and shape in catalog.catalog_shapes(repo):
        refusal = gate.enforce_session_admission(repo, shape, seam="request", caller=caller, creation=False)
        if refusal is not None:
            return refusal
    req = {"requested_at": now_dt.isoformat(), "reason": reason, "shape": shape}
    if caller:
        req["caller"] = caller
    if not_before is not None:
        req["not_before"] = not_before.isoformat() if isinstance(not_before, datetime) else foundation.parse_dt(str(not_before)).isoformat()
    p = request_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(req, indent=2))
    return {"action": "requested", "request": req}


def load_request(repo: Path) -> dict | None:
    req = foundation.load_json(request_path(repo), None)
    return req if isinstance(req, dict) and req.get("requested_at") else None


def clear_request(repo: Path) -> None:
    request_path(repo).unlink(missing_ok=True)


def auto_enabled(cfg: dict) -> bool:
    """The config seam is config/lease.json's auto_acquire.enabled; the
    COLAB_RUNNER_AUTO_ACQUIRE env var overrides it (the test harness
    uses the override so it never edits the tracked config)."""
    env = os.environ.get("COLAB_RUNNER_AUTO_ACQUIRE")
    if env is not None:
        return env == "1"
    return bool(cfg.get("auto_acquire", {}).get("enabled"))


def auto_acquire(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty, inert unless auto-acquire is enabled: when a driver
    has left a demand request and no live lease satisfies it, acquire
    one through acquire() — the same budget guard, keepalive launch,
    and kernel pulse as a manual acquire. The request is consumed when
    a lease is live or newly created; it is kept when acquisition was
    refused or failed, so a later poll retries. Makes no colab calls at
    all while disabled or when there is no demand.

    A demand is satisfied by a live lease at its shape held by its
    own caller (an unattributed demand, by any live lease at the
    shape): leases are per-caller, so another caller's lease at the
    same shape never silently satisfies this caller's ask."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    if not auto_enabled(cfg):
        return {"action": "auto-disabled"}
    req = load_request(repo)
    if req is None:
        return {"action": "no-demand"}
    not_before = req.get("not_before")
    if not_before:
        try:
            due_at = foundation.parse_dt(str(not_before))
        except ValueError:
            due_at = None  # an unparseable stamp never wedges the demand
        if due_at is not None and now_dt < due_at:
            # A timed start that has not come due: the request is
            # neither consumed nor failed — it simply waits for a
            # later poll. No colab calls happen on this path.
            return {"action": "not-due", "not_before": not_before, "shape": req.get("shape")}
    shapes = catalog.catalog_shapes(repo, colab)
    shape_id = req.get("shape") or catalog.default_shape(repo)
    if shape_id not in shapes:
        return {"action": "auto-failed", "error": f"unknown shape {shape_id!r}"}
    demand_caller = req.get("caller") or req.get("via")
    candidates = [r for r in records.all_records(repo) if r.get("shape") == shape_id and (demand_caller is None or r.get("caller") == demand_caller)]
    rec = max(candidates, key=lambda r: r.get("deadline") or "", default=None)
    if rec:
        state = sessions.session_state(colab, rec["session"])
        if state == "alive":
            clear_request(repo)
            return {"action": "satisfied-live", "session": rec["session"]}
        if state == "unknown":
            return {"action": "skipped-unknown", "session": rec["session"]}
        spend.append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
        records.delete_record(repo, rec["session"])  # confirmed dead: clear before acquiring
    # The duty creates on the demand: the demand's caller becomes
    # the lease's caller (a pre-attribution demand's via tag reads
    # as the same claim), and the duty marker carries the creation
    # past the agent creation denial — the shape check inside
    # acquire still applies in full.
    result = lifecycle.acquire(colab, repo, shape_id, now_dt=now_dt, caller=demand_caller, duty=True)
    action = result.get("action")
    if action == "created":
        clear_request(repo)
        return {"action": "auto-acquired", "session": result["record"]["session"], "deadline": result["record"]["deadline"], "reason": req.get("reason", ""), "via": req.get("via"), "caller": demand_caller}
    if action == "adopted":
        clear_request(repo)
        return {"action": "satisfied-live", "session": result["record"]["session"]}
    if action == "shape-refused":
        # A shape refusal is permanent for this demand (unlike a
        # budget refusal, which a later poll may outgrow): clear
        # the demand so the duty never retries it forever. The
        # refusal itself is journaled at the acquire seam; this
        # entry records the demand's disposition.
        clear_request(repo)
        gate.append_gate_journal(repo, {"kind": "demand-shape-refused", "seam": "auto", "shape": shape_id, "via": req.get("via"), "caller": demand_caller, "shape_class": result.get("shape_class")})
        return {"action": "skipped-shape", "shape": shape_id, "shape_class": result.get("shape_class")}
    if action == "refused-budget":
        return {"action": "skipped-budget", "budget": result.get("budget")}
    if action == "acquire-unknown":
        return {"action": "skipped-unknown", "session": result.get("session")}
    return {"action": "auto-failed", "error": result.get("error", action)}
