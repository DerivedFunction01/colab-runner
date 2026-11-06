"""Machine-written scheduler entries for live leases.

Session liveness plan (.plans/2026-11-06-session-liveness-
schedule.md): every live lease carries a derived schedule in
the scheduler server's registry — jobs/colab-liveness-
<session>.yaml, a scripted entry running bin/colab-liveness
every 15 minutes. The lease record is the record of truth and
the entry is its derived schedule, exactly the relationship
the workbench harness keeps between a todo and its
wb-progress entry.

The writer seams are the lease core's own record writers:
save_record arms (or refreshes) the entry, delete_record
removes it, so coverage holds by construction at every birth
(acquire, adoption, renewal swap, reconcile) and every death
(release, reap-terminal, clear). A sync failure never fails
the lease operation — reconcile (driven by the reaper hook's
janitor duty and by bin/colab-liveness --sync) repairs any
drift, and a leaked entry self-deletes on the checker's first
run that finds no live record.

Jobs-dir resolution: $COLAB_LIVENESS_JOBS_DIR when set (the
deliberate override — tests, tooling). Otherwise the real
registry is used only when the repo being operated on is
this module's own installed repo and the workspace scheduler
sibling exists under the process HOME: test suites operate
on scratch repos and scratch HOMEs, so those guards keep a
suite run from writing entries for fixture sessions into
the live registry (the test-isolation rule, learned from
the 2026-11-05 binding pollution). One more guard rides
here: under pytest (PYTEST_CURRENT_TEST set — in-process
tests, and the subprocesses they spawn from os.environ) the
default resolution always refuses, because in-process tests
drive the real repo against the fake CLI. No scheduler on
the machine means the sync is a silent no-op, as with the
workbench harness's seam.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENTRY_PREFIX = "colab-liveness-"
_MODULE_REPO = Path(__file__).resolve().parent.parent


def jobs_dir(repo: Path) -> Path | None:
    env = os.environ.get("COLAB_LIVENESS_JOBS_DIR")
    if env:
        return Path(env)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if Path(repo).resolve() != _MODULE_REPO:
        return None
    sibling = Path.home() / "workspace" / "scheduler" / "jobs"
    return sibling if sibling.is_dir() else None


def entry_id(session: str) -> str:
    return ENTRY_PREFIX + session


def entry_path(repo: Path, session: str) -> Path | None:
    jd = jobs_dir(repo)
    return (jd / f"{entry_id(session)}.yaml") if jd else None


def render_entry(session: str) -> str:
    eid = entry_id(session)
    return f"""id: {eid}
title: 'Liveness check - Colab session {session}'
kind: scripted
status: active
cadence:
  type: interval
  every: 15m
owner: none
owner_reason: 'Machine-written by the colab-runner lease core for one live lease; the lease record is the record of truth and this entry is its derived schedule.'
run:
  argv:
    - python3
    - ${{WS}}/colab-runner/bin/colab-liveness
    - {session}
timeout_secs: 120
report:
  when: on_failure
  ok_exit_codes:
    - 0
escalate_body: bodies/colab-liveness.md
state_paths:
  - ${{WS}}/colab-runner/var/liveness-{session}.json
classification: 'fixed command (bin/colab-liveness <session>); local file reads only, never dials the session; exit 0 healthy, cleared, or gone (self-disarmed), exit 3 with one LIVENESS line per finding it must speak; when it speaks is fully determined by its state file and the exit code, no content judgment'
lifetime: until_complete
self_removal: 'the lease core deletes this entry in the same call that deletes the lease record; the checker deletes it itself on the first run that finds no live record, and the reaper hook reconciliation retires any orphan'
migrated: false
"""


def sync_for_record(repo: Path, rec: dict) -> str:
    """Arm (or refresh) the entry for a live record. Returns a
    status word; never raises."""
    session = (rec or {}).get("session")
    if not session:
        return "no-session"
    try:
        path = entry_path(repo, session)
        if path is None:
            return "unavailable"
        text = render_entry(session)
        if path.exists() and path.read_text() == text:
            return "present"
        path.write_text(text)
        return "written"
    except OSError as exc:
        return f"error: {exc}"


def remove_for_session(repo: Path, session: str) -> str:
    """Disarm: delete the entry and the checker's state file.
    Never raises."""
    try:
        path = entry_path(repo, session)
        if path is not None:
            path.unlink(missing_ok=True)
        state = var_dir(repo) / f"liveness-{session}.json"
        state.unlink(missing_ok=True)
        return "removed"
    except OSError as exc:
        return f"error: {exc}"


def live_sessions(records: list) -> set:
    return {r.get("session") for r in records if isinstance(r, dict) and r.get("session")}


def reconcile(repo: Path, records: list) -> dict:
    """Bring the registry in line with the live records: arm
    any live session missing an entry, retire any entry whose
    session holds no record. Returns what changed."""
    jd = jobs_dir(repo)
    if jd is None:
        return {"armed": [], "removed": [], "unavailable": True}
    live = live_sessions(records)
    by_session = {r["session"]: r for r in records if isinstance(r, dict) and r.get("session")}
    armed, removed = [], []
    for path in sorted(jd.glob(f"{ENTRY_PREFIX}*.yaml")):
        session = path.name[len(ENTRY_PREFIX):-len(".yaml")]
        if session not in live:
            remove_for_session(repo, session)
            removed.append(session)
    for session in sorted(live):
        if sync_for_record(repo, by_session[session]) == "written":
            armed.append(session)
    return {"armed": armed, "removed": removed}


def var_dir(repo: Path) -> Path:
    """The repo's var dir, honoring the same COLAB_RUNNER_VAR
    seam the lease core uses."""
    return Path(os.environ.get("COLAB_RUNNER_VAR", Path(repo) / "var"))


def load_records(repo: Path) -> list:
    """The repo's lease records, read plainly (this module is
    imported by the lease core, so it cannot import it back).
    Mirrors the core's record-file rules: lease-*.json plus
    the legacy lease.json, minus the spend/request ledgers,
    keeping only bodies that carry a session."""
    var = var_dir(repo)
    files = sorted(var.glob("lease-*.json"))
    legacy = var / "lease.json"
    if legacy.exists():
        files.append(legacy)
    records = []
    for f in files:
        if f.name in ("lease-spend.json", "lease-request.json", "lease-handover.json"):
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("session"):
            records.append(rec)
    return records
