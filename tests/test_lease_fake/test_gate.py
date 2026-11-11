"""Session shape gate against the fake colab CLI and in
process (under the enforcing gate_repo fixture): the refusal
matrix, the panel bypass's two halves, the test claim, the
standing exemption, and the CLI exit codes."""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from fake_harness import PNOW, FakeColab, _seed_cli_catalog, gate_journal, gate_repo, lease_bin, lease_env, lease_mod, record_files, run_lease  # noqa: F401

# --- Session shape gate (plan: .plans/2026-11-04-agent-session-shape-gate.md) --
# The gate's own tests run in-process under an ENFORCING fixture
# config (gate_repo), unlike the general suite's permissive doors.
# One CLI test pins the refusal's exit code and the panel bypass
# against the live config.


def test_gate_refuses_agent_on_standard_and_gpu(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    assert out["shape_class"] == "standard" and out["actor"] == "agent"
    out = lease_mod.acquire(fake, repo, "t4", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    assert out["shape_class"] == "gpu"
    # No creation was ever attempted for a refused pair.
    assert not [c for c in fake.calls if c[0] == "new" and "-s" in c]
    assert lease_mod.all_records(repo) == []
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-refused", "shape-refused"]
    assert [e["shape"] for e in journal] == ["cpu", "t4"]
    assert all(e["seam"] == "acquire" for e in journal)


def test_gate_agent_high_ram_is_now_a_caller_start_refusal(tmp_path, monkeypatch):
    """Caller attribution reversed the old agent-creation allowance:
    an agent-class actor may no longer create even a High-RAM
    session — creation belongs to the panel, the standing duty,
    and the lease duty consuming demands. The refusal names the
    caller and the seam, attempts no creation, and leaves no record."""
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "cpu-highram", now_dt=PNOW, caller="alpha")
    assert out["action"] == "caller-start-refused"
    assert out["shape_class"] == "high_ram" and out["caller"] == "alpha"
    assert out["basis"] == "agent-creation-denied"
    assert not [c for c in fake.calls if c[0] == "new" and "-s" in c]
    assert lease_mod.all_records(repo) == []
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["caller-start-refused"]
    assert journal[0]["caller"] == "alpha" and journal[0]["seam"] == "acquire"
    # The duty exemption: the same creation on a demand passes.
    out = lease_mod.acquire(fake, repo, "cpu-highram", now_dt=PNOW, caller="alpha", duty=True)
    assert out["action"] == "created"
    assert out["record"]["caller"] == "alpha"


def test_gate_panel_bypass_needs_via_and_env(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    # The env claim alone, without the panel via, is not a bypass.
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "panel")
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    # Both halves together bypass, and the bypass is journaled.
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, via="panel")
    assert out["action"] == "created"
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-refused", "shape-bypass"]
    assert journal[1]["via"] == "panel" and journal[1]["env_actor"] == "panel"


