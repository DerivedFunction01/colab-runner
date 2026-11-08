import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, records, callers

# --- Synchronous channel claims ----------------------------------------------
# Local why: a runtime admits exactly one SSH connection, and a
# synchronous colab-sh / colab-fs call holds it for the call's whole
# duration — long enough, unrecorded, to starve every other rider
# invisibly (the 2026-11-08 starvation incidents). Synchronous use
# is therefore a claim: TTL'd, caller-attributed, granted only to a
# session's sole active caller, and checked per attach. The claim
# makes "one active caller" an explicit, journaled fact; detached
# work (colab-run jobs, colab-sh --detach) never needs one and
# stays open to every caller. Design of record:
# .plans/2026-11-08-sync-channel-design.md (option 3 composite).
# Mechanics: docs/lease-workflow.md.

CLAIM_KEY = "sync_claim"
NONTERMINAL_RUN_STATES = ("starting", "running", "polling")
DEFAULT_TTL_MINUTES = 60
DEFAULT_ATTACH_WINDOW_MINUTES = 15


def _now(now_dt: datetime | None) -> datetime:
    return now_dt or datetime.now(timezone.utc)


def _parse(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        dt = foundation.parse_dt(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def sync_claim_policy(repo: Path) -> dict:
    """The sync_claim block of the lease config, resolved:
    {ttl_minutes, attach_window_minutes}. A missing block or a
    non-positive/non-numeric value falls back to the default —
    the knobs tune the window, they never disable the claim."""
    block = foundation.load_config(repo).get("sync_claim")
    if not isinstance(block, dict):
        block = {}
    out = {}
    for key, default in (("ttl_minutes", DEFAULT_TTL_MINUTES), ("attach_window_minutes", DEFAULT_ATTACH_WINDOW_MINUTES)):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            value = default
        out[key] = value
    return out


# --- Active-caller evidence ---------------------------------------------------


def _run_records(repo: Path) -> list[dict]:
    """Every runner record on disk (results/runs/*.json), read
    tolerantly — the extend guard's activity precedent
    (sync/extend_guard.py reads the same directory the same way);
    duplicated here because the guard lives outside the package
    and the claim check must not depend on its cadence."""
    out = []
    runs = repo / "results" / "runs"
    if runs.is_dir():
        for path in sorted(runs.glob("*.json")):
            rec = foundation.load_json(path, None)
            if isinstance(rec, dict):
                out.append(rec)
    return out


def active_callers(repo: Path, session: str, now_dt: datetime | None = None) -> dict:
    """The distinct callers with live evidence on one session:
    {caller: [evidence, ...]}. Evidence is exactly three kinds —
    a job registered on the lease record, a non-terminal run
    record naming the caller, or a channel attach in the
    caller-claims journal inside the freshness window. Anything
    older or weaker is not activity; a caller with no live
    evidence is not active, however recent their lease is."""
    now = _now(now_dt)
    window_s = sync_claim_policy(repo)["attach_window_minutes"] * 60
    evidence: dict[str, list[dict]] = {}

    def add(caller, item):
        if caller:
            evidence.setdefault(caller, []).append(item)

    runs = _run_records(repo)
    by_run = {r.get("run_id"): r for r in runs if r.get("run_id")}
    rec = records.load_record(repo, session)
    if rec is not None:
        for run_id in rec.get("jobs") or []:
            run = by_run.get(run_id) or {}
            add(run.get("caller") or rec.get("caller"), {"kind": "job", "run_id": run_id})
    for run in runs:
        if run.get("session") == session and run.get("state") in NONTERMINAL_RUN_STATES:
            add(run.get("caller"), {"kind": "run", "run_id": run.get("run_id"), "state": run.get("state")})
    # Channel attaches: the freshest attach per caller inside the
    # window is the evidence; older lines are history.
    freshest: dict[str, dict] = {}
    try:
        lines = callers.caller_claims_path(repo).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("session") != session or not entry.get("caller"):
            continue
        ts = _parse(entry.get("ts"))
        if ts is None or ts > now or (now - ts).total_seconds() > window_s:
            continue
        prev = freshest.get(entry["caller"])
        if prev is None or ts >= prev["_ts"]:
            freshest[entry["caller"]] = {"kind": "attach", "seam": entry.get("seam"), "ts": entry.get("ts"), "_ts": ts}
    for caller, item in freshest.items():
        item = {k: v for k, v in item.items() if k != "_ts"}
        add(caller, item)
    return evidence


# --- Claim storage --------------------------------------------------------------
# One claim per session, stored in exactly one place: on the lease
# record when a record exists (written with save_record's staged
# sibling + rename), else a standalone var file written the same
# way. A torn or denied write therefore never leaves a half-claim:
# the sibling is discarded and the standing claim survives.


def _claim_file(repo: Path, session: str) -> Path:
    return foundation.var_dir(repo) / f"sync-claim-{session}.json"


def _write_claim_file(repo: Path, session: str, claim: dict) -> None:
    p = _claim_file(repo, session)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps({"session": session, **claim}, indent=2))
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _stored_claim(repo: Path, session: str) -> dict | None:
    """The claim as stored (no lapse evaluation): the record's
    field wins when a record carries one; the standalone file is
    the record-less session's home."""
    rec = records.load_record(repo, session)
    if rec is not None and isinstance(rec.get(CLAIM_KEY), dict):
        return rec[CLAIM_KEY]
    data = foundation.load_json(_claim_file(repo, session), None)
    if isinstance(data, dict) and isinstance(data.get("caller"), str):
        return {k: data.get(k) for k in ("caller", "claimed_at", "expires_at", "last_attach_at")}
    return None


def _store_claim(repo: Path, session: str, claim: dict) -> None:
    rec = records.load_record(repo, session)
    if rec is not None:
        rec[CLAIM_KEY] = claim
        records.save_record(repo, rec)
        # The record is the claim's one home now; a standalone
        # file from the session's record-less days is superseded.
        _claim_file(repo, session).unlink(missing_ok=True)
    else:
        _write_claim_file(repo, session, claim)


def _clear_claim(repo: Path, session: str) -> None:
    rec = records.load_record(repo, session)
    if rec is not None and CLAIM_KEY in rec:
        rec.pop(CLAIM_KEY, None)
        records.save_record(repo, rec)
    _claim_file(repo, session).unlink(missing_ok=True)


def read_sync_claim(repo: Path, session: str, now_dt: datetime | None = None) -> dict | None:
    """The session's live claim, or None. A claim is live until
    its TTL expires AND while its holder is not quiet: a holder
    whose last attach is older than the freshness window and who
    shows no other live evidence has lapsed — the stored claim
    reads as absent (a later grant overwrites it), so a dead
    claimant never squats past their own silence."""
    now = _now(now_dt)
    claim = _stored_claim(repo, session)
    if claim is None:
        return None
    expires = _parse(claim.get("expires_at"))
    if expires is None or now >= expires:
        return None
    last_attach = _parse(claim.get("last_attach_at"))
    window_s = sync_claim_policy(repo)["attach_window_minutes"] * 60
    fresh = last_attach is not None and (now - last_attach).total_seconds() <= window_s
    if not fresh and claim.get("caller") not in active_callers(repo, session, now):
        return None
    return {"session": session, **claim}


def _grant(repo: Path, session: str, caller: str, now: datetime) -> dict:
    policy = sync_claim_policy(repo)
    claim = {
        "caller": caller,
        "claimed_at": _iso(now),
        "expires_at": _iso(now + timedelta(minutes=policy["ttl_minutes"])),
        "last_attach_at": _iso(now),
    }
    _store_claim(repo, session, claim)
    # The grant is journaled in the caller-claims journal, the
    # codebase's attach-evidence record: a sync-claim line is the
    # holder's claim made visible to every other rider.
    callers.append_caller_claim(repo, session, caller, "sync-claim")
    return {"session": session, **claim}


def _refresh(repo: Path, session: str, claim: dict, now: datetime) -> dict:
    claim = {**claim, "last_attach_at": _iso(now)}
    stored = {k: claim.get(k) for k in ("caller", "claimed_at", "expires_at", "last_attach_at")}
    _store_claim(repo, session, stored)
    return {"session": session, **stored}


# --- The gate -------------------------------------------------------------------


def sync_rights(repo: Path, session: str, caller: str | None, now_dt: datetime | None = None) -> dict:
    """May `caller` use the synchronous channel to `session` now?
    The verdict: {"allowed": True, "claim", "auto_claimed"} or
    {"allowed": False, "blocked_by": "claim"|"activity", "holder",
    "evidence", "expires_at"}. Rules, in order:

    - A live claim held by another caller refuses (blocked_by
      claim) — the holder is named, with the claim's expiry.
    - Any other caller with live evidence refuses (blocked_by
      activity) — synchronous is off while multiple threads are
      active, for the claimant too; their claim is NOT revoked,
      it resumes when the session is single-occupancy again.
    - Otherwise the caller is the sole active caller: an
      unclaimed session auto-claims to them (journaled), a claim
      of theirs refreshes its attach stamp.
    - An unattributed caller (None) holds no claim and is checked
      against claims and activity like anyone else; in a quiet,
      unclaimed session they pass as today, unattributed.
    """
    now = _now(now_dt)
    claim = read_sync_claim(repo, session, now)
    active = active_callers(repo, session, now)
    others = {c: ev for c, ev in active.items() if c != caller}
    if claim is not None and claim.get("caller") != caller:
        return {
            "allowed": False,
            "action": "sync-refused-claim",
            "blocked_by": "claim",
            "holder": claim.get("caller"),
            "evidence": [{"kind": "claim", "claimed_at": claim.get("claimed_at"), "expires_at": claim.get("expires_at")}],
            "expires_at": claim.get("expires_at"),
            "claim": claim,
        }
    if others:
        holder = sorted(others)[0]
        return {
            "allowed": False,
            "action": "sync-refused-activity",
            "blocked_by": "activity",
            "holder": holder,
            "others": sorted(others),
            "evidence": others[holder],
            "expires_at": claim.get("expires_at") if claim else None,
            "claim": claim,
        }
    auto = False
    if caller is not None and claim is None:
        claim = _grant(repo, session, caller, now)
        auto = True
    elif claim is not None:
        claim = _refresh(repo, session, claim, now)
    return {"allowed": True, "action": "sync-allowed", "claim": claim, "auto_claimed": auto}


def claim_sync(repo: Path, session: str, caller: str | None, now_dt: datetime | None = None) -> dict:
    """The explicit claim (bin/colab-lease claim-sync): the gate's
    verdict as a lifecycle result — sync-claimed, or a refusal
    naming what blocks. Claiming is the gate run as an action:
    an auto-claim on attach and an explicit claim are the same
    grant."""
    if caller is None:
        return {"action": "sync-claim-unattributed", "session": session}
    verdict = sync_rights(repo, session, caller, now_dt)
    if verdict["allowed"]:
        return {"action": "sync-claimed", "session": session, "caller": caller, "claim": verdict["claim"], "auto_claimed": verdict["auto_claimed"]}
    return {"action": f"sync-claim-refused-{verdict['blocked_by']}", "session": session, "caller": caller, **{k: verdict[k] for k in ("blocked_by", "holder", "evidence", "expires_at")}}


def release_sync_claim(repo: Path, session: str, caller: str | None = None, now_dt: datetime | None = None) -> dict:
    """Release the session's claim. A presented caller who is not
    the holder is refused — a claim is its holder's to end. No
    presented caller is the operator's release (the panel's
    actor), which always clears. Releasing frees the session
    immediately; the next sole-active attach re-claims."""
    stored = _stored_claim(repo, session)
    if stored is None:
        return {"action": "sync-no-claim", "session": session}
    holder = stored.get("caller")
    if caller is not None and caller != holder:
        return {"action": "sync-release-refused", "session": session, "holder": holder}
    _clear_claim(repo, session)
    callers.append_caller_claim(repo, session, holder, "sync-claim-release")
    return {"action": "sync-released", "session": session, "caller": holder}
