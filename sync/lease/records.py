import importlib.util as _ilu
import json
import os
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


def save_record(repo: Path, rec: dict) -> None:
    """Write one lease's record at its session path. Any other
    file holding a record for the same session is removed, so a
    stale copy can never resurrect the lease's old shape."""
    session = rec["session"]
    p = session_record_path(repo, session)
    p.parent.mkdir(parents=True, exist_ok=True)
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


def delete_record(repo: Path, session: str) -> None:
    """Remove a lease's record, wherever it lives — for the same
    reason save cleans up: no stale copy may resurrect a released
    lease."""
    for path in _record_files(repo):
        held = _valid_record(foundation.load_json(path, None))
        if held is not None and held.get("session") == session:
            path.unlink(missing_ok=True)
    # Session liveness: the derived scheduler entry dies with
    # the record, in the same call.
    try:
        liveness_entry.remove_for_session(repo, session)
    except Exception:
        pass
