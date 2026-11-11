#!/usr/bin/env python3
"""The non-launch verbs: --poll (one bounded reconcile of a
launched run), --wait (a bounded wait over the local record
only), and the lease close-out a reconciled run's terminal
transition performs.
"""

import json
import os
import sys
import time
from datetime import datetime

from .foundation import REPO, RUNS, TOKEN, lease, mirror, now
from .pull import finalize_run, pull_outputs
from .reader import _absence_grace_s, _launch_age_s, read_reader, register_poll_failure, write_reader
from .records import TERMINAL_STATES, load_run_record, mark_terminal, save_record
from .sweep import orphan_sweep


def close_out_lease(rec: dict) -> None:
    """The terminal close-out for a run reconciled by --poll:
    what the foreground poller's finally does — release the job
    registration (finish_job no-ops when none matches) and
    capture the transient state."""
    if lease.load_config(REPO).get("enabled"):
        lease.finish_job(REPO, rec["run_id"])
    mirror("capture")


def poll_run(run_id: str) -> int:
    """One bounded reconcile of a launched run (E5): orphan
    sweep, one reader exec, one pull, one record update — nothing
    here waits, so any caller (an agent, a scheduler job) can
    drive a detached run to its terminal state by repeating the
    verb. Exit 0 complete, 1 failed/orphaned, 2 no such run or
    not staged, 3 still running."""
    rec = load_run_record(run_id)
    if rec is None:
        print(f"colab-run --poll: no run record at {RUNS / f'{run_id}.json'}", file=sys.stderr)
        return 2
    if not TOKEN.exists() and not os.environ.get("COLAB_BIN"):
        print("colab-run: STAGED — no colab login yet. Run ops/bootstrap_local.sh, then the one-time login (see README).", file=sys.stderr)
        return 2
    manifest_path = REPO / "jobs" / str(rec.get("job")) / "job.json"
    if not manifest_path.exists():
        print(f"colab-run --poll: no job manifest at {manifest_path}", file=sys.stderr)
        return 2
    job = json.loads(manifest_path.read_text())
    orphan_sweep()
    rec = load_run_record(run_id) or rec
    if rec.get("state") in TERMINAL_STATES:
        print(json.dumps({"run_id": run_id, "state": rec["state"]}, indent=2))
        return 0 if rec["state"] == "complete" else 1
    session = rec.get("session")
    remote_out = rec.get("remote_out")
    if not session or not remote_out:
        print(json.dumps({"run_id": run_id, "state": rec.get("state"), "error": "record predates launch (no session/remote_out); --resume relaunches it"}, indent=2))
        return 1
    reader = write_reader(run_id, remote_out, job.get("outputs", []))
    poll_timeout = int(os.environ.get("COLAB_RUN_POLL_TIMEOUT_S", "300"))
    state, poll_error = read_reader(session, reader, poll_timeout)
    if state is None:
        if register_poll_failure(rec, session, poll_error) == "failed":
            close_out_lease(rec)
            print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
            return 1
        print(json.dumps({"run_id": run_id, "state": rec["state"]}, indent=2))
        return 3
    rec["poll_failures"] = 0
    done_units = (state.get("checkpoint") or {}).get("completed_units", 0)
    rec["last_checkpoint_units"] = done_units
    rec["last_evidence_at"] = now()
    save_record(rec)
    if job.get("outputs"):
        pull_outputs(rec, job, session, remote_out, state)
        save_record(rec)
    log = state.get("heartbeat_tail") or []
    complete = any("complete" in ln for ln in log) or (job.get("units") and done_units >= job["units"]) or state.get("exit_stamp") is not None
    if not complete and not state.get("job_pids") and not state.get("job_log_tail") and state.get("exit_stamp") is None and not state.get("checkpoint") and _launch_age_s(rec) > _absence_grace_s():
        # Total absence past the grace bound: the launch never
        # landed, and the record closes as launch-lost instead
        # of reading "still running" forever (2026-11-11
        # consol-featall: planted run.py, no out dir, no process,
        # record "running" for hours).
        mark_terminal(rec, "failed", launch_lost=True, error=f"no job footprint on {session} {_absence_grace_s()}s after launch: the launch never landed; relaunch with --resume {run_id} to retry")
        close_out_lease(rec)
        print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
        return 1
    if not complete and not state.get("job_pids") and state.get("job_log_tail"):
        # The launched process is gone with no completion signal:
        # the same death the poll loop fails on (see main).
        tail = "\n".join(state["job_log_tail"])
        mark_terminal(rec, "failed", error=(f"job exited before completing; remote log tail: {tail}")[-500:])
        close_out_lease(rec)
        print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
        return 1
    past_deadline = False
    try:
        past_deadline = time.time() >= datetime.fromisoformat(rec["started_at"]).timestamp() + job.get("timeout", 18000)
    except (KeyError, ValueError):
        past_deadline = False
    if complete or past_deadline:
        rc, result = finalize_run(rec, job, session, remote_out, state, rec.get("usage_before"))
        close_out_lease(rec)
        print(json.dumps(result, indent=2))
        return rc
    print(json.dumps({"run_id": run_id, "state": rec["state"]}, indent=2))
    return 3


def wait_run(run_id: str, max_seconds: int) -> int:
    """A bounded wait over the LOCAL run record only (E13): zero
    channel use — no CLI call, no lease read — so waiting never
    becomes the polling loop it replaces. Exit 0 complete, 1
    failed/orphaned, 2 no such record, 3 still running when the
    bound runs out."""
    if not (RUNS / f"{run_id}.json").exists():
        print(f"colab-run --wait: no run record for {run_id}", file=sys.stderr)
        return 2
    end = time.time() + max_seconds
    while True:
        rec = load_run_record(run_id)
        state = (rec or {}).get("state")
        if state == "complete":
            print(json.dumps({"run_id": run_id, "state": state}))
            return 0
        if state in ("failed", "orphaned"):
            print(json.dumps({"run_id": run_id, "state": state}))
            return 1
        remaining = end - time.time()
        if remaining <= 0:
            print(json.dumps({"run_id": run_id, "state": state}))
            return 3
        time.sleep(min(2, remaining))
