import importlib.util as _ilu
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from . import foundation

_naming_spec = _ilu.spec_from_file_location("colab_naming", Path(__file__).resolve().parent.parent / "naming.py")
naming = _ilu.module_from_spec(_naming_spec)
_naming_spec.loader.exec_module(naming)

_liveness_spec = _ilu.spec_from_file_location("colab_liveness_entry", Path(__file__).resolve().parent.parent / "liveness_entry.py")
liveness_entry = _ilu.module_from_spec(_liveness_spec)
_liveness_spec.loader.exec_module(liveness_entry)


def session_record_path(repo: Path, session: str) -> Path:
    return foundation.var_dir(repo) / f"lease-{session}.json"


def session_evidence_path(repo: Path, session: str) -> Path:
    return foundation.var_dir(repo) / f"session-evidence-{session}.json"


def write_session_evidence(repo: Path, rec: dict) -> None:
    """Project the record's deadline-bearing fields into the
    session-evidence file (E2): {deadline, acquired_at, shape,
    spec, caller} — a verbatim copy of the same record, written
    with the record's own staged-sibling discipline. The
    projection exists so orphan derivation keeps an evidenced
    deadline when the record itself is lost; save_record
    refreshes it on every save, so no lifecycle path that
    creates or moves a deadline — acquire, extend, shorten,
    renewal, adoption — can miss it, and it never depends on
    the renewal kit having installed."""
    session = rec.get("session")
    if not session:
        return
    evidence = {
        "deadline": rec.get("deadline"),
        "acquired_at": rec.get("acquired_at"),
        "shape": rec.get("shape"),
        "spec": rec.get("shape_spec"),
        "caller": rec.get("caller"),
    }
    p = session_evidence_path(repo, session)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(evidence, indent=2))
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _valid_record(data) -> dict | None:
    return data if isinstance(data, dict) and data.get("session") else None


def _record_files(repo: Path) -> list[Path]:
    """Every file that may hold a lease record: the per-session
    files under var/ (the name a file carries says nothing; its
    record's session is the key)."""
    var = foundation.var_dir(repo)
    files = []
    if var.exists():
        for path in sorted(var.glob("lease-*.json")):
            if path.name in ("lease-spend.json", "lease-request.json"):
                continue
            files.append(path)
    return files


def _filename_session(name: str) -> str:
    """The session a record file's NAME claims
    (lease-<session>.json): the only attribution a file whose
    contents cannot be read still carries."""
    stem = name[len("lease-"):] if name.startswith("lease-") else name
    return stem[: -len(".json")] if stem.endswith(".json") else stem


def _record_from_bytes(path: Path) -> dict | None:
    """The record a file holds, or None when the file cannot
    yield one — unreadable, unparseable, or not a record."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _valid_record(data)


def torn_records(repo: Path) -> list[dict]:
    """Every lease-record file whose contents cannot yield a
    record (E1): served as a finding instead of vanishing from
    every reader the way an unparseable file does today. A
    torn file is attributed by the only thing it still
    carries — its filename. The handover file is a different
    class living under the same prefix (a live handover
    marker, never a record) and is excluded by name. This is
    a pure read: nothing here renames anything — quarantine
    is a writer's act, below."""
    out = []
    for path in _record_files(repo):
        if path.name == "lease-handover.json":
            continue
        if _record_from_bytes(path) is not None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append({"session": _filename_session(path.name), "file": path.name, "bytes": size})
    return out


def quarantine_torn(repo: Path, session: str | None = None, now_dt: datetime | None = None) -> list[dict]:
    """Rename torn record files out of the record namespace —
    <name>.torn-<ts>, bytes preserved — so the writers that
    need the path (save_record's target, the adoption path)
    never reason about a torn file in place. With a session,
    only that session's file moves. The rename is the whole
    remedy: the torn bytes are evidence and are never deleted
    here. Returns the quarantined entries, each with its new
    name under "quarantined_as"."""
    now_dt = now_dt or datetime.now(timezone.utc)
    stamp = now_dt.strftime("%Y%m%dT%H%M%SZ")
    out = []
    for entry in torn_records(repo):
        if session is not None and entry["session"] != session:
            continue
        src = foundation.var_dir(repo) / entry["file"]
        dst = src.with_name(f"{entry['file']}.torn-{stamp}")
        if dst.exists():
            dst = src.with_name(f"{entry['file']}.torn-{stamp}-{os.getpid()}")
        os.replace(src, dst)
        out.append({**entry, "quarantined_as": dst.name})
    return out


