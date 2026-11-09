"""End-to-end runner test against the fake colab CLI (no login, no network).

Runs the smoke job (3 heartbeat units x 1 s) through bin/colab-run and
asserts the pulled outputs are hash-equal to the deterministic control.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)


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
    # The runner under test runs against a fixture repo (authored
    # config in the test tree): bin/colab-run derives its repo root
    # from its own location, so its lease reads and its run records
    # land in the fixture, never the checkout.
    root = repo_fixture.cli_repo(tmp_path / "repo", tmp_path / "var")
    return dict(os.environ, HOME=str(home), TEST_REPO_ROOT=str(root), COLAB_BIN=str(FAKE), FAKE_COLAB_STATE=str(tmp_path / "fake"), COLAB_RUNNER_VAR=str(tmp_path / "var"), **FAKE_ENV_KEEPALIVE, **FAKE_ENV_GATE, **extra)


def run_bin(env):
    return Path(env["TEST_REPO_ROOT"]) / "bin" / "colab-run"


def runs_dir(env):
    return Path(env["TEST_REPO_ROOT"]) / "results" / "runs"


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


def run_verb(env, *argv):
    return subprocess.run([sys.executable, str(run_bin(env)), *argv], capture_output=True, text=True, env=env, timeout=300)


def outbox_events(env):
    path = Path(env["COLAB_RUNNER_VAR"]) / "ui-outbox.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def terminal_events(env, run_id):
    return [e for e in outbox_events(env) if e.get("kind") == "run-terminal" and e.get("detail", {}).get("run_id") == run_id]


def logging_wrapper(tmp_path):
    """A COLAB_BIN wrapper that journals every invocation's argv
    (one JSON list per line) and delegates to the real fake."""
    wrapper = tmp_path / "wrapper_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "with (state / 'cli-calls.log').open('a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *sys.argv[1:]]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def cli_calls(env):
    log = Path(env["FAKE_COLAB_STATE"]) / "cli-calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


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


def _fire_smoke(env):
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
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
    downloads = [" ".join(call) for call in cli_calls(env) if "download" in call]
    heartbeat = [d for d in downloads if d.endswith("heartbeat.log")]
    unit0 = [d for d in downloads if d.endswith("unit-0.bin")]
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


def flaky_wrapper(tmp_path):
    """A COLAB_BIN wrapper with a test-controlled failure budget,
    steered by a JSON control file in the fake state dir:
    {"fail_reads": N} makes the next N reader execs exit 1;
    {"sessions_mode": "auth"} makes `sessions` answer with the
    CLI's auth prompt (the lease core's definitive-unknown read).
    Everything else delegates to the real fake."""
    wrapper = tmp_path / "flaky_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "ctl_path = state / 'flaky.json'\n"
        "ctl = json.loads(ctl_path.read_text()) if ctl_path.exists() else {}\n"
        "args = sys.argv[1:]\n"
        "if 'sessions' in args and ctl.get('sessions_mode') == 'auth':\n"
        "    print('Enter the authorization code:')\n"
        "    sys.exit(0)\n"
        "if 'exec' in args and any('read-' in a for a in args):\n"
        "    left = int(ctl.get('fail_reads', 0))\n"
        "    if left > 0:\n"
        "        ctl['fail_reads'] = left - 1\n"
        "        ctl_path.write_text(json.dumps(ctl))\n"
        "        print('flaky wrapper: reader refused', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def flaky_env(tmp_path, ctl=None):
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(flaky_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    if ctl is not None:
        state = Path(env["FAKE_COLAB_STATE"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "flaky.json").write_text(json.dumps(ctl))
    return env


def set_flaky_ctl(env, ctl):
    state = Path(env["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "flaky.json").write_text(json.dumps(ctl))


def _detach_smoke(env):
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--detach"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


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


def install_bare_job(env, name, exit_code, uploads=None):
    """Replace the fixture repo's jobs symlink with a real dir
    holding one checkpoint-less job: its payload writes its
    declared output and exits with the given code, speaking
    none of the runner's completion protocol (no checkpoint,
    no 'complete' line) — the shape the 2026-11-08 remainder
    gate jobs had when green runs recorded failed."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'result.txt').write_text('bare-result\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["result.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    if uploads:
        manifest["uploads"] = uploads
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_checkpoint_less_job_completes_on_its_exit_stamp(tmp_path):
    """D2 (2026-11-08 remainder report): a job that writes its
    outputs and exits 0 without the checkpoint protocol records
    complete — the launch wrapper's exit stamp is the outcome
    truth the death branch used to override with 'failed'."""
    env = runner_env(tmp_path)
    install_bare_job(env, "bareok", 0)
    r = run_verb(env, "bareok")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert any(p["path"] == "result.txt" for p in out["pulled"])
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"


