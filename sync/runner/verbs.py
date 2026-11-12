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
from datetime import datetime, timezone
from pathlib import Path

from .foundation import REPO, RUNS, TOKEN, lease, mirror, now
from .pull import finalize_run, pull_outputs
from .reader import _absence_grace_s, _launch_age_s, read_reader, register_poll_failure, write_reader
from .records import TERMINAL_STATES, collector_stale, load_run_record, mark_terminal, save_record, stamp_collector
from .sweep import orphan_sweep


def plant_git_identity(session: str, repo_ref: str, tree: str, caller: "str | None") -> int:
    """The plant verb, beside the key plant: set the source
    repo's configured git identity repo-locally in a session
    tree formed outside the seed path (a key clone, a branch
    transport), read it back, and record the plant on its own
    run record. Exit 0 planted, 1 the plant or its readback
    failed on the session, 2 no complete identity is
    configured at the source repo (nothing is planted)."""
    from .foundation import colab, git_identity, repo_registry

    local_path = None
    if Path(repo_ref).expanduser().is_dir():
        local_path = str(Path(repo_ref).expanduser())
    else:
        try:
            registry = repo_registry.load_local_registry(REPO)
            _repo_id, entry = repo_registry.get_entry(registry, repo_ref)
            local_path = entry["local_path"]
        except (ValueError, KeyError) as exc:
            print(f"colab-run plant-git-identity: cannot resolve repo {repo_ref!r}: {exc}", file=sys.stderr)
            return 2
    identity = git_identity.repo_identity(local_path)
    if identity is None:
        print(f"colab-run plant-git-identity: no complete repo-local identity (user.name + user.email) configured at {local_path} — nothing planted", file=sys.stderr)
        return 2
    run_id = datetime.now(timezone.utc).strftime("plant-git-identity-%Y%m%dT%H%M%SZ")
    rec = {
        "run_id": run_id,
        "job": "plant-git-identity",
        "session": session,
        "caller": caller,
        "tree": tree,
        "repo": repo_ref,
        "local_path": local_path,
        "git_identity": identity,
        "state": "starting",
        "started_at": now(),
    }
    save_record(rec)
    payload = REPO / "var" / f"plant-git-identity-{run_id}.py"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(git_identity.plant_payload(tree, identity["name"], identity["email"]))
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(payload), timeout=150)
        verdict = git_identity.parse_plant_output(getattr(r, "stdout", "") or "")
    except Exception as exc:  # the exec itself failed: the record says so
        verdict = {"planted": False, "error": f"exec failed: {exc}"[:300]}
    rec["plant"] = verdict
    rec["state"] = "complete" if verdict and verdict.get("planted") else "failed"
    if rec["state"] != "complete" and not (verdict and verdict.get("error")):
        rec["error"] = "plant readback did not match the source identity"
    rec["finished_at"] = now()
    save_record(rec)
    print(json.dumps(rec, indent=2))
    return 0 if rec["state"] == "complete" else 1


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
        if register_poll_failure(rec, session, poll_error, collector=os.environ.get("COLAB_CALLER")) == "failed":
            close_out_lease(rec)
            print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
            return 1
        print(json.dumps({"run_id": run_id, "state": rec["state"], "collector_stale": collector_stale(rec)}, indent=2))
        return 3
    rec["poll_failures"] = 0
    done_units = (state.get("checkpoint") or {}).get("completed_units", 0)
    rec["last_checkpoint_units"] = done_units
    rec["last_evidence_at"] = now()
    stamp_collector(rec, os.environ.get("COLAB_CALLER"))
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
        stamp_collector(rec, os.environ.get("COLAB_CALLER"))
        rc, result = finalize_run(rec, job, session, remote_out, state, rec.get("usage_before"))
        close_out_lease(rec)
        print(json.dumps(result, indent=2))
        return rc
    print(json.dumps({"run_id": run_id, "state": rec["state"], "collector_stale": collector_stale(rec), "collector": rec.get("collector")}, indent=2))
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
            print(json.dumps({"run_id": run_id, "state": state, "collector_stale": collector_stale(rec or {}), "collector": (rec or {}).get("collector")}))
            return 3
        time.sleep(min(2, remaining))
