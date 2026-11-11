"""Lease lifecycle against the fake colab CLI (no login, no
network): acquire/adopt/status, the reaper's hard deadline,
manual release, extend, and window renewal — driven through
the CLI as a subprocess."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fake_harness import dummy_home_env, lease_bin, lease_env, record, record_path, run_lease  # noqa: F401


def test_acquire_adopt_status(tmp_path):
    env = lease_env(tmp_path)
    first = run_lease(env, "acquire", "--shape", "cpu")
    assert first["action"] == "created"
    assert first["keepalive"] == "launched"
    session = first["record"]["session"]
    sandbox_log = Path(env["FAKE_COLAB_STATE"]) / "sessions" / session / "keepalive.log"
    import time

    for _ in range(50):
        if sandbox_log.exists() and sandbox_log.read_text().strip():
            break
        time.sleep(0.2)
    assert sandbox_log.exists() and json.loads(sandbox_log.read_text().strip().split("\n")[-1])["cycle"] >= 1
    second = run_lease(env, "acquire", "--shape", "cpu")
    assert second["action"] == "adopted"
    assert second["record"]["acquired_at"] == first["record"]["acquired_at"]
    assert run_lease(env, "status")["action"] == "active"


def test_reap_releases_after_deadline(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "--shape", "cpu")
    assert run_lease(env, "reap")["action"] == "kept"
    rec = record(env)
    rec["deadline"] = "2000-01-01T00:00:00+00:00"
    record_path(env).write_text(json.dumps(rec))
    assert run_lease(env, "reap")["action"] == "released-deadline"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_reap_clears_dead_session(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "--shape", "cpu")
    active = Path(env["FAKE_COLAB_STATE"]) / "sessions" / out["record"]["session"] / "ACTIVE"
    active.unlink()
    assert run_lease(env, "reap")["action"] == "cleared-dead"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_release(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "--shape", "cpu")
    assert run_lease(env, "release")["action"] == "released"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_extend_moves_deadline_and_flags_renew(tmp_path):
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "--shape", "cpu", "--hours", "2")
    before = record(env)
    out = run_lease(env, "extend", before["session"], "--hours", "1")
    assert out["action"] == "extended"
    # The delta is priced at the shape's rate: 0.08 x 1 h.
    assert out["budget"]["projected"] == 0.08
    after = record(env)
    delta = datetime.fromisoformat(after["deadline"]) - datetime.fromisoformat(before["deadline"])
    assert delta == timedelta(hours=1)
    assert after["renew"] is True
    assert after["session"] == before["session"]  # the session is untouched


def test_extend_cli_rejects_bad_hours(tmp_path):
    env = lease_env(tmp_path)
    r = subprocess.run([sys.executable, str(lease_bin(env)), "extend", "some-session", "--hours", "0"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2
    r = subprocess.run([sys.executable, str(lease_bin(env)), "extend", "some-session"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2


def test_reap_renews_window_lease_with_fresh_session(tmp_path):
    # The subject shape is the High-RAM one: renewal re-checks
    # the shape with no actor bypass (session shape gate), so a
    # Standard window lease's renewal is refused by design — that
    # refusal is covered in the gate tests below.
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "--shape", "cpu-highram", "--hours", "2")
    rec_path = record_path(env)
    rec = json.loads(rec_path.read_text())
    parent = rec["session"]
    now = datetime.now(timezone.utc)
    rec["renew"] = True
    rec["acquired_at"] = (now - timedelta(hours=12)).isoformat()  # past the threshold
    rec["deadline"] = (now + timedelta(hours=12)).isoformat()
    rec_path.write_text(json.dumps(rec))
    out = run_lease(env, "reap")
    assert out["action"] == "renewed"
    assert out["driver"] == 2  # no kit in the fake world: reaper fallback
    after = json.loads(record_path(env).read_text())
    assert after["session"].startswith(parent + "-r")
    assert after["deadline"] == rec["deadline"]  # the window end never moves
    sessions = Path(env["FAKE_COLAB_STATE"]) / "sessions"
    assert (sessions / after["session"] / "ACTIVE").exists()
    assert not (sessions / parent / "ACTIVE").exists()
