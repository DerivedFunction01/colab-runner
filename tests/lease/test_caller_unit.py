"""Caller attribution unit tests (plan: .plans/2026-11-04-caller-
attribution.md, Step 1): the registry, the claim resolver, and the
claims journal, against a scratch repo whose config carries its own
fictional registry — the machine's local registry never leaks in."""

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

import sys
_spec = importlib.util.spec_from_file_location(
    "lease_mod", REPO / "sync" / "lease" / "__init__.py",
    submodule_search_locations=[str(REPO / "sync" / "lease")])
lease_mod = importlib.util.module_from_spec(_spec)
sys.modules["lease_mod"] = lease_mod
_spec.loader.exec_module(lease_mod)


def caller_repo(tmp_path, monkeypatch, callers=("alpha", "beta"), gate=None):
    """A scratch repo: minimal profiles + lease config, and the
    caller registry under test (callers=None writes no file)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "profiles.json").write_text(json.dumps({
        "default_profile": "main",
        "profiles": {
            "main": {"label": "Main", "high_mem": True, "rate_cu_per_hour": 0.26, "session": "main", "timeout": 18000},
            "plain": {"label": "Plain", "high_mem": False, "rate_cu_per_hour": 0.08, "session": "plain", "timeout": 3600},
        },
    }))
    (cfg_dir / "lease.json").write_text(json.dumps({
        "enabled": True, "daily_cu_budget": 15, "monthly_cu_budget": 150, "max_hours": 8,
        "session_gate": gate if gate is not None else {"enabled": True, "bypass_actors": ["panel", "test"], "agent_classes": ["high_ram"], "infra_actor": "standing"},
    }, sort_keys=True, separators=(",", ":")))
    if callers is not None:
        (cfg_dir / "callers.json").write_text(json.dumps({"callers": list(callers)}))
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COLAB_LEASE_ACTOR", raising=False)
    return tmp_path


def test_flag_beats_env(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    assert lease_mod.resolve_caller(repo, flag="alpha", env="beta") == "alpha"
    assert lease_mod.resolve_caller(repo, flag=None, env="beta") == "beta"
    assert lease_mod.resolve_caller(repo, flag="alpha", env=None) == "alpha"


def test_no_claim_resolves_none_without_error(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    assert lease_mod.resolve_caller(repo) is None
    assert lease_mod.resolve_caller(repo, flag=None, env=None) is None
    assert lease_mod.caller_claim_error(repo) is None


def test_lane_suffix_validates_against_the_base(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    assert lease_mod.resolve_caller(repo, flag="alpha:lane-1") == "alpha:lane-1"
    assert lease_mod.resolve_caller(repo, flag="alpha:") is None
    assert lease_mod.resolve_caller(repo, flag="alpha:lane:two") is None
    assert lease_mod.resolve_caller(repo, flag="gamma:lane-1") is None


def test_unregistered_caller_is_refused_at_claim_time(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    assert lease_mod.resolve_caller(repo, flag="gamma") is None
    err = lease_mod.caller_claim_error(repo, flag="gamma")
    assert err is not None and "gamma" in err
    # A malformed shape is a bad claim too, not an absent one.
    assert lease_mod.caller_claim_error(repo, flag="Not A Caller") is not None
    assert lease_mod.caller_claim_error(repo, flag="alpha") is None


def test_machinery_callers_are_always_valid(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch, callers=())
    for name in ("panel", "standing", "test", "lease"):
        assert lease_mod.resolve_caller(repo, flag=name) == name
    assert lease_mod.resolve_caller(repo, flag="panel:ops") == "panel:ops"


def test_registry_absent_fails_closed_without_crashing(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch, callers=None)
    assert lease_mod.resolve_caller(repo, flag="alpha") is None
    assert lease_mod.caller_claim_error(repo, flag="alpha") is not None
    # The machinery set survives a missing registry untouched.
    assert lease_mod.resolve_caller(repo, flag="panel") == "panel"
    assert lease_mod.caller_registry(repo) == set(lease_mod.MACHINERY_CALLERS)


def test_registry_reads_fresh_and_ignores_malformed_entries(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch, callers=("alpha",))
    (repo / "config" / "callers.json").write_text(json.dumps({"callers": ["beta", 7, "", "bad name", None]}))
    assert lease_mod.resolve_caller(repo, flag="alpha") is None
    assert lease_mod.resolve_caller(repo, flag="beta") == "beta"


def test_active_marking_reads_the_registry(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch, callers=("alpha", "beta"))
    (repo / "config" / "callers.json").write_text(json.dumps({"callers": ["alpha", "beta"], "active": ["alpha"]}))
    assert lease_mod.active_caller_registry(repo) == {"alpha"}
    assert lease_mod.is_active_caller(repo, "alpha") is True
    # A lane suffix inherits its base's marking.
    assert lease_mod.is_active_caller(repo, "alpha:lane-1") is True
    assert lease_mod.is_active_caller(repo, "beta") is False
    assert lease_mod.is_active_caller(repo, "beta:lane-1") is False
    # An invalid or unregistered claim is never active.
    assert lease_mod.is_active_caller(repo, "gamma") is False
    assert lease_mod.is_active_caller(repo, None) is False


def test_active_marking_ignores_unregistered_names_and_absence(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch, callers=("alpha", "beta"))
    # A marking for an unregistered caller is ignored, not an error.
    (repo / "config" / "callers.json").write_text(json.dumps({"callers": ["alpha", "beta"], "active": ["gamma", 7, None]}))
    assert lease_mod.active_caller_registry(repo) == set()
    # No "active" list at all marks nobody.
    (repo / "config" / "callers.json").write_text(json.dumps({"callers": ["alpha", "beta"]}))
    assert lease_mod.active_caller_registry(repo) == set()
    assert lease_mod.is_active_caller(repo, "alpha") is False


def test_claims_journal_round_trip(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    assert lease_mod.load_caller_claims(repo) == {}
    assert lease_mod.append_caller_claim(repo, "sess-a", "alpha", "colab-sh")
    assert lease_mod.append_caller_claim(repo, "sess-a", None, "colab-sh")
    assert lease_mod.append_caller_claim(repo, "sess-b", "beta:lane-2", "colab-sh")
    claims = lease_mod.load_caller_claims(repo)
    # The unattributed attach does not erase the earlier attribution;
    # the latest attributed claim per session wins.
    assert claims["sess-a"]["caller"] == "alpha"
    assert claims["sess-b"]["caller"] == "beta:lane-2"
    assert claims["sess-a"]["ts"]


def test_gate_journal_events_carry_the_caller(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    verdict = lease_mod.enforce_session_admission(repo, "plain", caller="alpha")
    assert verdict is not None and verdict["action"] == "shape-refused"
    assert verdict["caller"] == "alpha"
    journal = [json.loads(line) for line in (repo / "var" / "session-gate-journal.jsonl").read_text().splitlines() if line.strip()]
    assert [e["kind"] for e in journal] == ["shape-refused"]
    assert journal[0]["caller"] == "alpha"
    # A bypass admission journals its caller the same way.
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "panel")
    assert lease_mod.enforce_session_admission(repo, "plain", via="panel", caller="panel") is None
    journal = [json.loads(line) for line in (repo / "var" / "session-gate-journal.jsonl").read_text().splitlines() if line.strip()]
    assert journal[-1]["kind"] == "shape-bypass" and journal[-1]["caller"] == "panel"


def _seed_binding(repo, session, host_part="m-hm-zz9"):
    binding = repo / "home" / ".config" / "colab-cli" / "sessions.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(json.dumps({session: {"endpoint": f"https://{host_part}-fake.prod.r.colab.dev"}}))


def test_approval_event_names_the_caller_when_the_record_carries_one(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    _seed_binding(repo, "sess-a")
    rec = {"profile": "main", "session": "sess-a", "caller": "alpha"}
    assert lease_mod._capture_session_host(repo, rec, "acquire", spool_on_first=True)
    events = [json.loads(line) for line in (repo / "var" / "reaper-events.jsonl").read_text().splitlines() if line.strip()]
    assert events[0]["kind"] == "approval-needed" and events[0]["caller"] == "alpha"


def test_approval_event_omits_caller_for_an_unattributed_record(tmp_path, monkeypatch):
    repo = caller_repo(tmp_path, monkeypatch)
    _seed_binding(repo, "sess-b")
    rec = {"profile": "main", "session": "sess-b"}
    assert lease_mod._capture_session_host(repo, rec, "acquire", spool_on_first=True)
    events = [json.loads(line) for line in (repo / "var" / "reaper-events.jsonl").read_text().splitlines() if line.strip()]
    assert events[0]["kind"] == "approval-needed" and "caller" not in events[0]
