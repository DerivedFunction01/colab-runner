"""Auto-extension guard: every live lease is extended before it
lapses (sync/extend_guard.py, driven by bin/colab-extend-guard
on a scheduler cadence).

In-process against the lease fake harness (scratch repo + fake
CLI from test_lease_fake): a lease far from its deadline is left
alone, a lease inside the threshold is extended by a full window
through the core's extend(), a standing lease is skipped (its
lifetime belongs to the grant), and a refusal surfaces as an
exit-3 finding, episode-tracked so it speaks once.
"""

import importlib.util
import json
import sys
from datetime import timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "sync"))
import extend_guard as guard_mod  # noqa: E402

spec = importlib.util.spec_from_loader("lease_fake_harness", SourceFileLoader("lease_fake_harness", str(REPO / "tests" / "test_lease_fake.py")))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
lease_mod = harness.lease_mod
FakeColab = harness.FakeColab
scratch_repo = harness.scratch_repo
PNOW = harness.PNOW


def test_guard_leaves_a_lease_far_from_its_deadline_alone(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    a = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    assert a["action"] == "created"
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW + timedelta(hours=1))
    assert code == 0 and lines == []


def test_guard_extends_a_lease_inside_the_threshold(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    a = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    assert a["action"] == "created"
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW + timedelta(hours=7))
    assert code == 0
    session = lease_mod.load_record(repo)["session"]
    assert len(lines) == 1 and lines[0]["action"] == "extended" and lines[0]["session"] == session
    rec = lease_mod.load_record(repo, session)
    assert rec["deadline"] == lines[0]["new_deadline"] and rec["renew"] is True
    # And the next pass is quiet: the new deadline is far away.
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW + timedelta(hours=7, minutes=5))
    assert code == 0 and lines == []


def test_guard_skips_a_standing_lease(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    rec = {"session": "standing-lease-1", "shape": "cpu", "caller": "standing", "standing": True, "deadline": (PNOW + timedelta(minutes=30)).isoformat(), "acquired_at": PNOW.isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []}
    (repo / "var" / "lease-standing-lease-1.json").write_text(json.dumps(rec))
    code, lines = guard_mod.run_guard(lease_mod, FakeColab(), repo, now_dt=PNOW)
    assert code == 0 and lines == []
    assert json.loads((repo / "var" / "lease-standing-lease-1.json").read_text())["deadline"] == rec["deadline"]


def test_guard_reports_a_refusal_once_per_episode(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    rec = {"session": "gone-lease-1", "shape": "cpu", "caller": "alpha", "deadline": (PNOW + timedelta(minutes=30)).isoformat(), "acquired_at": PNOW.isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []}
    (repo / "var" / "lease-gone-lease-1.json").write_text(json.dumps(rec))
    fake = FakeColab()
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW)
    assert code == 3 and len(lines) == 1
    assert lines[0]["finding"] is True and lines[0]["episode_first"] is True and lines[0]["action"] == "extend-no-lease"
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW + timedelta(minutes=30))
    assert code == 3 and lines[0]["episode_first"] is False
    state = json.loads((repo / "var" / "extend-guard-state.json").read_text())
    assert state["episodes"]["gone-lease-1"]["action"] == "extend-no-lease"


# --- policy loader (plan step 1) ---


def _policy(**overrides):
    policy = dict(guard_mod.POLICY_DEFAULTS)
    policy["shapes"] = {}
    policy["callers"] = {}
    policy.update(overrides)
    return policy


def test_policy_defaults_when_the_block_is_absent(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    policy, findings = guard_mod.load_policy(lease_mod.load_config(repo).get("extend_guard"))
    assert findings == []
    assert policy["default_mode"] == "active-or-first" and policy["max_extended_per_shape"] == 1
    assert policy["threshold_minutes"] == 120 and policy["idle_after_minutes"] == 60


def test_policy_unknown_mode_fails_closed(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    cfg_path = repo / "config" / "lease.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["extend_guard"] = {"shapes": {"cpu": "sometimes"}, "callers": {"alpha": "active-only"}}
    cfg_path.write_text(json.dumps(cfg))
    policy, findings = guard_mod.load_policy(lease_mod.load_config(repo).get("extend_guard"))
    assert policy["shapes"]["cpu"] == "never" and policy["callers"]["alpha"] == "active-only"
    assert len(findings) == 1 and findings[0]["action"] == "policy-unknown-mode"


# --- decision procedure (plan step 2) ---


def _craft(repo, session, *, last_used_h, deadline_m=30, jobs=None, caller="alpha"):
    rec = {"session": session, "shape": "cpu", "caller": caller, "deadline": (PNOW + timedelta(minutes=deadline_m)).isoformat(), "acquired_at": (PNOW - timedelta(hours=8)).isoformat(), "last_used_at": (PNOW - timedelta(hours=last_used_h)).isoformat(), "jobs": jobs or []}
    (repo / "var" / f"lease-{session}.json").write_text(json.dumps(rec))
    return rec


def test_guard_extends_only_the_warmest_idle_twin(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    _craft(repo, "twin-warm-1", last_used_h=3)
    _craft(repo, "twin-cold-1", last_used_h=5)
    fake = FakeColab(alive={"twin-warm-1", "twin-cold-1"})
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW)
    assert code == 0
    by_session = {line["session"]: line["action"] for line in lines}
    assert by_session == {"twin-warm-1": "extended", "twin-cold-1": "skipped-count"}


def test_guard_never_mode_skips_even_an_active_lease(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    _craft(repo, "busy-lease-1", last_used_h=0, jobs=["run-9"])
    fake = FakeColab(alive={"busy-lease-1"})
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW, policy=_policy(shapes={"cpu": "never"}))
    assert code == 0 and lines == [{"session": "busy-lease-1", "action": "skipped-shape", "decision": True}]
    assert "renew" not in lease_mod.load_record(repo, "busy-lease-1")  # untouched: extend() flags renew when it acts


def test_guard_active_lease_extends_while_idle_twin_is_capped_out(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    _craft(repo, "jobbed-lease-1", last_used_h=5, jobs=["run-9"])
    _craft(repo, "idle-lease-1", last_used_h=3)
    fake = FakeColab(alive={"jobbed-lease-1", "idle-lease-1"})
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW, policy=_policy(max_extended_per_shape=0))
    assert code == 0
    by_session = {line["session"]: line["action"] for line in lines}
    assert by_session == {"jobbed-lease-1": "extended", "idle-lease-1": "skipped-count"}


def test_guard_does_not_re_extend_an_idle_lease_it_already_extended(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    _craft(repo, "idle-lease-1", last_used_h=5)
    fake = FakeColab(alive={"idle-lease-1"})
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW)
    assert code == 0 and lines[0]["action"] == "extended"
    deadline_after_first = lease_mod.load_record(repo, "idle-lease-1")["deadline"]
    # Eight hours on: due again, still idle at the same stamp.
    code, lines = guard_mod.run_guard(lease_mod, fake, repo, now_dt=PNOW + timedelta(hours=8))
    assert code == 0 and lines == [{"session": "idle-lease-1", "action": "skipped-idle", "decision": True}]
    assert lease_mod.load_record(repo, "idle-lease-1")["deadline"] == deadline_after_first