def test_gate_via_panel_alone_is_not_a_bypass(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    out = lease_mod.acquire(FakeColab(), repo, "cpu", now_dt=PNOW, via="panel")
    assert out["action"] == "shape-refused"


def test_gate_test_claim_bypasses_and_journals(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "test")
    out = lease_mod.acquire(FakeColab(), repo, "cpu", now_dt=PNOW)
    assert out["action"] == "created"
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-bypass"]
    assert journal[0]["env_actor"] == "test"


def test_gate_standing_exemption_is_exact(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    # The infra actor admitting the standing duty's own lease: the
    # gate fixture's standing shape is plain CPU, and the exemption
    # needs the duty's standing marker as well as the via.
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, via="standing", standing=True)
    assert out["action"] == "created"
    assert [e["kind"] for e in gate_journal(repo)] == ["shape-infra"]
    # The same actor is gated like any agent on any other shape.
    out = lease_mod.acquire(fake, repo, "t4", now_dt=PNOW, via="standing")
    assert out["action"] == "shape-refused"


def test_gate_disabled_opens_everything(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch, gate={"enabled": False})
    out = lease_mod.acquire(FakeColab(), repo, "cpu", now_dt=PNOW)
    assert out["action"] == "created"
    assert gate_journal(repo) == []


def test_gate_absent_block_enforces_defaults(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch, gate=None)
    # Rewrite the fixture config without the block at all: the
    # built-in defaults enforce (fail-closed).
    cfg_path = repo / "config" / "lease.json"
    cfg = json.loads(cfg_path.read_text())
    del cfg["session_gate"]
    cfg_path.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    out = lease_mod.acquire(FakeColab(), repo, "cpu", now_dt=PNOW)
    assert out["action"] == "shape-refused"


def test_gate_demand_refusal_clears_and_journals(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    fake = FakeColab()
    # An attributed demand for a blocked shape is refused at write.
    out = lease_mod.write_request(repo, reason="burst", shape="cpu", caller="alpha", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    assert lease_mod.load_request(repo) is None
    # An unattributed demand defers to consumption, which refuses,
    # clears the demand, and journals the disposition.
    out = lease_mod.write_request(repo, reason="burst", shape="cpu", now_dt=PNOW)
    assert out["action"] == "requested"
    out = lease_mod.auto_acquire(fake, repo, now_dt=PNOW)
    assert out["action"] == "skipped-shape"
    assert lease_mod.load_request(repo) is None
    assert lease_mod.all_records(repo) == []
    kinds = [e["kind"] for e in gate_journal(repo)]
    assert kinds == ["shape-refused", "shape-refused", "demand-shape-refused"]


def test_gate_renewal_refuses_standard_window(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()  # nothing alive: both sessions read absent
    for shape, session in (("cpu", "standard"), ("cpu-highram", "alpha")):
        lease_mod.save_record(repo, {"shape": shape, "session": session, "acquired_at": (PNOW - timedelta(hours=12)).isoformat(), "deadline": (PNOW + timedelta(hours=12)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": [], "renew": True})
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    # The Standard window is not recreated; its record is kept and
    # the reap surfaces renewal-failed. The High-RAM window renews
    # (its result names the new session the record swapped onto).
    by_session = {r["session"]: r["action"] for r in out["records"]}
    assert by_session["standard"] == "renewal-failed"
    renewed = [r for r in out["records"] if r["action"] == "renewed"]
    assert len(renewed) == 1 and renewed[0]["new_session"].startswith("alpha-r")
    assert lease_mod.load_record(repo, "standard")["session"] == "standard"
    assert lease_mod.load_record(repo, "alpha") is None
    assert lease_mod.load_record(repo)["session"].startswith("alpha-r")
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-refused"]
    assert journal[0]["seam"] == "renewal" and journal[0]["shape"] == "cpu"


def test_gate_cli_refusal_exit_code_and_panel_bypass(tmp_path):
    """Against the live config: an agent actor (no claim in env) is
    refused with exit 1 on Standard and GPU shapes; the panel's
    two halves together start the Standard shape."""
    env = lease_env(tmp_path)
    env.pop("COLAB_LEASE_ACTOR", None)
    _seed_cli_catalog(env)

    def run(*args):
        return subprocess.run([sys.executable, str(lease_bin(env)), *args], capture_output=True, text=True, env=env, timeout=300)

    r = run("acquire", "--shape", "cpu")
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["action"] == "shape-refused" and out["shape_class"] == "standard"
    r = run("acquire", "--shape", "t4")
    assert r.returncode == 1
    assert json.loads(r.stdout)["shape_class"] == "gpu"
    assert record_files(env) == []
    # A bare --via panel, still without the env claim, is refused.
    r = run("acquire", "--shape", "cpu", "--via", "panel")
    assert r.returncode == 1
    assert json.loads(r.stdout)["action"] == "shape-refused"
    # Both halves: created.
    env["COLAB_LEASE_ACTOR"] = "panel"
    r = run("acquire", "--shape", "cpu", "--via", "panel")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["action"] == "created"


def test_gate_post_start_shape_mismatch_stops_and_refuses(tmp_path):
    """Step 5: the binding the fake CLI records says the fresh
    High-RAM session came up on a Standard endpoint (m-s-) — the
    acquire stops it, writes no lease record, and reports
    shape-mismatch. The agreeing class lets the next acquire
    through untouched."""
    env = lease_env(tmp_path)
    env["FAKE_COLAB_FORCE_CLASS"] = "s"
    r = subprocess.run([sys.executable, str(lease_bin(env)), "acquire", "--shape", "cpu-highram"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["action"] == "shape-mismatch"
    assert out["actual_class"] == "standard" and out["declared_high_mem"] is True
    assert out["stop"] == "stopped"
    assert record_files(env) == []
    sessions = Path(env["FAKE_COLAB_STATE"]) / "sessions"
    assert not (sessions / out["session"] / "ACTIVE").exists()
    journal = [json.loads(line) for line in (Path(env["COLAB_RUNNER_VAR"]) / "session-gate-journal.jsonl").read_text().splitlines() if line.strip()]
    assert [e["kind"] for e in journal] == ["shape-mismatch"]
    assert journal[0]["seam"] == "acquire" and journal[0]["shape"] == "cpu-highram"
    env.pop("FAKE_COLAB_FORCE_CLASS")
    out = run_lease(env, "acquire", "--shape", "cpu-highram")
    assert out["action"] == "created"
    assert len(record_files(env)) == 1
