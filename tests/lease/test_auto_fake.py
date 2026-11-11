"""Auto-acquire against the fake colab CLI (no login, no network).

The flag is driven through the COLAB_RUNNER_AUTO_ACQUIRE env override so
the tests never edit the tracked config (whose shipped default is off).
Covers: inert while disabled, inert with no demand, acquire + consume
on demand, satisfied-live + consume when a lease already exists, and a
budget refusal keeping the demand for a later poll.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import repo_fixture  # noqa: E402
from env_fixtures import auto_env  # noqa: E402, F401


def run_lease(env, *args, expect_rc=0):
    r = subprocess.run([sys.executable, str(Path(env["TEST_REPO_ROOT"]) / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == expect_rc, r.stdout + r.stderr
    return json.loads(r.stdout)


def request_file(env):
    return Path(env["COLAB_RUNNER_VAR"]) / "lease-request.json"


def test_auto_disabled_is_inert(tmp_path):
    env = auto_env(tmp_path, enabled=False)
    run_lease(env, "request", "--shape", "cpu", "--reason", "test")
    assert run_lease(env, "auto")["action"] == "auto-disabled"
    assert run_lease(env, "status")["action"] == "no-lease"
    assert request_file(env).exists()  # not consumed while disabled


def test_auto_no_demand_is_inert(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    assert run_lease(env, "auto")["action"] == "no-demand"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_auto_acquires_and_consumes(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    run_lease(env, "request", "--shape", "cpu", "--reason", "unit test demand")
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    assert out["reason"] == "unit test demand"
    assert out["session"] == run_lease(env, "status")["record"]["session"]
    assert not request_file(env).exists()
    assert run_lease(env, "status")["action"] == "active"


def test_auto_satisfied_live_consumes(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    run_lease(env, "acquire", "--shape", "cpu")
    run_lease(env, "request", "--shape", "cpu", "--reason", "already up")
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
    # added. The cap is the fixture repo's authored value (see
    # tests/fixtures/repo_fixture.py), not the operator's.
    daily_cap = repo_fixture.FIXTURE_DAILY_CAP
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max((now - day_start).total_seconds() / 3600.0, 1 / 60)
    seg = {"shape": "cpu", "session": "seed", "start": day_start.isoformat(), "end": now.isoformat(), "rate": (daily_cap + 1) / hours}
    (var / "lease-spend-journal.jsonl").write_text(json.dumps(seg) + "\n")
    run_lease(env, "request", "--shape", "cpu", "--reason", "too expensive today")
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
    run_lease(env, "request", "--shape", "cpu", "--reason", "timed", "--at", future)
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
    run_lease(env, "request", "--shape", "cpu")
    assert run_lease(env, "unrequest")["action"] == "request-cleared"
    assert run_lease(env, "auto")["action"] == "no-demand"


# --- Session shape gate (plan: .plans/2026-11-04-agent-session-shape-gate.md) --
# These tests pop the suite's test actor claim so the demand path
# runs as a plain agent actor, exactly as a thread's demand does.


def test_request_refused_at_write_for_agent_standard(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    env.pop("COLAB_LEASE_ACTOR", None)
    out = run_lease(env, "request", "--shape", "cpu", "--reason", "burst", "--caller", "test", expect_rc=1)
    assert out["action"] == "shape-refused"
    assert out["shape_class"] == "standard"
    assert not request_file(env).exists()  # a refused demand is never recorded


def test_auto_shape_refusal_clears_demand(tmp_path):
    env = auto_env(tmp_path, enabled=True)
    env.pop("COLAB_LEASE_ACTOR", None)
    # No via: the write defers, and consumption refuses + clears,
    # so the duty never retries a permanently refused demand.
    run_lease(env, "request", "--shape", "cpu", "--reason", "unattributed")
    out = run_lease(env, "auto")
    assert out["action"] == "skipped-shape"
    assert not request_file(env).exists()
    assert run_lease(env, "status")["action"] == "no-lease"
    journal = Path(env["COLAB_RUNNER_VAR"]) / "session-gate-journal.jsonl"
    kinds = [json.loads(line)["kind"] for line in journal.read_text().splitlines() if line.strip()]
    assert kinds == ["shape-refused", "demand-shape-refused"]
