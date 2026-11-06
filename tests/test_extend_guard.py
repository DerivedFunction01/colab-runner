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
