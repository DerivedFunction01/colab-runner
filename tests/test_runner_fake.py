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

# Session shape gate: the suite's test actor claim (see
# test_lease_fake.lease_env) — without it the runner's acquire is
# an agent actor and caller attribution refuses the fresh start.
FAKE_ENV_GATE = dict(COLAB_LEASE_ACTOR="test")


def runner_env(tmp_path, **extra):
    # A scratch HOME, exactly as test_lease_fake.lease_env builds:
    # without it the fake CLI writes its session bindings into the
    # real ~/.config/colab-cli/sessions.json (the 2026-11-05
    # pollution that blinded the lease machinery to live sessions).
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    (home / ".config" / "colab-cli" / "sessions.json").write_text("{}")
    return dict(os.environ, HOME=str(home), COLAB_BIN=str(FAKE), FAKE_COLAB_STATE=str(tmp_path / "fake"), COLAB_RUNNER_VAR=str(tmp_path / "var"), **FAKE_ENV_KEEPALIVE, **FAKE_ENV_GATE, **extra)


def test_colab_run_smoke_end_to_end(tmp_path):
    env = runner_env(tmp_path)
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
    env = runner_env(tmp_path)
    runs = REPO / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stale = runs / "ghost-20000101T000000Z.json"
    stale.write_text(json.dumps({"run_id": stale.stem, "job": "smoke", "session": "ghost-session-absent", "state": "running", "started_at": "2000-01-01T00:00:00+00:00"}))
    try:
        r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-run"), "smoke"], capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        assert json.loads(stale.read_text())["state"] == "orphaned"
    finally:
        stale.unlink(missing_ok=True)


def _fire_smoke(env):
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-run"), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def _write_run_record(runs, run_id, session, state="running"):
    p = runs / f"{run_id}.json"
    p.write_text(json.dumps({"run_id": run_id, "job": "smoke", "session": session, "state": state, "started_at": "2000-01-01T00:00:00+00:00"}))
    return p


def test_orphan_sweep_keeps_cotenants_of_a_live_session(tmp_path):
    """Machine model (2026-11-06): jobs on one session are
    co-tenants, not rivals. The sweep's predicate is the
    session's liveness, so two in-flight runs sharing a live
    session are both left exactly as they are — and the second
    fire lands on the same session and completes alongside
    them, never serial behind them."""
    env = runner_env(tmp_path)
    runs = REPO / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    tenants = [_write_run_record(runs, f"cotenant-{tag}", session) for tag in ("a", "b")]
    before = [p.read_text() for p in tenants]
    try:
        second = _fire_smoke(env)
        assert second["state"] == "complete"
        second_rec = json.loads((runs / f"{second['run_id']}.json").read_text())
        assert second_rec["session"] == session, "the second fire rides the same live session"
        assert [p.read_text() for p in tenants] == before, "the sweep and the second fire's poll leave co-tenant records untouched"
    finally:
        for p in tenants:
            p.unlink(missing_ok=True)


def test_orphan_sweep_marks_cotenants_of_a_dead_session_together(tmp_path):
    """Co-tenants share the session's fate: when the session is
    gone, every in-flight run bound to it is marked orphaned in
    the same sweep — while a run on a live session is untouched."""
    env = runner_env(tmp_path)
    runs = REPO / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    live_tenant = _write_run_record(runs, "cotenant-live", session)
    dead_tenants = [_write_run_record(runs, f"cotenant-dead-{tag}", "ghost-session-absent") for tag in ("a", "b")]
    live_before = live_tenant.read_text()
    try:
        _fire_smoke(env)
        for p in dead_tenants:
            assert json.loads(p.read_text())["state"] == "orphaned"
        assert live_tenant.read_text() == live_before
    finally:
        live_tenant.unlink(missing_ok=True)
        for p in dead_tenants:
            p.unlink(missing_ok=True)


def test_resume_preserves_the_original_started_at(tmp_path):
    """--resume continues a run; it does not relaunch it. The
    record's launch facts (started_at above all — the field
    concurrency evidence reads) survive a resume byte-identical;
    the resume adds its own facts. (Live defect 2026-11-06: a
    resumed hold came back stamped with the resume moment.)"""
    env = runner_env(tmp_path)
    runs = REPO / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_id = "smoke-resumeproof"
    rec_path = runs / f"{run_id}.json"
    rec_path.write_text(json.dumps({"run_id": run_id, "job": "smoke", "state": "orphaned", "started_at": "2000-01-01T00:00:00+00:00", "session": "ghost-session-absent"}))
    try:
        r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-run"), "smoke", "--resume", run_id], capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        rec = json.loads(rec_path.read_text())
        assert rec["state"] == "complete"
        assert rec["started_at"] == "2000-01-01T00:00:00+00:00", "resume overwrote the launch time"
        assert rec.get("resumed") is True and rec.get("resumed_at")
    finally:
        rec_path.unlink(missing_ok=True)


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
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-run"), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    rec = json.loads((REPO / "results" / "runs" / f"{out['run_id']}.json").read_text())
    assert rec.get("last_poll_error"), "the timed-out poll left no note on the record"
