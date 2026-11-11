"""Funnel hardening (2026-11-11) against the fake colab CLI:
the session pin, launch confirmation and launch-lost closure,
launch-health journaling, and the sweep's stale-running and
stale-starting rules."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from runner_harness import FAKE, _fire_smoke, _write_run_record, run_bin, run_verb, runner_env, runs_dir, terminal_events  # noqa: F401

# --- Funnel hardening (2026-11-11): pin, launch truth, health ----------------


def _slow_probe_wrapper(tmp_path):
    """A COLAB_BIN wrapper that stalls every upload verify-probe
    exec past the (test-shrunk) probe timeout and delegates
    everything else to the real fake."""
    wrapper = tmp_path / "slow_probe_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "args = sys.argv[1:]\n"
        "if 'exec' in args and any('upload-probe-' in a for a in args):\n"
        "    time.sleep(5)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def test_probe_timeout_fails_the_run_instead_of_stranding_it(tmp_path):
    """The consol defect (2026-11-11): the verify-probe exec
    timed out at 300 s and the uncaught TimeoutExpired killed
    the launcher, leaving the record at "starting" — five
    launches in a row. A probe timeout is now an upload failure:
    the run closes failed with the reason on the record, and the
    failure is journaled as launch-channel health."""
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(_slow_probe_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE), COLAB_RUN_PROBE_TIMEOUT_S="2")
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed", "the record stranded at a non-terminal state"
    assert "verify timed out" in rec["error"]
    journal = Path(env["COLAB_RUNNER_VAR"]) / "launch-health.jsonl"
    entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    assert any(e["stage"] == "probe" and e["ok"] is False for e in entries), "the probe failure was not journaled as health evidence"


def _noop_launch_wrapper(tmp_path):
    """A COLAB_BIN wrapper whose launch exec reports success
    without planting anything — the daemon-side launch loss
    (kernel saturation ate the exec) as a fixture."""
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


def test_launch_that_never_lands_closes_launch_lost(tmp_path):
    """The launch exec's result used to be ignored and the
    record advanced to polling unconditionally, so a launch the
    venue never received read as a running job forever. The
    confirmation read now decides: no footprint, terminal
    failed with launch_lost set."""
    env = runner_env(tmp_path)
    # The absence grace shrinks so the verdict is bounded even
    # if the confirmation pass races a foreign payload's pids
    # (the fake reader's pgrep is box-global under xdist; the
    # in-VM reader it models is session-local). Either path —
    # confirmation or grace — closes the same launch-lost.
    env.update(COLAB_BIN=str(_noop_launch_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE), COLAB_RUN_ABSENCE_GRACE_S="5")
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed" and rec["launch_lost"] is True
    assert "never landed" in rec["error"]
    events = terminal_events(env, out["run_id"])
    assert len(events) == 1 and events[0]["detail"]["state"] == "failed"


def test_poll_closes_a_never_landed_run_as_launch_lost(tmp_path):
    """The featall shape at reconcile time: run.py planted, its
    out dir and process never created, the record reading
    "running"/"polling" for hours. A poll whose reader finds
    total absence past the launch grace closes the record as
    launch-lost instead of reporting still-running forever."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    ghost_id = "smoke-20000101T000000Z"
    (runs / f"{ghost_id}.json").write_text(json.dumps({
        "run_id": ghost_id, "job": "smoke", "session": session, "state": "polling",
        "started_at": "2000-01-01T00:00:00+00:00", "launch_at": "2000-01-01T00:00:00+00:00",
        "remote_out": f"smoke-out-{ghost_id}",
    }))
    r = run_verb(env, "--poll", ghost_id)
    assert r.returncode == 1, r.stdout + r.stderr
    rec = json.loads((runs / f"{ghost_id}.json").read_text())
    assert rec["state"] == "failed" and rec["launch_lost"] is True


def test_sweep_closes_stale_running_but_spares_registrations_and_fresh_evidence(tmp_path):
    """The sweep's stale-running rule closes records whose
    launcher died mid-launch, discriminated by the launch
    stamps: a registration-style record (no remote_out — the
    co-tenancy evidence class) and a launched record with
    fresh reconcile evidence are both left exactly as found."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    stale = runs / "launched-stale.json"
    stale.write_text(json.dumps({
        "run_id": "launched-stale", "job": "smoke", "session": session, "state": "running",
        "started_at": "2000-01-01T00:00:00+00:00", "remote_out": "smoke-out-launched-stale",
    }))
    registration = _write_run_record(runs, "registration-only", session)
    fresh = runs / "launched-fresh.json"
    fresh.write_text(json.dumps({
        "run_id": "launched-fresh", "job": "smoke", "session": session, "state": "running",
        "started_at": "2000-01-01T00:00:00+00:00", "remote_out": "smoke-out-launched-fresh",
        "last_evidence_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }))
    before = [registration.read_text(), fresh.read_text()]
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(stale.read_text())
    assert rec["state"] == "failed" and rec["launch_lost"] is True
    assert [registration.read_text(), fresh.read_text()] == before, "the sweep touched a record outside the stale-launched class"


def test_session_pin_adopts_the_named_lease_and_refuses_an_unknown_one(tmp_path):
    """--session pins the run to the named lease: the pinned
    run lands on it with the pin stamped; an unknown name fails
    pin-unavailable and adopts nothing."""
    env = runner_env(tmp_path)
    first = _fire_smoke(env)
    session = json.loads((runs_dir(env) / f"{first['run_id']}.json").read_text())["session"]
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--session", session], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["session"] == session and rec["session_pin"] == session
    assert rec.get("launch_confirmed_at"), "a normal launch carries no confirmation stamp"
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--session", "no-such-session-00"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed" and "pin-unavailable" in rec["error"]
    assert not rec.get("session"), "a refused pin adopted a session anyway"


def _write_starting_record(runs, run_id, session, started_at):
    p = runs / f"{run_id}.json"
    p.write_text(json.dumps({"run_id": run_id, "job": "smoke", "session": session, "caller": "test", "state": "starting", "started_at": started_at}))
    return p


def test_sweep_closes_a_stale_starting_record_and_spares_a_fresh_one(tmp_path):
    """The sweep's "starting" class: a pre-launch record whose
    freshest stamp (and file mtime — every pre-launch save
    rewrites the file) is past the stale bound is a phantom
    whose launcher died in the upload phase; it closes
    launch-lost instead of reading as in-flight work forever
    (2026-11-11: six steampower records sat in "starting" for
    hours). A fresh "starting" record — a launch in progress —
    is untouched."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    first = _fire_smoke(env)
    session = json.loads((runs / f"{first['run_id']}.json").read_text())["session"]
    stale = _write_starting_record(runs, "ghost-start", session, "2000-01-01T00:00:00+00:00")
    os.utime(stale, (946684800, 946684800))
    fresh = _write_starting_record(runs, "fresh-start", session, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(stale.read_text())
    assert rec["state"] == "failed" and rec.get("launch_lost"), rec
    assert json.loads(fresh.read_text())["state"] == "starting"


def test_sweep_orphans_a_starting_record_on_a_dead_session(tmp_path):
    """A "starting" record bound to a session that no longer
    exists shares the fate of its running/polling siblings:
    orphaned by the same sweep, resumable by --resume."""
    env = runner_env(tmp_path)
    runs = runs_dir(env)
    runs.mkdir(parents=True, exist_ok=True)
    _fire_smoke(env)
    p = _write_starting_record(runs, "dead-start", "no-such-session-xyz", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    r = run_verb(env, "--sweep")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(p.read_text())["state"] == "orphaned"
