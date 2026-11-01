"""Lease lifecycle against the fake colab CLI (no login, no network).

Covers the lease model's core promises: one session per profile acquired
once and adopted afterwards, the reaper enforcing the hard deadline, a
record whose session died being cleared, and manual release.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"


def lease_env(tmp_path):
    return dict(
        os.environ,
        COLAB_BIN=str(FAKE),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        # Keepalive: two instant cycles in the sandbox, then exit — an
        # unbounded loop here would leak a process onto this machine.
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
    )


def run_lease(env, *args):
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def record_path(env):
    # Records live per profile since the two-tier build; the legacy
    # lease.json is only the migration fallback.
    var = Path(env["COLAB_RUNNER_VAR"])
    per_profile = var / "lease-standard.json"
    return per_profile if per_profile.exists() else var / "lease.json"


def record(env):
    return json.loads(record_path(env).read_text())


def test_acquire_adopt_status(tmp_path):
    env = lease_env(tmp_path)
    first = run_lease(env, "acquire", "standard")
    assert first["action"] == "created"
    assert first["keepalive"] == "launched"
    assert first["record"]["session"] == "standard"
    sandbox_log = Path(env["FAKE_COLAB_STATE"]) / "sessions" / "standard" / "keepalive.log"
    import time

    for _ in range(50):
        if sandbox_log.exists() and sandbox_log.read_text().strip():
            break
        time.sleep(0.2)
    assert sandbox_log.exists() and json.loads(sandbox_log.read_text().strip().split("\n")[-1])["cycle"] >= 1
    second = run_lease(env, "acquire", "standard")
    assert second["action"] == "adopted"
    assert second["record"]["acquired_at"] == first["record"]["acquired_at"]
    assert run_lease(env, "status")["action"] == "active"


def test_reap_releases_after_deadline(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "standard")
    assert run_lease(env, "reap")["action"] == "kept"
    rec = record(env)
    rec["deadline"] = "2000-01-01T00:00:00+00:00"
    record_path(env).write_text(json.dumps(rec))
    assert run_lease(env, "reap")["action"] == "released-deadline"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_reap_clears_dead_session(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "standard")
    active = Path(env["FAKE_COLAB_STATE"]) / "sessions" / "standard" / "ACTIVE"
    active.unlink()
    assert run_lease(env, "reap")["action"] == "cleared-dead"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_release(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "standard")
    assert run_lease(env, "release")["action"] == "released"
    assert run_lease(env, "status")["action"] == "no-lease"


def dummy_home_env(env, tmp_path):
    """Point HOME at a scratch home with a dummy token, so the
    renewal kit's token staging never touches the real login."""
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    env["HOME"] = str(home)
    return env


def test_extend_moves_deadline_and_flags_renew(tmp_path):
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "standard", "--hours", "2")
    before = record(env)
    out = run_lease(env, "extend", "standard", "--hours", "1")
    assert out["action"] == "extended"
    # The delta is priced at the profile's rate: 0.08 x 1 h.
    assert out["budget"]["projected"] == 0.08
    after = record(env)
    delta = datetime.fromisoformat(after["deadline"]) - datetime.fromisoformat(before["deadline"])
    assert delta == timedelta(hours=1)
    assert after["renew"] is True
    assert after["session"] == "standard"  # the session is untouched


def test_extend_cli_rejects_bad_hours(tmp_path):
    env = lease_env(tmp_path)
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), "extend", "standard", "--hours", "0"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), "extend", "standard"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2


def test_reap_renews_window_lease_with_fresh_session(tmp_path):
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "standard", "--hours", "2")
    rec = record(env)
    now = datetime.now(timezone.utc)
    rec["renew"] = True
    rec["acquired_at"] = (now - timedelta(hours=12)).isoformat()  # past the threshold
    rec["deadline"] = (now + timedelta(hours=12)).isoformat()
    record_path(env).write_text(json.dumps(rec))
    out = run_lease(env, "reap")
    assert out["action"] == "renewed"
    assert out["driver"] == 2  # no kit in the fake world: reaper fallback
    after = record(env)
    assert after["session"].startswith("standard-r")
    assert after["deadline"] == rec["deadline"]  # the window end never moves
    sessions = Path(env["FAKE_COLAB_STATE"]) / "sessions"
    assert (sessions / after["session"] / "ACTIVE").exists()
    assert not (sessions / "standard" / "ACTIVE").exists()
