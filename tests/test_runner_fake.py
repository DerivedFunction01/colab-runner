"""End-to-end runner test against the fake colab CLI (no login, no network).

Runs the smoke job (3 heartbeat units x 1 s) through bin/colab-run and
asserts the pulled outputs are hash-equal to the deterministic control.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"


def control_sha(seed: str, unit: int) -> str:
    payload = hashlib.sha256(f"{seed}:{unit}".encode()).hexdigest().encode() * 64
    return hashlib.sha256(payload).hexdigest()


FAKE_ENV_KEEPALIVE = dict(KEEPALIVE_MAX_CYCLES="2", KEEPALIVE_INTERVAL_SECONDS="0", KEEPALIVE_ALLOW_NONCOLAB="1")


def test_colab_run_smoke_end_to_end(tmp_path):
    env = dict(os.environ, COLAB_BIN=str(FAKE), FAKE_COLAB_STATE=str(tmp_path / "fake"), COLAB_RUNNER_VAR=str(tmp_path / "var"), **FAKE_ENV_KEEPALIVE)
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-run"), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    pulled = {p["path"]: p["sha256"] for p in out["pulled"]}
    for unit in range(3):
        assert pulled[f"unit-{unit}.bin"] == control_sha("smoke", unit)
    rec = json.loads((REPO / "results" / "runs" / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"
    assert rec["usage_before"] and rec["usage_after"]


def test_orphan_sweep_marks_dead_session(tmp_path):
    env = dict(os.environ, COLAB_BIN=str(FAKE), FAKE_COLAB_STATE=str(tmp_path / "fake"), COLAB_RUNNER_VAR=str(tmp_path / "var"), **FAKE_ENV_KEEPALIVE)
    runs = REPO / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stale = runs / "ghost-20000101T000000Z.json"
    stale.write_text(json.dumps({"run_id": stale.stem, "job": "smoke", "session": "workload", "state": "running", "started_at": "2000-01-01T00:00:00+00:00"}))
    try:
        r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-run"), "smoke"], capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        assert json.loads(stale.read_text())["state"] == "orphaned"
    finally:
        stale.unlink(missing_ok=True)
