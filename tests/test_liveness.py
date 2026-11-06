"""Session liveness: the checker (bin/colab-liveness) and the
derived scheduler entries (sync/liveness_entry.py).

The checker is driven as a subprocess against a fixture repo
so the tests exercise the shipped script end to end: local
fixtures for the lease record, the CLI binding, and the
approval-gate state, with every path seam pointed at the
fixture via the environment (the isolate-tests-from-live-
state rule: nothing here may touch the real registry, the
real binding, or the real gate state).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "bin" / "colab-liveness"

sys.path.insert(0, str(REPO / "sync"))
import liveness_entry as le  # noqa: E402

SESSION = "lilac-reef-93"
HOST = "8080-m-hm-test-aaaa.prod.r.colab.dev"


def iso(dt):
    return dt.isoformat()


def now():
    return datetime.now(timezone.utc)


class Fixture:
    def __init__(self, tmp_path):
        self.root = tmp_path / "runner"
        (self.root / "var").mkdir(parents=True)
        (self.root / "results" / "runs").mkdir(parents=True)
        self.jobs = tmp_path / "jobs"
        self.jobs.mkdir()
        self.bindings = tmp_path / "sessions.json"
        self.gate = tmp_path / "gate.json"
        self.env = {
            **os.environ,
            "COLAB_LIVENESS_JOBS_DIR": str(self.jobs),
            "COLAB_LIVENESS_BINDINGS": str(self.bindings),
            "COLAB_LIVENESS_GATE_STATE": str(self.gate),
            "COLAB_RUNNER_VAR": str(self.root / "var"),
        }

    def record(self, **over):
        rec = {
            "session": SESSION,
            "caller": "secforms",
            "shape": "cpu-highram",
            "host": HOST,
            "acquired_at": iso(now() - timedelta(hours=1)),
            "deadline": iso(now() + timedelta(hours=5)),
            "last_pulse_at": iso(now() - timedelta(minutes=5)),
            "last_pulse_result": "ok",
            "jobs": [],
        }
        rec.update(over)
        (self.root / "var" / f"lease-{SESSION}.json").write_text(json.dumps(rec))
        return rec

    def bind(self, host=HOST):
        self.bindings.write_text(json.dumps({SESSION: {"url": f"https://{host}/"}}))

    def gate_state(self, **over):
        meta = {"blocked": True, "contact_ok": False, "first_seen": iso(now() - timedelta(minutes=10)), "blocked_since": iso(now() - timedelta(minutes=9)), "last_probe": None}
        meta.update(over)
        self.gate.write_text(json.dumps({"hosts": {HOST: meta}}))

    def run(self, *args):
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            capture_output=True, text=True, env=self.env, timeout=60,
        )

    def state(self):
        return json.loads((self.root / "var" / f"liveness-{SESSION}.json").read_text())


def healthy(tmp_path):
    fx = Fixture(tmp_path)
    fx.record()
    fx.bind()
    fx.gate_state(contact_ok=True, blocked=False)
    return fx


def test_healthy_is_silent(tmp_path):
    fx = healthy(tmp_path)
    r = fx.run(SESSION)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert fx.state()["open"] == {}


def test_gone_session_self_disarms(tmp_path):
    fx = healthy(tmp_path)
    entry = fx.jobs / f"colab-liveness-{SESSION}.yaml"
    entry.write_text(le.render_entry(SESSION))
    (fx.root / "var" / f"lease-{SESSION}.json").unlink()
    r = fx.run(SESSION)
    assert r.returncode == 0
    assert not entry.exists(), "leaked entry must delete itself"
    assert not (fx.root / "var" / f"liveness-{SESSION}.json").exists()


def test_binding_missing_speaks_once(tmp_path):
    fx = healthy(tmp_path)
    fx.bindings.write_text("{}")
    r1 = fx.run(SESSION)
    assert r1.returncode == 3 and "binding-missing" in r1.stdout
    r2 = fx.run(SESSION)
    assert r2.returncode == 0 and r2.stdout == "", "a persisting finding must not re-speak"


def test_host_divergence(tmp_path):
    fx = healthy(tmp_path)
    fx.bind(host="8080-m-hm-test-zzzz.prod.r.colab.dev")
    r = fx.run(SESSION)
    assert r.returncode == 3 and "host-divergence" in r.stdout


def test_deadline_overdue(tmp_path):
    fx = healthy(tmp_path)
    fx.record(deadline=iso(now() - timedelta(minutes=30)))
    r = fx.run(SESSION)
    assert r.returncode == 3 and "deadline-overdue" in r.stdout


def test_pulse_failing_and_stale(tmp_path):
    fx = healthy(tmp_path)
    fx.record(last_pulse_result="failed")
    r = fx.run(SESSION)
    assert r.returncode == 3 and "pulse-failing" in r.stdout
    fx2 = healthy(tmp_path / "b")
    fx2.record(last_pulse_at=iso(now() - timedelta(hours=2)))
    r2 = fx2.run(SESSION)
    assert r2.returncode == 3 and "pulse-stale" in r2.stdout


def test_tonight_shape_approval_escalates_once_per_episode(tmp_path):
    """2026-11-05/06 lilac: gate blocked, pulse failing, jobs
    empty. The approval finding stays unspoken below the
    threshold, escalates exactly once past it, and the
    mechanical pulse finding speaks on its own first sight."""
    fx = Fixture(tmp_path)
    fx.record(last_pulse_result="failed", jobs=[])
    fx.bind()
    fx.gate_state(first_seen=iso(now() - timedelta(minutes=10)))
    r = fx.run(SESSION)
    assert r.returncode == 3 and "pulse-failing" in r.stdout
    assert "approval-outstanding" not in r.stdout, "below threshold: no escalation yet"
    # The blockage ages past the 30-minute threshold.
    fx.gate_state(first_seen=iso(now() - timedelta(minutes=40)),
                  blocked_since=iso(now() - timedelta(minutes=39)))
    r = fx.run(SESSION)
    assert r.returncode == 3 and "approval-outstanding" in r.stdout
    # Next ping: the episode is spent, the pulse finding is old.
    r = fx.run(SESSION)
    assert r.returncode == 0 and r.stdout == ""
    # Approval lands: contact proves, everything clears, once.
    fx.record(last_pulse_result="ok")
    fx.gate_state(contact_ok=True, blocked=False)
    r = fx.run(SESSION)
    assert r.returncode == 3 and "cleared" in r.stdout
    r = fx.run(SESSION)
    assert r.returncode == 0 and r.stdout == ""


def lease_cli_env(tmp_path):
    """The test_lease_fake harness shape: scratch HOME with a
    fake token, the fake CLI, a scratch var — plus the jobs
    seam pointed at a scratch registry so the lifecycle arms
    and disarms entries the test can inspect."""
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    (home / ".config" / "colab-cli" / "sessions.json").write_text("{}")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    env = dict(
        os.environ,
        HOME=str(home),
        COLAB_BIN=str(REPO / "tests" / "fixtures" / "fake_colab.py"),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        COLAB_LEASE_ACTOR="test",
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
        COLAB_LIVENESS_JOBS_DIR=str(jobs),
    )
    return env, jobs


def run_lease_cli(env, *args):
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args],
                       capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def test_render_entry_shape():
    text = le.render_entry(SESSION)
    assert f"id: colab-liveness-{SESSION}" in text
    assert "every: 15m" in text
    assert "escalate_body: bodies/colab-liveness.md" in text
    assert SESSION in text


def test_lease_lifecycle_arms_and_disarms(tmp_path):
    """Step 2's verify: acquire writes the session's entry via
    the record writers, release removes it — no seam calls in
    the test, the lifecycle itself does it."""
    env, jobs = lease_cli_env(tmp_path)
    out = run_lease_cli(env, "acquire", "--shape", "cpu")
    session = out["record"]["session"]
    entry = jobs / f"colab-liveness-{session}.yaml"
    assert entry.exists(), "acquire must arm the liveness entry"
    assert f"colab-liveness-{session}" in entry.read_text()
    run_lease_cli(env, "release")
    assert not entry.exists(), "release must retire the liveness entry"

    text = le.render_entry(SESSION)
    assert f"id: colab-liveness-{SESSION}" in text
    assert "every: 15m" in text
    assert "escalate_body: bodies/colab-liveness.md" in text
    assert SESSION in text


def test_arming_waits_for_the_escalate_body(tmp_path, monkeypatch):
    """Default-resolution arming refuses while the entry's
    shared escalate body is absent from the scheduler repo:
    an entry pointing at a missing body fails registry
    validation, so the body lands first or nothing arms."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("COLAB_LIVENESS_JOBS_DIR", raising=False)
    jobs = tmp_path / "home" / "workspace" / "scheduler" / "jobs"
    jobs.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rec = {"session": "body-guard-00"}
    assert le.sync_for_record(REPO, rec) == "unavailable"
    body = tmp_path / "home" / "workspace" / "scheduler" / "bodies" / "colab-liveness.md"
    body.parent.mkdir(parents=True)
    body.write_text("# body\n")
    assert le.sync_for_record(REPO, rec) == "written"
    assert (jobs / "colab-liveness-body-guard-00.yaml").exists()


def test_sync_and_reconcile(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    repo = tmp_path / "runner"
    (repo / "var").mkdir(parents=True)
    os.environ["COLAB_LIVENESS_JOBS_DIR"] = str(jobs)
    try:
        rec = {"session": SESSION, "caller": "secforms"}
        assert le.sync_for_record(repo, rec) == "written"
        assert le.sync_for_record(repo, rec) == "present", "refresh must not churn identical content"
        orphan = jobs / "colab-liveness-ghost-00.yaml"
        orphan.write_text(le.render_entry("ghost-00"))
        out = le.reconcile(repo, [rec])
        assert out["removed"] == ["ghost-00"] and not orphan.exists()
        assert le.remove_for_session(repo, SESSION) == "removed"
        assert not (jobs / f"colab-liveness-{SESSION}.yaml").exists()
        out = le.reconcile(repo, [rec])
        assert out["armed"] == [SESSION], "reconcile re-arms a live session whose entry was deleted"
    finally:
        del os.environ["COLAB_LIVENESS_JOBS_DIR"]
