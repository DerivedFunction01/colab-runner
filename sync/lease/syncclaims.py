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
NONTERMINAL_DETACHED_STATES = ("running",)
DEFAULT_TTL_MINUTES = 60
DEFAULT_ATTACH_WINDOW_MINUTES = 15
DEFAULT_MAX_SYNC_SECONDS = 300

# The journal evidence read is a bounded tail, not the whole file:
# evidence only ever names attaches inside the freshness window,
# and a journal line is ~150 bytes, so 256 KiB of tail is on the
# order of a thousand attaches — days of burst traffic, far beyond
# anything the window can reach. The bound exists so the gate's
# hot path stops paying for the journal's whole history (E15).
JOURNAL_TAIL_BYTES = 256 * 1024


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
    {ttl_minutes, attach_window_minutes, max_sync_seconds}. A
    missing block or a non-positive/non-numeric value falls back
    to the default — the knobs tune the window, they never
    disable the claim."""
    block = foundation.load_config(repo).get("sync_claim")
    if not isinstance(block, dict):
        block = {}
    out = {}
    for key, default in (("ttl_minutes", DEFAULT_TTL_MINUTES), ("attach_window_minutes", DEFAULT_ATTACH_WINDOW_MINUTES), ("max_sync_seconds", DEFAULT_MAX_SYNC_SECONDS)):
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


# --- Detached shell records -----------------------------------------------------
# colab-sh --detach launches register locally (E8): one record per
# launch at results/detached/<id>.json, in the run-record
# vocabulary — {id, session, caller, state, exit_code, started_at,
# finished_at}. Written at launch, marked terminal by the poll
# that observes the end. This is the record shape the orphan
# census and the claim gate both read; it is a contract, kept
# exact.


def detached_record_path(repo: Path, did: str) -> Path:
    return repo / "results" / "detached" / f"{did}.json"


def write_detached_record(repo: Path, rec: dict) -> None:
    """Persist one detached record (staged sibling + rename, the
    records discipline: a denied write leaves the standing record
    intact and surfaces instead)."""
    p = detached_record_path(repo, rec["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(rec, indent=2))
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_detached_record(repo: Path, did: str) -> dict | None:
    data = foundation.load_json(detached_record_path(repo, did), None)
    return data if isinstance(data, dict) else None


def _detached_records(repo: Path) -> list[dict]:
    """Every detached record on disk, read tolerantly — the
    _run_records discipline over results/detached/."""
    out = []
    detached = repo / "results" / "detached"
    if detached.is_dir():
        for path in sorted(detached.glob("*.json")):
            rec = foundation.load_json(path, None)
            if isinstance(rec, dict):
                out.append(rec)
    return out


def mark_detached_terminal(repo: Path, did: str, state: str, exit_code, now_dt: datetime | None = None) -> dict | None:
    """Mark a detached record terminal (state "complete"/"failed",
    with the job's exit code and finish stamp) as its poll
    observes the end. Returns the updated record, or None when no
    local record exists — a launch from another machine is not
    this machine's to mark."""
    rec = read_detached_record(repo, did)
    if rec is None:
        return None
    rec["state"] = state
    rec["exit_code"] = exit_code
    rec["finished_at"] = _iso(_now(now_dt))
    write_detached_record(repo, rec)
    return rec


def _journal_tail_lines(path: Path) -> list[str]:
    """The caller-claims journal's tail lines: the last
    JOURNAL_TAIL_BYTES of the file, with the first (possibly
    partial) line dropped when the read started mid-file. See
    the bound's note at the constant."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > JOURNAL_TAIL_BYTES:
                fh.seek(size - JOURNAL_TAIL_BYTES)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > JOURNAL_TAIL_BYTES and lines:
        lines = lines[1:]
    return lines


def active_callers(repo: Path, session: str, now_dt: datetime | None = None) -> dict:
    """The distinct callers with live evidence on one session:
    {caller: [evidence, ...]}. Evidence kinds — a job registered
    on the lease record, a non-terminal run record naming the
    caller, a running detached record naming the caller, a
    channel attach in the caller-claims journal inside the
    freshness window, or an unexpired long hold on the session's
    claim. Anything older or weaker is not activity; a caller
    with no live evidence is not active, however recent their
    lease is."""
    now = _now(now_dt)
    window_s = sync_claim_policy(repo)["attach_window_minutes"] * 60
    evidence: dict[str, list[dict]] = {}

    def add(caller, item):
        if caller:
            evidence.setdefault(caller, []).append(item)

    runs = _run_records(repo)
    by_run = {r.get("run_id"): r for r in runs if r.get("run_id")}
    detached = _detached_records(repo)
    by_detached = {d.get("id"): d for d in detached if d.get("id")}
    rec = records.load_record(repo, session)
    seen_detached: set = set()
    if rec is not None:
        for job_id in rec.get("jobs") or []:
            if isinstance(job_id, str) and job_id.startswith("detached:"):
                det_id = job_id.split(":", 1)[1]
                det = by_detached.get(det_id) or {}
                if det.get("state", "running") in NONTERMINAL_DETACHED_STATES:
                    add(det.get("caller") or rec.get("caller"), {"kind": "detached", "id": det_id})
                    seen_detached.add(det_id)
                continue
            run = by_run.get(job_id) or {}
            add(run.get("caller") or rec.get("caller"), {"kind": "job", "run_id": job_id})
    for run in runs:
        if run.get("session") == session and run.get("state") in NONTERMINAL_RUN_STATES:
            add(run.get("caller"), {"kind": "run", "run_id": run.get("run_id"), "state": run.get("state")})
    for det in detached:
        if det.get("id") in seen_detached:
            continue
        if det.get("session") == session and det.get("state") in NONTERMINAL_DETACHED_STATES:
            add(det.get("caller"), {"kind": "detached", "id": det.get("id")})
    # An unexpired long hold is its holder's live evidence until
    # its stated end (the decision-1 amendment): the holder makes
    # no attaches during a hold, so without this the claim would
    # lapse holder-quiet mid-hold and admit a second sync caller
    # into the collision the hold journal exists to prevent.
    stored = _stored_claim(repo, session)
    if stored is not None:
        hold_until = _parse(stored.get("long_hold_until"))
        expires = _parse(stored.get("expires_at"))
        if hold_until is not None and now < hold_until and expires is not None and now < expires:
            add(stored.get("caller"), {"kind": "long-hold", "until": stored.get("long_hold_until")})
    # Channel attaches: the freshest attach per caller inside the
    # window is the evidence; older lines are history. The read is
    # the journal's bounded tail (E15) — the window it must cover
    # is minutes, the tail is days.
    freshest: dict[str, dict] = {}
    for line in _journal_tail_lines(callers.caller_claims_path(repo)):
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


_CLAIM_FIELDS = ("caller", "claimed_at", "expires_at", "last_attach_at", "long_hold_until")


def _stored_claim(repo: Path, session: str) -> dict | None:
    """The claim as stored (no lapse evaluation): the record's
    field wins when a record carries one; the standalone file is
    the record-less session's home."""
    rec = records.load_record(repo, session)
    if rec is not None and isinstance(rec.get(CLAIM_KEY), dict):
        return rec[CLAIM_KEY]
    data = foundation.load_json(_claim_file(repo, session), None)
    if isinstance(data, dict) and isinstance(data.get("caller"), str):
        return {k: data.get(k) for k in _CLAIM_FIELDS}
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
    claimant never squats past their own silence. One exception
    (the decision-1 amendment): a holder inside a journaled long
    hold is never quiet — the hold is live evidence until its
    stated end, so the claim cannot lapse mid-hold."""
    now = _now(now_dt)
    claim = _stored_claim(repo, session)
    if claim is None:
        return None
    expires = _parse(claim.get("expires_at"))
    if expires is None or now >= expires:
        return None
    hold_until = _parse(claim.get("long_hold_until"))
    if hold_until is not None and now < hold_until:
        return {"session": session, **claim}
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
        "long_hold_until": None,
    }
    _store_claim(repo, session, claim)
    # The grant is journaled in the caller-claims journal, the
    # codebase's attach-evidence record: a sync-claim line is the
    # holder's claim made visible to every other rider.
    callers.append_caller_claim(repo, session, caller, "sync-claim")
    return {"session": session, **claim}


def _refresh(repo: Path, session: str, claim: dict, now: datetime) -> dict:
    claim = {**claim, "last_attach_at": _iso(now)}
    stored = {k: claim.get(k) for k in _CLAIM_FIELDS}
    _store_claim(repo, session, stored)
    return {"session": session, **stored}


def note_long_hold(repo: Path, session: str, caller: str, bound_seconds: float, now_dt: datetime | None = None) -> dict:
    """Record a --long hold on the caller's own live claim (E3):
    the claim gains long_hold_until = now + the hold's bound, its
    expiry extends to the hold's end when that is later (the
    claim must live at least as long as the hold it evidences),
    and the attach stamp refreshes. The hold is journaled in the
    caller-claims journal. Strict, like a grant: a hold that
    cannot be stored is not recorded — it raises — because an
    unrecorded hold is exactly the invisible collision the cap
    exists to prevent."""
    now = _now(now_dt)
    claim = read_sync_claim(repo, session, now)
    if claim is None or claim.get("caller") != caller:
        raise ValueError(f"no live sync claim by {caller!r} on session {session!r} to hold")
    hold_until = now + timedelta(seconds=float(bound_seconds))
    stored = {k: claim.get(k) for k in _CLAIM_FIELDS}
    stored["long_hold_until"] = _iso(hold_until)
    stored["last_attach_at"] = _iso(now)
    expires = _parse(stored.get("expires_at"))
    if expires is None or hold_until > expires:
        stored["expires_at"] = _iso(hold_until)
    _store_claim(repo, session, stored)
    callers.append_caller_claim(repo, session, caller, "sync-long-hold")
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
    refresh_degraded = False
    if caller is not None and claim is None:
        claim = _grant(repo, session, caller, now)
        auto = True
    elif claim is not None:
        # A refresh is best-effort (E12, decision 8): the claim
        # already stands and was just read live; a failed refresh
        # write degrades to the stored claim and journals the
        # degradation — it never refuses a valid holder for a
        # local write condition. A grant, above, stays strict:
        # an unstored claim is not granted, and its write failure
        # propagates to the wrapper's fail-closed verdict.
        try:
            claim = _refresh(repo, session, claim, now)
        except Exception:  # noqa: BLE001
            refresh_degraded = True
            try:
                callers.append_caller_claim(repo, session, caller, "sync-claim-refresh-degraded")
            except Exception:  # noqa: BLE001
                pass
    return {"allowed": True, "action": "sync-allowed", "claim": claim, "auto_claimed": auto, "refresh_degraded": refresh_degraded}


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
