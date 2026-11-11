"""Two-tier pricing unit tests with stub colab callables (no subprocess).

Pins the shape-catalog pricing rules: a shape's measured
rate_cu_per_hour prices its acquires, and an unconfirmed (null)
rate passes the budget check with rate_unconfirmed and a null
projection; no number is ever fabricated for such a shape. The
extension tests pin the two-tier edge cases from the
lease-extension plan: grant extension prices the standing delta
only, burst extension is independent of the grant, direct standing
extension is refused, and an expired grant refuses extension.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
import sys

sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import repo_fixture  # noqa: E402
from module_loader import load_lease  # noqa: E402

lease = load_lease()

import pytest


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A scratch repo carrying the authored fixture config (see
    tests/fixtures/repo_fixture.py): the suite must pass in a
    bare clone, where the checkout has no operator config."""
    return repo_fixture.lease_repo(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def gate_permissive(monkeypatch):
    """Session shape gate: permissive fixture state, declared
    explicitly (see test_lease_unit.gate_permissive)."""
    monkeypatch.setattr(lease.gate, "session_gate_policy", lambda repo: {"enabled": False, "agent_classes": ["high_ram"], "bypass_actors": ["panel"], "infra_actor": "standing"})


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def make_stub(alive):
    def stub(*a, **k):
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(sorted(alive)))
        if cmd == "new":
            alive.add(a[a.index("-s") + 1])
            return cp("created")
        if cmd == "stop":
            alive.discard(a[a.index("-s") + 1])
            return cp("stopped")
        if cmd == "download":
            return cp("", rc=1)  # no heartbeat file in the stub world
        return cp("")

    return stub


def seed_cli_catalog(tmp_path):
    """Seed the scratch var's CLI catalog cache with one GPU shape
    whose rate is unconfirmed (no measured rate is seeded for it):
    the null-rate shape these tests price."""
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    (var / "cli-catalog.json").write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "gpu": ["T4"], "tpu": ["v6e1"], "no_high_mem": []}))


def test_budget_check_null_rate_unconfirmed(repo, tmp_path, monkeypatch):
    out = lease.budget_check(make_stub(set()), repo, lease.load_config(repo), {"high_mem": False, "rate_cu_per_hour": None})
    assert out["ok"] is True
    assert out["rate_unconfirmed"] is True
    assert out["projected"] is None
    assert out["balance"] == 500


def test_budget_check_shape_rate_prices_the_window(repo, tmp_path, monkeypatch):
    # The shape entry's own rate (0.5) is the one used — never a
    # tier map's generic figure.
    out = lease.budget_check(make_stub(set()), repo, lease.load_config(repo), {"high_mem": True, "rate_cu_per_hour": 0.5}, hours=2)
    assert out["ok"] is True
    assert out["projected"] == 1.0


def test_acquire_null_rate_shape(repo, tmp_path, monkeypatch):
    seed_cli_catalog(tmp_path)
    alive = set()
    out = lease.acquire(make_stub(alive), repo, "t4")
    assert out["action"] == "created"
    budget = out["record"]["budget_at_acquire"]
    assert budget["rate_unconfirmed"] is True
    assert budget["projected"] is None
    session = out["record"]["session"]
    assert session in alive
    # The record is keyed by its generated session and carries the
    # shape with its unconfirmed rate snapshot.
    rec = lease.load_record(repo, session)
    assert rec["shape"] == "t4" and rec["rate_cu_per_hour"] is None


T0 = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)


_SHAPE_FIXTURES = {
    "cpu": ({"gpu": None, "tpu": None, "high_mem": False}, 0.08),
    "cpu-highram": ({"gpu": None, "tpu": None, "high_mem": True}, 0.26),
}


def seed_record(repo, session, shape, **over):
    shape_spec, rate = _SHAPE_FIXTURES[shape]
    rec = {
        "session": session,
        "shape": shape,
        "shape_spec": shape_spec,
        "rate_cu_per_hour": rate,
        "acquired_at": "2026-10-29T17:00:00+00:00",
        "deadline": "2026-10-30T02:00:00+00:00",
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": [],
    }
    rec.update(over)
    lease.save_record(repo, rec)
    return rec