def all_records(repo: Path) -> list[dict]:
    """Every lease record on disk, exactly as written. One record
    per session: when two files hold the same session, the
    session-keyed file wins. Order: acquisition order."""
    by_session: dict[str, dict] = {}
    canonical: set[str] = set()
    for path in _record_files(repo):
        rec = _valid_record(foundation.load_json(path, None))
        if rec is None:
            continue
        session = rec["session"]
        if path.name == f"lease-{session}.json":
            by_session[session] = rec
            canonical.add(session)
        elif session not in canonical:
            by_session.setdefault(session, rec)
    recs = list(by_session.values())
    recs.sort(key=lambda r: (r.get("acquired_at") or "", r.get("session") or ""))
    return recs


def load_record(repo: Path, session: str | None = None) -> dict | None:
    """With a session: that session's record. Without: the most
    recently used record (the lease a job call most plausibly
    belongs to), or None when no lease is on disk."""
    recs = all_records(repo)
    if session is not None:
        return next((r for r in recs if r.get("session") == session), None)
    if not recs:
        return None
    return max(recs, key=lambda r: (r.get("last_used_at") or "", r.get("acquired_at") or ""))


# --- Pruned-jobs projection ----------------------------------------------------
# Local why: job registrations were append-only — touch_job adds,
# finish_job removes, and a registration whose backing record
# ended without a finish (a killed runner, a detached call nobody
# polled to its end) stayed on the record forever: one live
# record carried 63 registrations, 23 of them backed by terminal
# or missing records, and every consumer that read the raw list
# (the state document, the reap's idle test, the sync-claim
# evidence) inherited the lie. The projection is the one truth:
# a registration counts only while its backing run record
# (results/runs/<id>.json) or detached record
# (results/detached/<id>.json) exists and is non-terminal.
# The state vocabularies are the consumers' own: the runner's
# terminal states for runs, and a detached record is live only
# in its running state with no finish stamp.

RUN_NONTERMINAL_STATES = ("starting", "running", "polling")
DETACHED_LIVE_STATES = ("running",)


def run_backing(repo: Path, run_id: str) -> tuple[dict | None, bool]:
    """One run record by id: (record, file_exists). The record
    is None when the file cannot yield one — missing, or torn
    mid-write; the flag separates the two, because a torn
    record is unknown, never dead."""
    path = repo / "results" / "runs" / f"{run_id}.json"
    rec = foundation.load_json(path, None)
    return (rec if isinstance(rec, dict) else None), path.exists()


def detached_backing(repo: Path, det_id: str) -> tuple[dict | None, bool]:
    """One detached record by id (the part after `detached:`),
    under the same contract as run_backing."""
    path = repo / "results" / "detached" / f"{det_id}.json"
    rec = foundation.load_json(path, None)
    return (rec if isinstance(rec, dict) else None), path.exists()


def detached_activity(repo: Path, session: str) -> str | None:
    """The newest activity stamp among the session's detached
    records (results/detached/): a record's activity is its
    finished_at when it has one, else its started_at. None
    when the session has no detached records. This is the
    evidence the served last-use line folds in (state.status):
    detached shell work is session use even though no lease
    path stamps the lease record for it — the panel read cozy
    as unused since 15:55Z while a detached probe ran there
    from 15:58Z (2026-11-11)."""
    latest = None
    detached = repo / "results" / "detached"
    if detached.is_dir():
        for path in sorted(detached.glob("*.json")):
            det = foundation.load_json(path, None)
            if not isinstance(det, dict) or det.get("session") != session:
                continue
            stamp = det.get("finished_at") or det.get("started_at")
            if isinstance(stamp, str) and (latest is None or stamp > latest):
                latest = stamp
    return latest


