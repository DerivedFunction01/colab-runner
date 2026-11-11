"""Resume and the pull cadence, against the fake colab CLI:
a resume preserves the record's launch facts, a torn prior
starts fresh, declared outputs pull at every checkpoint
during the run, a timed-out poll degrades instead of killing
the poller, and a shrinking re-pull rotates the held copy."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from runner_harness import FAKE, cli_calls, logging_wrapper, run_bin, runner_env, runs_dir  # noqa: F401


def test_resume_preserves_the_original_started_at(tmp_path):
    """--resume continues a run; it does not relaunch it. The
    record's launch facts (started_at above all — the field
    concurrency evidence reads) survive a resume byte-identical;
    the resume adds its own facts. (Live defect 2026-11-06: a
    resumed hold came back stamped with the resume moment.)"""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    run_id = "smoke-resumeproof"
    rec_path = runs / f"{run_id}.json"
    rec_path.write_text(json.dumps({"run_id": run_id, "job": "smoke", "state": "orphaned", "started_at": "2000-01-01T00:00:00+00:00", "session": "ghost-session-absent"}))
    try:
        r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--resume", run_id], capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        rec = json.loads(rec_path.read_text())
        assert rec["state"] == "complete"
        assert rec["started_at"] == "2000-01-01T00:00:00+00:00", "resume overwrote the launch time"
        assert rec.get("resumed") is True and rec.get("resumed_at")
    finally:
        rec_path.unlink(missing_ok=True)


def test_resume_over_a_torn_record_starts_fresh_instead_of_crashing(tmp_path):
    """A flap-killed fire can leave a 0-byte run record. The
    sweep already skips torn records; the resume path must too
    — a torn prior holds nothing to preserve, so the run starts
    fresh (its damaged file replaced by a valid record) rather
    than every later entry dying on JSONDecodeError (the
    geodata lane's 2026-11-08 block)."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    run_id = "smoke-tornprior"
    rec_path = runs / f"{run_id}.json"
    rec_path.write_bytes(b"")
    try:
        r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--resume", run_id], capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "torn" in r.stderr
        rec = json.loads(rec_path.read_text())
        assert rec["state"] == "complete"
    finally:
        rec_path.unlink(missing_ok=True)


def test_checkpoint_pull_lands_outputs_during_the_run(tmp_path):
    """checkpoint_pull_seconds is a pull cadence, not only a poll
    cadence: declared outputs download at every checkpoint while
    the job runs, so a long job's growing outputs (the venue
    keeper's log is the live case) reach local readers mid-run
    instead of only at completion. (Live defect 2026-11-07: the
    relay waiting on the keeper's log starved for the job's whole
    12 h timeout because pulls happened only after the loop.)

    E6 change detection rides the same cadence: a CHANGING
    output (heartbeat.log grows every unit) is pulled at multiple
    cadences, while a STABLE output (unit-0.bin, written once at
    its unit and never touched again) is pulled exactly once —
    its reader stat never changes, so later cadences skip it."""
    wrapper = logging_wrapper(tmp_path)
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(wrapper), FAKE_COLAB_REAL=str(FAKE))
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    downloads = [call for call in cli_calls(env) if "download" in call]
    def pulls_of(name):
        # A pull is counted by its remote source: pulls land on a
        # staging sibling (dest ends .pulling) since the shrink
        # guard, so the destination no longer ends with the name.
        return [c for c in downloads if any(a.startswith("/content/") and a.endswith("/" + name) for a in c)]
    heartbeat = pulls_of("heartbeat.log")
    unit0 = pulls_of("unit-0.bin")
    assert len(heartbeat) >= 2, f"heartbeat.log downloaded {len(heartbeat)} time(s): the final pull only, no checkpoint pull during the run"
    assert len(unit0) == 1, f"unit-0.bin downloaded {len(unit0)} time(s): an unchanged output must be pulled once and skipped thereafter"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec.get("last_checkpoint_pull_at"), "the run record carries no checkpoint pull stamp"
    assert rec.get("output_stats", {}).get("unit-0.bin"), "the run record carries no output stat for the skipped output"


def test_poll_timeout_degrades_instead_of_killing_the_poller(tmp_path):
    """A checkpoint poll whose CLI call exceeds its timeout is a
    failed poll, not a dead poller: the loop notes it on the
    record and continues to the next cadence. (Live defect
    2026-11-06: an uncaught TimeoutExpired killed the poller and
    stranded the run at polling while its payload completed.)"""
    wrapper = tmp_path / "wrapper_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "marker = state / 'read-delayed'\n"
        "args = sys.argv[1:]\n"
        "if 'exec' in args and any('read-' in a for a in args) and not marker.exists():\n"
        "    state.mkdir(parents=True, exist_ok=True)\n"
        "    marker.write_text('1')\n"
        "    time.sleep(5)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(wrapper), FAKE_COLAB_REAL=str(FAKE), COLAB_RUN_POLL_TIMEOUT_S="2")
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec.get("last_poll_error"), "the timed-out poll left no note on the record"


def test_shrinking_repull_rotates_the_held_copy(tmp_path):
    """A remote output that restarts smaller must not erase the
    history already pulled: the re-pull rotates the held copy
    aside under a stamped sibling name, the live file takes the
    new content, and the rotation is noted on the pull entry and
    the run record. (Live defect 2026-11-08: after a wrapper
    resume, the venue keeper's pulled log shrank to its first
    line and the accumulated record was gone.)"""
    wrapper = tmp_path / "shrink_wrapper.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "args = sys.argv[1:]\n"
        "if 'download' in args and any(a.endswith('/checkpoint.json') for a in args):\n"
        "    marker = state / 'checkpoint-downloads'\n"
        "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
        "    marker.write_text(str(count))\n"
        "    if count >= 1:\n"
        "        for remote in state.glob('sessions/*/**/unit-0.bin'):\n"
        "            remote.write_bytes(b'shrunk\\n')\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(wrapper), FAKE_COLAB_REAL=str(FAKE))
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert (results / "unit-0.bin").read_bytes() == b"shrunk\n", "the live file does not hold the new, smaller content"
    rotated = sorted(results.glob("unit-0.bin.rotated-*"))
    assert len(rotated) == 1, [p.name for p in results.iterdir()]
    payload = hashlib.sha256(b"smoke:0").hexdigest().encode() * 64
    assert rotated[0].read_bytes() == payload, "the rotated sibling lost the previously pulled content"
    assert not list(results.glob("*.pulling")), "a staging file was left behind"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert "unit-0.bin" in (rec.get("last_pull_rotation") or ""), "the rotation is not noted on the run record"
    entry = next(e for e in rec["pulled"] if e["path"] == "unit-0.bin")
    assert entry["rotated"]["bytes"] == len(payload), "the rotation is not noted on the pull entry"