def test_extend_grant_prices_lobby_delta_only(repo, tmp_path, monkeypatch):
    lease.write_grant(repo, 8, now_dt=T0)
    out = lease.extend_grant(make_stub(set()), repo, 2, now_dt=T0)
    assert out["action"] == "grant-extended"
    assert out["old_until"] == "2026-10-30T02:00:00+00:00"
    assert out["new_until"] == "2026-10-30T04:00:00+00:00"
    # Priced on the standing shape alone: 0.08 x 2 h — not the burst's rate.
    assert out["budget"]["projected"] == 0.16
    assert lease.load_grant(repo)["until"] == "2026-10-30T04:00:00+00:00"


def test_extend_grant_inactive_without_grant(repo, tmp_path, monkeypatch):
    out = lease.extend_grant(make_stub(set()), repo, 2, now_dt=T0)
    assert out["action"] == "grant-inactive"


def test_extend_grant_inactive_when_expired(repo, tmp_path, monkeypatch):
    lease.write_grant(repo, 1, now_dt=T0)  # until T0 + 1 h
    later = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
    out = lease.extend_grant(make_stub(set()), repo, 2, now_dt=later)
    assert out["action"] == "grant-inactive"
    # A new window is a new grant: the expired one is untouched.
    assert lease.load_grant(repo)["until"] == "2026-10-29T19:00:00+00:00"


def test_write_grant_records_scope_snapshot(repo, tmp_path, monkeypatch):
    cfg = lease.load_config(repo)
    cfg = {**cfg, "daily_cu_budget": 42, "monthly_cu_budget": 4242, "standing": {**cfg.get("standing", {}), "daily_cu_cap": 2.0}}
    monkeypatch.setattr(lease.foundation, "load_config", lambda repo: cfg)
    out = lease.write_grant(repo, 8, burst_shape="cpu-highram", now_dt=T0)
    grant = out["grant"]
    # The grant's granted_at is its envelope id.
    assert grant["granted_at"] == T0.isoformat()
    scope = grant["scope"]
    assert scope["standing_shape"] == "cpu"
    assert scope["burst_shape"] == "cpu-highram"
    assert scope["shapes"] == ["cpu", "cpu-highram"]
    assert scope["caps"] == {"daily": 42, "monthly": 4242, "standing_daily": 2.0}
    # state() serves the scope with the grant.
    assert lease.load_grant(repo)["scope"] == scope


def test_extend_grant_moves_window_only_scope_frozen(repo, tmp_path, monkeypatch):
    cfg = {**lease.load_config(repo), "daily_cu_budget": 42}
    monkeypatch.setattr(lease.foundation, "load_config", lambda repo: cfg)
    lease.write_grant(repo, 8, now_dt=T0)
    scope_before = lease.load_grant(repo)["scope"]
    # Config caps move after the grant: the recorded scope must not.
    cfg2 = {**cfg, "daily_cu_budget": 7, "monthly_cu_budget": 70}
    monkeypatch.setattr(lease.foundation, "load_config", lambda repo: cfg2)
    out = lease.extend_grant(make_stub(set()), repo, 2, now_dt=T0)
    assert out["action"] == "grant-extended"
    grant = lease.load_grant(repo)
    assert grant["until"] == "2026-10-30T04:00:00+00:00"
    assert grant["scope"] == scope_before
    assert grant["scope"]["caps"]["daily"] == 42


def test_acquire_in_scope_stamps_envelope_and_via(repo, tmp_path, monkeypatch):
    lease.write_grant(repo, 8, burst_shape="cpu-highram", now_dt=T0)  # scope: standing cpu + burst cpu-highram
    alive = set()
    out = lease.acquire(make_stub(alive), repo, "cpu-highram", now_dt=T0, via="xbrl-thread")
    assert out["action"] == "created"
    rec = out["record"]
    assert rec["envelope_id"] == T0.isoformat()
    assert rec["via"] == "xbrl-thread"


def test_acquire_out_of_scope_carries_neither_stamp(repo, tmp_path, monkeypatch):
    seed_cli_catalog(tmp_path)
    lease.write_grant(repo, 8, burst_shape="cpu-highram", now_dt=T0)  # scope: standing cpu + burst cpu-highram
    alive = set()
    out = lease.acquire(make_stub(alive), repo, "t4", now_dt=T0, via="xbrl-thread")
    assert out["action"] == "created"
    assert "envelope_id" not in out["record"]
    assert "via" not in out["record"]