def job_entry(repo: Path, job_id: str) -> dict | None:
    """One registration resolved against its backing record:
    {id, kind, state, started_at} while the backing record
    exists and is non-terminal, else None (pruned). A backing
    file that exists but cannot be read is torn — unknown,
    never dead — and the registration stands with state
    "unknown". A registration with no backing-record kind (a
    bare name that is neither a run id nor `detached:<id>`)
    resolves against the runs directory like any run id."""
    if not isinstance(job_id, str):
        return None
    if job_id.startswith("detached:"):
        det_id = job_id.split(":", 1)[1]
        det, exists = detached_backing(repo, det_id)
        if det is None:
            return {"id": job_id, "kind": "detached", "state": "unknown", "started_at": None} if exists else None
        if det.get("finished_at"):
            return None
        state = det.get("state", "running")
        if state not in DETACHED_LIVE_STATES:
            return None
        return {"id": job_id, "kind": "detached", "state": state, "started_at": det.get("started_at")}
    run, exists = run_backing(repo, job_id)
    if run is None:
        return {"id": job_id, "kind": "run", "state": "unknown", "started_at": None} if exists else None
    state = run.get("state")
    if state not in RUN_NONTERMINAL_STATES:
        return None
    return {"id": job_id, "kind": "run", "state": state, "started_at": run.get("started_at")}


def live_jobs(repo: Path, rec: dict) -> list[dict]:
    """The record's pruned jobs projection: the kinded entries
    of the registrations that are still backed by a live
    record, in registration order. Every consumer of a record's
    jobs — the state document, the census, adoption seeding,
    the reap's idle test — reads this, never the raw list."""
    out = []
    for job_id in rec.get("jobs") or []:
        entry = job_entry(repo, job_id)
        if entry is not None:
            out.append(entry)
    return out


def save_record(repo: Path, rec: dict) -> None:
    """Write one lease's record at its session path. Any other
    file holding a record for the same session is removed, so a
    stale copy can never resurrect the lease's old shape."""
    # The jobs list persists pruned (the projection above):
    # registrations whose backing records are gone or terminal
    # are dropped at the write, so the store self-heals with no
    # migration. The caller's dict is pruned too — a save is the
    # moment the record's jobs become the projection everywhere,
    # including in the hands of the code that just saved it.
    if isinstance(rec.get("jobs"), list):
        rec["jobs"] = [entry["id"] for entry in live_jobs(repo, rec)]
    session = rec["session"]
    p = session_record_path(repo, session)
    p.parent.mkdir(parents=True, exist_ok=True)
    # A torn file at this record's own path is quarantined
    # first (E1): the staged replace below would otherwise
    # destroy the torn bytes, which are the only evidence the
    # damage happened. A failed quarantine never fails the
    # save — landing the record is the critical act.
    try:
        quarantine_torn(repo, session=session)
    except OSError:
        pass
    # Staged sibling + rename, never an in-place write: a writer
    # whose write is denied mid-save (the sandbox write-denial
    # flap truncates in-place targets, then refuses) must leave
    # the standing record intact — a torn sibling is discarded
    # and the failure surfaces to the caller instead.
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(rec, indent=2))
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for other in _record_files(repo):
        if other == p:
            continue
        held = _valid_record(foundation.load_json(other, None))
        if held is not None and held.get("session") == session:
            other.unlink(missing_ok=True)
    # Session liveness: a saved live record carries its derived
    # scheduler entry (sync/liveness_entry.py). The sync never
    # fails the save — drift is repaired by the reaper janitor.
    try:
        liveness_entry.sync_for_record(repo, rec)
    except Exception:
        pass
    # Session evidence (E2): the record's deadline projection,
    # refreshed on every save so every lifecycle write that
    # creates or moves a deadline leaves it current. Also
    # best-effort, for the same reason as the liveness sync:
    # the record itself is the primary copy.
    try:
        write_session_evidence(repo, rec)
    except Exception:
        pass


def delete_record(repo: Path, session: str) -> None:
    """Remove a lease's record, wherever it lives — for the same
    reason save cleans up: no stale copy may resurrect a released
    lease."""
    for path in _record_files(repo):
        held = _valid_record(foundation.load_json(path, None))
        if held is not None and held.get("session") == session:
            path.unlink(missing_ok=True)
    # The session-evidence projection (E2) dies with the
    # record it projects: a released lease leaves no deadline
    # evidence behind for a session that no longer exists.
    session_evidence_path(repo, session).unlink(missing_ok=True)
    # Session liveness: the derived scheduler entry dies with
    # the record, in the same call.
    try:
        liveness_entry.remove_for_session(repo, session)
    except Exception:
        pass
