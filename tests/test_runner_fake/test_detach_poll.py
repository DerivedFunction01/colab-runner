"""Detached runs and the poll reconcile, against the fake
colab CLI: --detach returns at once, --wait reads only the
local record, --poll drives the run to its terminal state,
the failure bound fails fast on a confirmed-absent session,
an unknown classification never fails the run, and an
oversize output pulls through the chunked transfer."""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from runner_harness import FAKE, _detach_smoke, _write_run_record, cli_calls, flaky_env, logging_wrapper, run_bin, run_verb, runner_env, runs_dir, set_flaky_ctl, terminal_events  # noqa: F401


def test_detach_returns_at_once_and_poll_reconciles_to_complete(tmp_path):
    """E5: --detach launches and returns with the record at
    polling — the launcher is gone, so the record is frozen
    until a reconcile runs (the killed-launcher case: before,
    the frozen record waited for a future launch's sweep to even
    be noticed). Repeated --poll drives it to complete; exactly
    one run-terminal event lands in the outbox; a further --poll
    is a stable no-op."""
    env = runner_env(tmp_path)
    out = _detach_smoke(env)
    assert out["session"], "detach printed no session"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "polling", "the record advanced with no poller alive"
    assert rec.get("detached_launch") is True
    rc = 3
    deadline = time.time() + 180
    while rc == 3 and time.time() < deadline:
        rc = run_verb(env, "--poll", out["run_id"]).returncode
    assert rc == 0, "the reconciles never drove the run to completion"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"
    events = terminal_events(env, out["run_id"])
    assert len(events) == 1 and events[0]["detail"]["state"] == "complete"
    assert events[0]["detail"]["session"] == out["session"]
    assert run_verb(env, "--poll", out["run_id"]).returncode == 0
    assert len(terminal_events(env, out["run_id"])) == 1, "a second terminal event was announced"
    assert run_verb(env, "--wait", out["run_id"], "--max", "5").returncode == 0


def test_wait_reads_only_the_local_record(tmp_path):
    """E13: --wait exits 0/1/3 by the record's state and makes
    zero CLI calls — the counting wrapper proves the wait never
    touches the channel."""
    wrapper = logging_wrapper(tmp_path)
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(wrapper), FAKE_COLAB_REAL=str(FAKE))
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    _write_run_record(runs, "wait-done", "some-session", state="complete")
    _write_run_record(runs, "wait-bad", "some-session", state="failed")
    _write_run_record(runs, "wait-live", "some-session", state="polling")
    assert run_verb(env, "--wait", "wait-done").returncode == 0
    assert run_verb(env, "--wait", "wait-bad").returncode == 1
    assert run_verb(env, "--wait", "wait-live", "--max", "1").returncode == 3
    assert run_verb(env, "--wait", "wait-missing").returncode == 2
    assert cli_calls(env) == [], "the wait touched the CLI"


def test_poll_loop_fails_fast_once_the_session_is_confirmed_absent(tmp_path):
    """E11: the poll loop counts consecutive poll failures; at
    the bound it classifies the session, and a confirmed-absent
    reading fails the run fast — long before the manifest
    timeout — with the resume note on the record. The wrapper
    kills the session (its ACTIVE marker) at the first reader
    exec and refuses every reader exec after; the sessions list
    itself stays the real fake's, so the classification is the
    core's own tri-state read of a genuinely dead session."""
    wrapper = tmp_path / "killer_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "args = sys.argv[1:]\n"
        "if 'exec' in args and any('read-' in a for a in args):\n"
        "    marker = state / 'killed'\n"
        "    if not marker.exists():\n"
        "        state.mkdir(parents=True, exist_ok=True)\n"
        "        marker.write_text('1')\n"
        "        name = args[args.index('-s') + 1]\n"
        "        (state / 'sessions' / name / 'ACTIVE').unlink(missing_ok=True)\n"
        "    print('killer wrapper: reader refused', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(wrapper), FAKE_COLAB_REAL=str(FAKE))
    started = time.time()
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    elapsed = time.time() - started
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed"
    assert "confirmed absent" in rec["error"] and f"--resume {out['run_id']}" in rec["error"]
    assert rec["last_classification"]["verdict"] == "absent"
    assert elapsed < 120, f"the run polled a dead session for {elapsed:.0f}s instead of failing at the bound"
    events = terminal_events(env, out["run_id"])
    assert len(events) == 1 and events[0]["detail"]["state"] == "failed"


def test_poll_classification_unknown_never_fails_the_run(tmp_path):
    """E11: unknown is not death. With the sessions read answering
    the auth prompt (the core's definitive unknown) and the
    reader refusing, three --poll reconciles bump the failure
    count and classify — the run stays polling throughout, and
    once the channel heals the same verb drives it to complete."""
    env = flaky_env(tmp_path, ctl={"fail_reads": 99, "sessions_mode": "auth"})
    out = _detach_smoke(env)
    for _ in range(3):
        assert run_verb(env, "--poll", out["run_id"]).returncode == 3
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "polling", "an unknown classification failed or orphaned the run"
    assert rec["poll_failures"] == 3
    assert rec["last_classification"]["verdict"] == "unknown"
    set_flaky_ctl(env, {"fail_reads": 0, "sessions_mode": "normal"})
    rc = 3
    deadline = time.time() + 180
    while rc == 3 and time.time() < deadline:
        rc = run_verb(env, "--poll", out["run_id"]).returncode
    assert rc == 0
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"


