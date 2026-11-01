"""Auto-acquire against the fake colab CLI (no login, no network).

The flag is driven through the COLAB_RUNNER_AUTO_ACQUIRE env override so
the tests never edit the tracked config (whose shipped default is off).
Covers: inert while disabled, inert with no demand, acquire + consume
on demand, satisfied-live + consume when a lease already exists, and a
budget refusal keeping the demand for a later poll.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"


def auto_env(tmp_path, enabled):
    return dict(
        os.environ,
        COLAB_BIN=str(FAKE),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        COLAB_RUNNER_AUTO_ACQUIRE="1" if enabled else "0",
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
    )


def run_lease(env, *args, expect_rc=0):
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == expect_rc, r.stdout + r.stderr
    return json.loads(r.stdout)


def request_file(env):
    return Path(env["COLAB_RUNNER_VAR"]) / "lease-request.json"


def test_auto_disabled_is_inert(tmp_path):
    env = auto_env(tmp_path, enabled=False)
    run_lease(env, "request", "standard", "--reason", "test")
    assert run_lease(env, "auto")["action"] == "auto-disabled"
    assert run_lease(env, "status")["action"] == "no-lease"
    assert request_file(env).exists()  # not consumed while disabled


def test_auto_no_demand_is_inert(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    assert run_lease(env, "auto")["action"] == "no-demand"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_auto_acquires_and_consumes(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    run_lease(env, "request", "standard", "--reason", "unit test demand")
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    assert out["session"] == "standard"
    assert out["reason"] == "unit test demand"
    assert not request_file(env).exists()
    assert run_lease(env, "status")["action"] == "active"


def test_auto_satisfied_live_consumes(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    run_lease(env, "acquire", "standard")
    run_lease(env, "request", "standard", "--reason", "already up")
    out = run_lease(env, "auto")
    assert out["action"] == "satisfied-live"
    assert not request_file(env).exists()


def test_auto_budget_refusal_keeps_demand(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    var = Path(env["COLAB_RUNNER_VAR"])
    var.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    # Attributed accounting: a journaled segment covering today so
    # far, rated to total (daily cap + 1) CU, puts attributed day
    # spend over the cap before the projected window cost is even
    # added. The cap is the operator's panel setting — read it from
    # the config rather than pinning the old 10 CU constant.
    daily_cap = json.loads((REPO / "config" / "lease.json").read_text())["daily_cu_budget"]
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max((now - day_start).total_seconds() / 3600.0, 1 / 60)
    seg = {"profile": "standard", "session": "seed", "start": day_start.isoformat(), "end": now.isoformat(), "rate": (daily_cap + 1) / hours}
    (var / "lease-spend-journal.jsonl").write_text(json.dumps(seg) + "\n")
    run_lease(env, "request", "standard", "--reason", "too expensive today")
    out = run_lease(env, "auto")
    assert out["action"] == "skipped-budget"
    assert request_file(env).exists()
    assert run_lease(env, "status")["action"] == "no-lease"


def test_auto_timed_start_via_cli(tmp_path):
    """`request --at` registers a timed start as data: an auto pass
    before the time leaves the demand untouched; once due (the stamp
    rewritten to the past here, standing in for time passing) the
    next pass fires it with no agent involved."""
    env = auto_env(tmp_path, enabled=True)
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    run_lease(env, "request", "standard", "--reason", "timed", "--at", future)
    out = run_lease(env, "auto")
    assert out["action"] == "not-due"
    assert request_file(env).exists()
    assert run_lease(env, "status")["action"] == "no-lease"
    req = json.loads(request_file(env).read_text())
    req["not_before"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    request_file(env).write_text(json.dumps(req))
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    assert not request_file(env).exists()


def test_unrequest(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    run_lease(env, "request", "standard")
    assert run_lease(env, "unrequest")["action"] == "request-cleared"
    assert run_lease(env, "auto")["action"] == "no-demand"