def test_checkpoint_less_job_failure_records_its_exit_code(tmp_path):
    """The same job exiting non-zero records failed with the
    payload's own code — not the generic 'exited before
    completing' the death branch used to stamp on it."""
    env = runner_env(tmp_path)
    install_bare_job(env, "barebad", 3)
    r = run_verb(env, "barebad")
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    assert "job payload exited 3" in out["error"]
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed" and "job payload exited 3" in rec["error"]


def upload_fail_wrapper(tmp_path):
    """A COLAB_BIN wrapper that fails uploads of one named file
    (ctl {"fail_upload_named": "name"} in the fake state dir),
    delegating everything else to the real fake — flaky_wrapper's
    shape, aimed at the upload verb."""
    wrapper = tmp_path / "uploadfail_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "ctl_path = state / 'uploadfail.json'\n"
        "ctl = json.loads(ctl_path.read_text()) if ctl_path.exists() else {}\n"
        "args = sys.argv[1:]\n"
        "if 'upload' in args and ctl.get('fail_upload_named') and any(ctl['fail_upload_named'] in a for a in args):\n"
        "    print('uploadfail wrapper: upload refused', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def test_manifest_upload_failure_fails_the_run_at_upload_time(tmp_path):
    """D1 (2026-11-08 remainder report): a manifest upload whose
    transfer fails ends the run AT the upload — failed record,
    the upload named — instead of launching a job that discovers
    the missing file itself (or never does)."""
    env = runner_env(tmp_path)
    install_bare_job(env, "bareup", 0, uploads=["var/payload-data.bin"])
    root = Path(env["TEST_REPO_ROOT"])
    (root / "var").mkdir(exist_ok=True)
    (root / "var" / "payload-data.bin").write_bytes(b"payload-data")
    state = Path(env["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "uploadfail.json").write_text(json.dumps({"fail_upload_named": "payload-data.bin"}))
    env.update(COLAB_BIN=str(upload_fail_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r = run_verb(env, "bareup")
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    assert "manifest upload failed" in out["error"] and "payload-data.bin" in out["error"]
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed" and "remote_out" not in rec


def test_oversize_manifest_upload_moves_chunked_and_verified(tmp_path):
    """An upload over the single-file ceiling routes through
    the chunked transfer (hash-verified) and lands whole on the
    session — the ~100 MB bundle shape that silently failed
    through the plain upload."""
    env = runner_env(tmp_path)
    install_bare_job(env, "barebig", 0, uploads=["var/big.bin"])
    root = Path(env["TEST_REPO_ROOT"])
    (root / "var").mkdir(exist_ok=True)
    big = root / "var" / "big.bin"
    with open(big, "wb") as fh:
        fh.seek(51 * 1024 * 1024 - 1)
        fh.write(b"\x01")
    r = run_verb(env, "barebig")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    landed = Path(env["FAKE_COLAB_STATE"]) / "sessions" / rec["session"] / "big.bin"
    assert landed.stat().st_size == 51 * 1024 * 1024


def test_job_scripts_upload_to_run_scoped_remote_names(tmp_path):
    """Foreign-payload regression (2026-11-08 remainder lanes):
    every lane's gate script is named run.py, and the runner's
    /content/<basename> slot made runs on a shared session
    execute whichever payload landed last — one run carried a
    foreign job's label and another died on its foreign args.
    The remote script name now carries the run id: two jobs
    whose scripts share a basename land in different slots,
    neither of them the bare shared name."""
    env = runner_env(tmp_path)
    install_bare_job(env, "scopeone", 0)
    install_bare_job(env, "scopetwo", 0)
    env.update(COLAB_BIN=str(logging_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r1 = run_verb(env, "scopeone")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run_verb(env, "scopetwo")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    uploads = [c for c in cli_calls(env) if "upload" in c]
    script_dests = [c[-1] for c in uploads if c[-1].endswith("payload.py")]
    assert len(script_dests) == 2, uploads
    assert "scopeone" in script_dests[0] and "scopetwo" in script_dests[1]
    assert script_dests[0] != script_dests[1]
    assert all(d != "/content/payload.py" for d in script_dests)


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