def test_acquire_adopted_in_scope_gains_envelope_stamp(repo, tmp_path, monkeypatch):
    seed_record(repo, "alpha", "cpu-highram")
    lease.write_grant(repo, 8, burst_shape="cpu-highram", now_dt=T0)
    out = lease.acquire(make_stub({"alpha"}), repo, "cpu-highram", now_dt=T0, via="panel")
    assert out["action"] == "adopted"
    assert out["record"]["envelope_id"] == T0.isoformat()
    # An adopted record keeps its creator's attribution: no via stamp.
    assert "via" not in out["record"]


def test_budget_check_caps_override_replaces_live_caps(repo, tmp_path, monkeypatch):
    # Explicit caps: the base config's caps are panel settings
    # that move independently of this test.
    cfg = {**lease.load_config(repo), "daily_cu_budget": 10, "monthly_cu_budget": 100}
    shape = {"high_mem": True, "rate_cu_per_hour": 0.5}
    # 30 h x 0.5 = 15 projected: refused by the live daily cap...
    out = lease.budget_check(make_stub(set()), repo, cfg, shape, hours=30)
    assert out["ok"] is False and out["reason"] == "daily-budget"
    # ...and allowed under a larger snapshot.
    out = lease.budget_check(make_stub(set()), repo, cfg, shape, hours=30, caps_override={"daily": 100, "monthly": 100, "standing_daily": 2.0})
    assert out["ok"] is True
    # The reverse: a smaller snapshot refuses what live caps allow.
    out = lease.budget_check(make_stub(set()), repo, cfg, shape, hours=12, caps_override={"daily": 5, "monthly": 100, "standing_daily": 2.0})
    assert out["ok"] is False and out["reason"] == "daily-budget" and out["cap"] == 5


def _cfg_holder(monkeypatch, repo, **over):
    holder = {"cfg": {**lease.load_config(repo), **over}}
    monkeypatch.setattr(lease.foundation, "load_config", lambda repo: holder["cfg"])
    return holder


def test_acquire_in_scope_prices_against_snapshot_not_live(repo, tmp_path, monkeypatch):
    holder = _cfg_holder(monkeypatch, repo, daily_cu_budget=42)
    lease.write_grant(repo, 8, burst_shape="cpu-highram", now_dt=T0)  # snapshot daily = 42
    holder["cfg"] = {**holder["cfg"], "daily_cu_budget": 1}  # live caps tighten
    out = lease.acquire(make_stub(set()), repo, "cpu-highram", now_dt=T0)
    # 8 h x 0.26 = 2.08 projected: the live cap (1) would refuse;
    # the envelope's snapshot (42) prices it instead.
    assert out["action"] == "created"


def test_acquire_in_scope_refused_by_smaller_snapshot(repo, tmp_path, monkeypatch):
    holder = _cfg_holder(monkeypatch, repo, daily_cu_budget=1)
    lease.write_grant(repo, 8, burst_shape="cpu-highram", now_dt=T0)  # snapshot daily = 1
    holder["cfg"] = {**holder["cfg"], "daily_cu_budget": 1000}  # live caps loosen
    out = lease.acquire(make_stub(set()), repo, "cpu-highram", now_dt=T0)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    assert out["budget"]["cap"] == 1


def test_extend_burst_independent_of_grant(repo, tmp_path, monkeypatch):
    lease.write_grant(repo, 8, now_dt=T0)
    seed_record(repo, "alpha", "cpu-highram")
    out = lease.extend(make_stub({"alpha"}), repo, "alpha", 2, now_dt=T0)
    assert out["action"] == "extended"
    assert out["budget"]["projected"] == 0.52  # the burst's own rate x delta
    # The grant neither extends nor blocks: its end is unmoved.
    assert lease.load_grant(repo)["until"] == "2026-10-30T02:00:00+00:00"


def test_extend_lobby_directly_refused(repo, tmp_path, monkeypatch):
    lease.write_grant(repo, 8, now_dt=T0)
    rec = seed_record(repo, "lobby", "cpu", standing=True)
    out = lease.extend(make_stub({"lobby"}), repo, "lobby", 2, now_dt=T0)
    assert out["action"] == "extend-standing-managed"
    assert lease.load_record(repo, "lobby")["deadline"] == rec["deadline"]
