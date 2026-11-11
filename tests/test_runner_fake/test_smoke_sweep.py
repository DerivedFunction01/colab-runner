"""Runner smoke end-to-end and the orphan sweep, against the
fake colab CLI: the smoke job's pulled outputs hash-match the
deterministic control, and the sweep marks exactly the runs
whose sessions are gone — co-tenants of a live session are
left exactly as found."""

import json
import subprocess
import sys
from pathlib import Path

from runner_harness import FAKE, _fire_smoke, _write_run_record, control_sha, run_bin, run_verb, runner_env, runs_dir  # noqa: F401


def test_colab_run_smoke_end_to_end(tmp_path):
    env = runner_env(tmp_path)
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    pulled = {p["path"]: p["sha256"] for p in out["pulled"]}
    for unit in range(3):
        assert pulled[f"unit-{unit}.bin"] == control_sha("smoke", unit)
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"
    assert rec["usage_before"] and rec["usage_after"]


def test_orphan_sweep_marks_dead_session(tmp_path):
    """E10: the sweep no longer rides the launch path. A plain
    launch leaves a dead session's stale record exactly as it
    found it; the reconcile verb (--sweep) is what marks it."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    stale = runs / "ghost-20000101T000000Z.json"
    stale.write_text(json.dumps({"run_id": stale.stem, "job": "smoke", "session": "ghost-session-absent", "state": "running", "started_at": "2000-01-01T00:00:00+00:00"}))
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(stale.read_text())["state"] == "running", "the launch path swept a record it had no business touching"
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(stale.read_text())["state"] == "orphaned"
    findings = json.loads(r.stdout)["findings"]
    assert [f["run_id"] for f in findings] == [stale.stem]


def test_orphan_sweep_keeps_cotenants_of_a_live_session(tmp_path):
    """Machine model (2026-11-06): jobs on one session are
    co-tenants, not rivals. The sweep's predicate is the
    session's liveness, so two in-flight runs sharing a live
    session are left exactly as they are — by a second fire
    landing alongside them and by an explicit --sweep alike."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    tenants = [_write_run_record(runs, f"cotenant-{tag}", session) for tag in ("a", "b")]
    before = [p.read_text() for p in tenants]
    second = _fire_smoke(env)
    assert second["state"] == "complete"
    second_rec = json.loads((runs / f"{second['run_id']}.json").read_text())
    assert second_rec["session"] == session, "the second fire rides the same live session"
    assert [p.read_text() for p in tenants] == before, "the second fire's launch left co-tenant records untouched"
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [p.read_text() for p in tenants] == before, "the sweep marked a co-tenant of a live session"


def test_orphan_sweep_marks_cotenants_of_a_dead_session_together(tmp_path):
    """Co-tenants share the session's fate: when the session is
    gone, every in-flight run bound to it is marked orphaned in
    the same sweep — while a run on a live session is untouched."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    live_tenant = _write_run_record(runs, "cotenant-live", session)
    dead_tenants = [_write_run_record(runs, f"cotenant-dead-{tag}", "ghost-session-absent") for tag in ("a", "b")]
    live_before = live_tenant.read_text()
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    for p in dead_tenants:
        assert json.loads(p.read_text())["state"] == "orphaned"
    assert live_tenant.read_text() == live_before


def test_sweep_matches_live_names_exactly(tmp_path):
    """E10: membership is exact-name through the core's parser.
    The retired substring test read a record as alive whenever
    its session text appeared anywhere in the raw list output —
    a prefix of a live name counted as alive. Exact matching
    marks the prefix and the suffix, and keeps the exact name."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    exact = _write_run_record(runs, "tenant-exact", session)
    suffixed = _write_run_record(runs, "tenant-suffix", session + "-extra")
    prefixed = _write_run_record(runs, "tenant-prefix", session[:8])
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(exact.read_text())["state"] == "running"
    assert json.loads(suffixed.read_text())["state"] == "orphaned"
    assert json.loads(prefixed.read_text())["state"] == "orphaned", "a name that is a substring of a live one read as alive"


def test_acquire_refusal_names_an_orphaned_live_session(tmp_path):
    """E1, runner half: when an acquire fails and a live session
    at the requested shape has no lease record, the failure path
    names the orphan state and prints the adopt command — the
    route back to the job path — instead of leaving the caller
    to rediscover it. A plain agent actor is refused the fresh
    start (the suite's test-actor door is shut for this fire),
    and the planted session is live in the fake but recorded
    nowhere."""
    env = runner_env(tmp_path)
    r = subprocess.run([sys.executable, str(FAKE), "--auth", "oauth2", "new", "-s", "orphan-live-1", "--high-mem"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    agent_env = dict(env)
    agent_env.pop("COLAB_LEASE_ACTOR", None)
    r = subprocess.run([sys.executable, str(run_bin(agent_env)), "smoke"], capture_output=True, text=True, env=agent_env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "orphan-live-1" in r.stderr and "bin/colab-lease adopt orphan-live-1" in r.stderr, r.stderr
