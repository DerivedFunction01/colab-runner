import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, callers, gate, sessions, records as _mod_records

def unattributed_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "unattributed.json"


def load_unattributed(repo: Path) -> dict:
    raw = foundation.load_json(unattributed_path(repo), {})
    return raw if isinstance(raw, dict) else {}


def save_unattributed(repo: Path, stamps: dict) -> None:
    p = unattributed_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stamps, indent=2, sort_keys=True))


def clear_unattributed(repo: Path, session: str) -> None:
    stamps = load_unattributed(repo)
    if session in stamps:
        del stamps[session]
        save_unattributed(repo, stamps)


def access_list(colab, repo: Path) -> list[dict]:
    """Every live session with its attribution, served ready for
    the panel: record-backed sessions carry their record's caller
    and acquired_at and their shape's declared shape class;
    claim-only sessions carry the latest claim and its time; the
    rest are unattributed (caller None, first_seen from the
    detection stamp). An unreadable live list serves an empty
    list — no access section rather than a fabricated one."""
    live = sessions.live_sessions(colab, repo)
    if live is None:
        return []
    records = {rec.get("session"): rec for rec in _mod_records.all_records(repo)}
    claims = callers.load_caller_claims(repo)
    stamps = load_unattributed(repo)
    shapes = catalog.catalog_shapes(repo)
    out = []
    for entry in live:
        name = entry["session"]
        rec = records.get(name)
        if rec is not None:
            shape = shapes.get(rec.get("shape")) or {}
            out.append({"session": name, "endpoint": entry["endpoint"], "shape_class": entry["shape_class"] or gate.shape_class(shape), "caller": rec.get("caller"), "first_seen": rec.get("acquired_at")})
            continue
        claim = claims.get(name)
        if claim is not None:
            out.append({"session": name, "endpoint": entry["endpoint"], "shape_class": entry["shape_class"], "caller": claim.get("caller"), "first_seen": claim.get("ts")})
            continue
        stamp = stamps.get(name) or {}
        out.append({"session": name, "endpoint": entry["endpoint"] or stamp.get("endpoint"), "shape_class": entry["shape_class"] or stamp.get("shape_class"), "caller": None, "first_seen": stamp.get("first_seen"), "unattributed": True})
    return out


def release_session(colab, repo: Path, session: str) -> dict:
    """Stop one session by name (the panel's one-tap release for an
    unattributed session) and clear its detection stamp. The stop
    goes through stop_session_safe — the server-verified stop —
    never a raw stop. A session with a lease record is refused
    here: records release per session through release(), which
    owns the record lifecycle and the spend fold."""
    if any(rec.get("session") == session for rec in _mod_records.all_records(repo)):
        return {"action": "session-has-record", "session": session}
    verdict = sessions.stop_session_safe(colab, session)
    if verdict == "stop-failed":
        return {"action": "session-stop-failed", "session": session, "stop": verdict}
    clear_unattributed(repo, session)
    return {"action": "session-released", "session": session, "stop": verdict}


def detect_unattributed(colab, repo: Path, now_dt: datetime) -> dict:
    """Unattributed detection (Step 6), run by reap() each pass: a
    live session with no lease record and no caller claim is a
    stray — stamped with its first-seen time and spooled once as
    unattributed-session (the spool drains into the outbox; the
    kind is not in the UI log's closed vocabulary, so it surfaces
    in the access list, not the log). A stamped session that stops
    being a live stray — it died, or it gained a record or a claim
    — is cleared with an unattributed-cleared event. An unreadable
    live list detects nothing: detection never acts on a list it
    could not read."""
    live = sessions.live_sessions(colab, repo)
    if live is None:
        return {"detected": [], "cleared": []}
    known = {rec.get("session") for rec in _mod_records.all_records(repo)} | set(callers.load_caller_claims(repo).keys())
    stamps = load_unattributed(repo)
    live_names = {e["session"] for e in live}
    detected: list[str] = []
    cleared: list[str] = []
    for entry in live:
        name = entry["session"]
        if name in known or name in stamps:
            continue
        stamps[name] = {"first_seen": now_dt.isoformat(), "endpoint": entry["endpoint"], "shape_class": entry["shape_class"]}
        foundation.append_reaper_event(repo, {"kind": "unattributed-session", "session": name, "endpoint": entry["endpoint"], "shape_class": entry["shape_class"], "first_seen": stamps[name]["first_seen"]})
        detected.append(name)
    for name in list(stamps):
        if name not in live_names or name in known:
            del stamps[name]
            foundation.append_reaper_event(repo, {"kind": "unattributed-cleared", "session": name})
            cleared.append(name)
    if detected or cleared:
        save_unattributed(repo, stamps)
    return {"detected": detected, "cleared": cleared}
