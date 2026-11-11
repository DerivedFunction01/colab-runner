"""Record truth (2026-11-11) against the fake colab CLI: a
confirmed launch clears the prior attempt's launch_lost /
error in the same save, and the confirmation stamps the
session's measured shape (the reader probe's machine block)
on the run record.
"""

import json
import os
import subprocess
import sys

from runner_harness import FAKE, _fire_smoke, run_bin, runner_env, runs_dir  # noqa: F401


def _noop_launch_wrapper(tmp_path):
    """A COLAB_BIN wrapper whose launch exec reports success
    without planting anything — the daemon-side launch loss
    (kernel saturation ate the exec) as a fixture (the
    test_funnel leaf's shape, kept local per the harness
    convention)."""
    wrapper = tmp_path / "noop_launch_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if 'exec' in args and any('launch-' in a for a in args):\n"
        "    print('launched')\n"
        "    sys.exit(0)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def test_confirmed_launch_stamps_the_measured_shape(tmp_path):
    """Sizing reads the machine that ran the job, not the
    lease's declared shape: the confirmation reader's probe
    (nproc / MemTotal / cgroup limits) lands on the record
    at launch confirmation."""
    env = runner_env(tmp_path)
    out = _fire_smoke(env)
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"
    measured = rec.get("measured_shape")
    assert isinstance(measured, dict), "the confirmed launch stamped no measured shape"
    assert isinstance(measured["nproc"], int) and measured["nproc"] >= 1
    assert isinstance(measured["mem_total_bytes"], int) and measured["mem_total_bytes"] > 0
    assert "mem_limit_bytes" in measured and "cpu_limit" in measured, "the cgroup readings are absent (None is a reading; a missing key is not)"
    assert measured["at"] == rec["launch_confirmed_at"]


def test_resume_after_launch_lost_clears_the_stale_verdict(tmp_path):
    """A record closed launch_lost keeps launch_lost + error
    as its history; the resume's own confirmed launch must
    clear both in the confirmation save — a record that
    reads confirmed and lost at once states a contradiction
    (the stripfix2 specimen: starting + remote_out +
    launch_lost on one record)."""
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(_noop_launch_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE), COLAB_RUN_ABSENCE_GRACE_S="5")
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    run_id = json.loads(r.stdout)["run_id"]
    rec = json.loads((runs_dir(env) / f"{run_id}.json").read_text())
    assert rec["launch_lost"] is True and rec["error"], "the fixture did not produce the stale verdict"

    env = runner_env(tmp_path)
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--resume", run_id], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads((runs_dir(env) / f"{run_id}.json").read_text())
    assert rec["state"] == "complete"
    assert rec.get("launch_confirmed_at"), "the resumed launch never confirmed"
    assert "launch_lost" not in rec, "the confirmed launch left the prior attempt's launch_lost standing"
    assert "error" not in rec, "the confirmed launch left the prior attempt's error standing"