def test_poll_failure_burst_below_the_bound_changes_nothing(tmp_path):
    """E11: transients are transients. Two refused reader execs
    (bound is three) are noted and the run completes on the next
    clean poll — no classification is ever taken."""
    env = flaky_env(tmp_path, ctl={"fail_reads": 2, "sessions_mode": "normal"})
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert "last_classification" not in rec, "a sub-bound burst was classified"
    assert rec.get("poll_failures") == 0


def test_pull_routes_an_oversize_output_through_the_chunked_transfer(tmp_path):
    """E6: an output above the 50 MB single-file ceiling moves
    via transfer.chunked_download — the downloads the channel
    sees are the .dl- parts, never one plain download of the
    whole file — and the landed bytes hash-match the remote's."""
    wrapper = logging_wrapper(tmp_path)
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(wrapper), FAKE_COLAB_REAL=str(FAKE))
    out = _detach_smoke(env)
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    sandbox = Path(env["FAKE_COLAB_STATE"]) / "sessions" / rec["session"] / rec["remote_out"]
    checkpoint = sandbox / "checkpoint.json"
    deadline = time.time() + 120
    while time.time() < deadline:
        if checkpoint.exists() and json.loads(checkpoint.read_text()).get("completed_units", 0) >= 3:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("the fake payload never completed its units")
    pattern = b"\x5a" * (51 * 1024 * 1024)
    (sandbox / "unit-0.bin").write_bytes(pattern)
    r = run_verb(env, "--poll", out["run_id"])
    assert r.returncode == 0, r.stdout + r.stderr
    downloads = [" ".join(call) for call in cli_calls(env) if "download" in call]
    assert any("unit-0.bin.dl-" in d for d in downloads), "the oversize output did not move in parts"
    assert not any("unit-0.bin" in d and ".dl-" not in d for d in downloads), "the oversize output also moved as one plain download"
    landed = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"] / "unit-0.bin"
    assert landed.stat().st_size == len(pattern)
    assert hashlib.sha256(landed.read_bytes()).hexdigest() == hashlib.sha256(pattern).hexdigest()
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    pulled = {p["path"]: p for p in rec["pulled"]}
    assert pulled["unit-0.bin"]["route"] == "chunked"


def test_poll_reconciles_a_foreground_shaped_record(tmp_path):
    """The resolution seam is launch-form-agnostic (2026-11-12
    commission): a "polling" record whose launcher died — the
    foreground form, no detached_launch marker — reconciles
    through --poll exactly like a detached one, because the
    payload is nohup-detached session-side either way. The
    two VM-restart specimens (p2f5form3, critic-union2) were
    this shape; one --poll per record is the whole cure."""
    env = runner_env(tmp_path)
    out = _detach_smoke(env)
    path = runs_dir(env) / f"{out['run_id']}.json"
    rec = json.loads(path.read_text())
    rec.pop("detached_launch", None)  # the foreground shape
    path.write_text(json.dumps(rec))
    rc = 3
    deadline = time.time() + 180
    while rc == 3 and time.time() < deadline:
        rc = run_verb(env, "--poll", out["run_id"]).returncode
    assert rc == 0
    rec = json.loads(path.read_text())
    assert rec["state"] == "complete"
    assert rec.get("collector_at"), "the reconciling polls never stamped their touches"
    assert rec.get("last_evidence_at"), "the record carries no evidence stamp"


def test_wait_reports_collector_staleness_from_the_record(tmp_path):
    """--wait's still-running read names attendance: a record
    whose collector stamps are fresh reports
    collector_stale false; the same record with its stamps
    aged past the bound (the dead-collector case) reports
    true — derived from the record alone, no channel use."""
    env = runner_env(tmp_path)
    out = _detach_smoke(env)
    path = runs_dir(env) / f"{out['run_id']}.json"
    r = run_verb(env, "--wait", out["run_id"], "--max", "0")
    assert r.returncode == 3, r.stdout + r.stderr
    assert json.loads(r.stdout)["collector_stale"] is False
    rec = json.loads(path.read_text())
    old = "2026-11-12T00:00:00+00:00"
    rec["started_at"] = old
    rec["last_evidence_at"] = old
    rec.pop("collector_at", None)
    path.write_text(json.dumps(rec))
    r = run_verb(env, "--wait", out["run_id"], "--max", "0")
    assert r.returncode == 3, r.stdout + r.stderr
    assert json.loads(r.stdout)["collector_stale"] is True
