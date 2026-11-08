import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, callers, access

# --- Orphaned sessions: evidence and adoption ----------------------------------
#
# Local why: a live session with no lease record is invisible to
# budget attribution and deadline enforcement; its history lives
# only in the journals and kit configs the lifecycle left behind.
# Mechanics: docs/lease-workflow.md.


def _jsonl_events(path: Path) -> list[dict]:
    """Every JSON object in a .jsonl journal, in file order. A
    missing file reads as no events; a torn line is skipped —
    journals are evidence, never a reason to fail a read."""
    events: list[dict] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return events
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _orphan_contribution(source: str, **fields) -> dict:
    contribution = {"source": source, "deadline": None, "acquired_at": None, "shape": None, "spec": None, "caller": None, "at": None}
    contribution.update(fields)
    return contribution


def _dt_or_none(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return foundation.parse_dt(value)
    except ValueError:
        return None


def orphan_evidence(repo: Path, session: str) -> dict:
    """Every local source's contribution to one unrecorded
    session's history: {session, sources, kit_installed,
    handover_pending}, each source a {source, deadline,
    acquired_at, shape, spec, caller, at} contribution (None
    where the source carries nothing). Reads are local-only and
    tolerant: a missing or unparseable source contributes
    nothing, never an exception (the load_json pattern).
    Deadline evidence exists in exactly two places — the
    renewal kit's config (a verbatim copy of the record at
    install) and the panel outbox's lease-extended chain —
    and nowhere else: claims, liveness state, and run records
    carry caller/shape/first-seen evidence only."""
    var = foundation.var_dir(repo)
    sources: list[dict] = []
    kit = foundation.load_json(var / f"renewal-config-{session}.json", None)
    kit_installed = isinstance(kit, dict)
    if kit_installed:
        spec = {"gpu": kit.get("gpu"), "tpu": kit.get("tpu"), "high_mem": bool(kit.get("high_mem"))}
        sources.append(_orphan_contribution("renewal-config", deadline=kit.get("deadline"), acquired_at=kit.get("acquired_at"), shape=kit.get("shape"), spec=spec))
    for event in _jsonl_events(var / "ui-outbox.jsonl"):
        detail = event.get("detail")
        if event.get("kind") != "lease-extended" or not isinstance(detail, dict) or detail.get("session") != session:
            continue
        sources.append(_orphan_contribution("ui-outbox", deadline=detail.get("new_deadline"), at=event.get("ts")))
    for event in _jsonl_events(foundation.reaper_events_path(repo)):
        if session not in (event.get("session"), event.get("new_session")):
            continue
        if event.get("caller"):
            sources.append(_orphan_contribution("reaper-events", caller=event.get("caller"), at=event.get("ts")))
    for claim in _jsonl_events(callers.caller_claims_path(repo)):
        if claim.get("session") == session and claim.get("caller"):
            sources.append(_orphan_contribution("caller-claims", caller=claim.get("caller"), at=claim.get("ts")))
            break
    liveness = foundation.load_json(var / f"liveness-{session}.json", None)
    if isinstance(liveness, dict):
        sources.append(_orphan_contribution("liveness", shape=liveness.get("shape"), caller=liveness.get("caller"), at=liveness.get("first_seen")))
    runs_dir = repo / "results" / "runs"
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.json")):
            run = foundation.load_json(path, None)
            if not isinstance(run, dict) or run.get("session") != session:
                continue
            contribution = _orphan_contribution("run-record", caller=run.get("caller"), at=run.get("started_at"))
            contribution["run_id"] = run.get("run_id") or path.stem
            contribution["state"] = run.get("state")
            sources.append(contribution)
    handover = foundation.load_json(var / "lease-handover.json", None)
    handover_pending = isinstance(handover, dict) and handover.get("child_session") == session
    return {"session": session, "sources": sources, "kit_installed": kit_installed, "handover_pending": handover_pending}


def derive_orphan(repo: Path, live_entry: dict) -> dict:
    """One orphan census entry, composed from the session's
    evidence by the priority ruling: the renewal-config's
    deadline/acquired_at/shape first (a verbatim copy of the
    record at kit install), any later lease-extended outbox
    event applied over it, and nothing else carrying a
    deadline. The record's creator is the renewal-origin
    caller in the reaper events, never the earliest claim — a
    claim is a ride, and a rider may predate nothing but their
    own attach. Every conflicting value is served with its
    source; the operator can always override at adoption.
    Shape from evidence first, else the live entry's class
    cross-checked against the catalog: candidates are the
    entries consistent with the class evidence (a high-RAM
    endpoint names high memory, not an accelerator, so its
    candidates span the high-memory CPU and accelerator
    shapes); more than one candidate is ambiguity, never a
    guess."""
    session = live_entry.get("session")
    evidence = orphan_evidence(repo, session)
    by_source: dict[str, list[dict]] = {}
    for contribution in evidence["sources"]:
        by_source.setdefault(contribution["source"], []).append(contribution)

    derived_deadline = None
    deadline_source = None
    deadline_stale_possible = False
    acquired_at = None
    shape = None
    shape_spec = None
    shape_evidenced = False
    kit_cfg = (by_source.get("renewal-config") or [None])[0]
    if kit_cfg is not None:
        acquired_at = kit_cfg.get("acquired_at")
        if kit_cfg.get("deadline"):
            derived_deadline = kit_cfg["deadline"]
            deadline_source = "renewal-config"
        if kit_cfg.get("spec"):
            shape_spec = kit_cfg["spec"]
            shape = catalog.shape_id_for(shape_spec.get("gpu"), shape_spec.get("tpu"), shape_spec.get("high_mem"))
            shape_evidenced = True
        elif kit_cfg.get("shape"):
            shape = kit_cfg["shape"]
            shape_evidenced = True
    extensions = by_source.get("ui-outbox") or []
    if extensions:
        last = extensions[-1]
        if _dt_or_none(last.get("deadline")) is not None:
            derived_deadline = last["deadline"]
            deadline_source = "ui-outbox"
        else:
            # An extension the config cannot reflect (extend does
            # not rewrite it once the kit is installed) whose
            # outbox event carries no usable new deadline: the
            # derived value may be stale, and says so.
            deadline_stale_possible = True

    conflicts: list[dict] = []
    deadline_values = []
    if kit_cfg is not None and kit_cfg.get("deadline"):
        deadline_values.append({"source": "renewal-config", "value": kit_cfg["deadline"]})
    for contribution in extensions:
        if contribution.get("deadline"):
            deadline_values.append({"source": "ui-outbox", "value": contribution["deadline"]})
    if len({v["value"] for v in deadline_values}) > 1:
        conflicts.append({"field": "deadline", "values": deadline_values})

    creator_caller = None
    for contribution in by_source.get("reaper-events") or []:
        if contribution.get("caller"):
            creator_caller = contribution["caller"]
            break
    first_claim = None
    claim = (by_source.get("caller-claims") or [None])[0]
    if claim is not None:
        first_claim = claim.get("caller")
    if creator_caller and first_claim and creator_caller != first_claim:
        conflicts.append({"field": "caller", "values": [{"source": "reaper-events", "value": creator_caller}, {"source": "caller-claims", "value": first_claim}]})

    shape_candidates: list[str] = []
    if not shape_evidenced:
        cls = live_entry.get("shape_class")
        entries = catalog.catalog_shapes(repo)
        if cls == "high_ram":
            shape_candidates = [sid for sid, entry in entries.items() if entry.get("high_mem")]
        elif cls == "standard":
            shape_candidates = [sid for sid, entry in entries.items() if not entry.get("high_mem")]
        if len(shape_candidates) == 1:
            shape = shape_candidates[0]

    jobs_in_flight = [c["run_id"] for c in by_source.get("run-record") or [] if c.get("state") in ("starting", "running", "polling") and c.get("run_id")]
    seen = [c["at"] for c in evidence["sources"] if c.get("at")]
    stamp = access.load_unattributed(repo).get(session) or {}
    if stamp.get("first_seen"):
        seen.append(stamp["first_seen"])
    return {
        "session": session,
        "endpoint": live_entry.get("endpoint"),
        "shape_class": live_entry.get("shape_class"),
        "shape": shape,
        "shape_spec": shape_spec,
        "shape_evidenced": shape_evidenced,
        "shape_candidates": shape_candidates,
        "creator_caller": creator_caller,
        "first_claim": first_claim,
        "derived_deadline": derived_deadline,
        "deadline_source": deadline_source,
        "deadline_stale_possible": deadline_stale_possible,
        "conflicts": conflicts,
        "acquired_at": acquired_at,
        "jobs_in_flight": jobs_in_flight,
        "first_seen": min(seen) if seen else None,
        "kit_installed": evidence["kit_installed"],
        "handover_pending": evidence["handover_pending"],
    }
