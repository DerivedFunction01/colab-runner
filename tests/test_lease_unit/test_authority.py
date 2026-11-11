"""Lease unit tests: caller creation authority — the
session shape gate's admission matrix, with stub colab
callables (no subprocess)."""

import json

from unit_harness import gate_permissive, lease, repo  # noqa: F401

# --- Caller creation authority (caller-attribution plan, Step 3) -----
# The refusal matrix over enforce_session_admission: past the shape
# check, an agent-class actor's fresh creation is refused on any
# shape; the panel's two halves, a bypass claim, the infra
# exemption, the duty marker, a non-creation consult, and a
# disabled gate each admit. The gate policy is pinned per test;
# the journal lands in the scratched var dir.

HIGH_RAM = {"high_mem": True}
STANDARD = {"high_mem": False}


def _authority_fixture(tmp_path, monkeypatch, enabled=True):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setattr(lease.gate, "session_gate_policy", lambda repo: {"enabled": enabled, "agent_classes": ["high_ram"], "bypass_actors": ["panel", "test"], "infra_actor": "standing"})
    monkeypatch.setattr(lease.foundation, "load_config", lambda repo: {"standing": {"shape": "cpu"}})
    monkeypatch.delenv("COLAB_LEASE_ACTOR", raising=False)


def _gate_journal(tmp_path):
    p = tmp_path / "var" / "session-gate-journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_authority_refuses_agent_creation_and_journals(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    out = lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha", seam="acquire")
    assert out is not None and out["action"] == "caller-start-refused"
    assert out["basis"] == "agent-creation-denied" and out["caller"] == "alpha"
    journal = _gate_journal(tmp_path)
    assert [e["kind"] for e in journal] == ["caller-start-refused"]
    assert journal[0]["caller"] == "alpha" and journal[0]["seam"] == "acquire"


def test_authority_shape_refusal_precedes_it(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    out = lease.enforce_session_admission(repo, "plain", shape=STANDARD, caller="alpha")
    assert out["action"] == "shape-refused"
    assert [e["kind"] for e in _gate_journal(tmp_path)] == ["shape-refused"]


def test_authority_non_creation_consult_and_duty_admit(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha", creation=False) is None
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha", duty=True) is None
    assert _gate_journal(tmp_path) == []


def test_authority_panel_two_halves_and_bypass_claim_admit(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "panel")
    # One half alone is not authority: the panel env claim without
    # its via corroboration is refused like any agent.
    out = lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="panel")
    assert out is not None and out["action"] == "caller-start-refused"
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, via="panel", caller="panel") is None
    # A whitelisted non-panel claim bypasses on the env half alone.
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "test")
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM) is None


def test_authority_infra_exemption_and_disabled_gate_admit(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    assert lease.enforce_session_admission(repo, "cpu", shape=STANDARD, via="standing", standing=True) is None
    assert [e["kind"] for e in _gate_journal(tmp_path)] == ["shape-infra"]
    _authority_fixture(tmp_path, monkeypatch, enabled=False)
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha") is None


def test_state_serves_extend_guard_policy_and_verdicts(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lease.json").write_text(json.dumps({"enabled": True, "extend_guard": {"shapes": {"a100-highram": "active-only"}}}))
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "extend-guard-state.json").write_text(json.dumps({"episodes": {}, "verdicts": {"busy-lease-1": {"action": "extended", "at": "2026-11-06T00:00:00+00:00"}}}))
    out = lease.state_extend_guard(tmp_path)
    assert out["extend_guard"]["policy"]["shapes"] == {"a100-highram": "active-only"}
    assert out["extend_guard"]["policy"]["default_mode"] == "active-or-first"
    assert out["extend_guard"]["verdicts"]["busy-lease-1"]["action"] == "extended"
