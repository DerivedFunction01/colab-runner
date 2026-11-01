"""Two-tier pricing unit tests with stub colab callables (no subprocess).

Pins the spec-catalog pricing rules: a profile's own rate_cu_per_hour
prices its acquires (in preference to the config tier map), and an
explicitly null rate is unconfirmed — the budget check passes with
rate_unconfirmed and a null projection; no number is ever fabricated
from the fallback map for such a profile. The extension tests pin the
two-tier edge cases from the lease-extension plan: grant extension
prices the lobby delta only, burst extension is independent of the
grant, direct lobby extension is refused, and an expired grant
refuses extension.
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lease", REPO / "sync" / "lease.py")
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)


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


def catalog(monkeypatch):
    profiles = lease.load_profiles(REPO)
    profiles["nullspec"] = {"session": "nullspec", "high_mem": False, "rate_cu_per_hour": None, "timeout": 3600}
    monkeypatch.setattr(lease, "load_profiles", lambda repo: profiles)
    return profiles


def test_budget_check_null_rate_unconfirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    out = lease.budget_check(make_stub(set()), REPO, lease.load_config(REPO), {"high_mem": False, "rate_cu_per_hour": None}, profile_name="nullspec")
    assert out["ok"] is True
    assert out["rate_unconfirmed"] is True
    assert out["projected"] is None
    assert out["balance"] == 500


def test_budget_check_profile_rate_wins_over_map(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    # The config map prices high_mem at 0.26; the profile's own rate
    # (0.5) must be the one used.
    out = lease.budget_check(make_stub(set()), REPO, lease.load_config(REPO), {"high_mem": True, "rate_cu_per_hour": 0.5}, hours=2, profile_name="odd")
    assert out["ok"] is True
    assert out["projected"] == 1.0


def test_acquire_null_rate_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    catalog(monkeypatch)
    alive = set()
    out = lease.acquire(make_stub(alive), REPO, "nullspec")
    assert out["action"] == "created"
    budget = out["record"]["budget_at_acquire"]
    assert budget["rate_unconfirmed"] is True
    assert budget["projected"] is None
    assert "nullspec" in alive
    # The record landed on the null-rate profile's own path.
    assert lease.load_record(REPO, "nullspec")["session"] == "nullspec"


T0 = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)


def seed_profile_record(profile, session, **over):
    rec = {
        "profile": profile,
        "session": session,
        "acquired_at": "2026-10-29T17:00:00+00:00",
        "deadline": "2026-10-30T02:00:00+00:00",
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": [],
    }
    rec.update(over)
    lease.save_record(REPO, rec)
    return rec


def test_extend_grant_prices_lobby_delta_only(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.write_grant(REPO, 8, now_dt=T0)
    out = lease.extend_grant(make_stub(set()), REPO, 2, now_dt=T0)
    assert out["action"] == "grant-extended"
    assert out["old_until"] == "2026-10-30T02:00:00+00:00"
    assert out["new_until"] == "2026-10-30T04:00:00+00:00"
    # Priced on the lobby alone: 0.08 x 2 h — not the burst's rate.
    assert out["budget"]["projected"] == 0.16
    assert lease.load_grant(REPO)["until"] == "2026-10-30T04:00:00+00:00"


def test_extend_grant_inactive_without_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    out = lease.extend_grant(make_stub(set()), REPO, 2, now_dt=T0)
    assert out["action"] == "grant-inactive"


def test_extend_grant_inactive_when_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.write_grant(REPO, 1, now_dt=T0)  # until T0 + 1 h
    later = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
    out = lease.extend_grant(make_stub(set()), REPO, 2, now_dt=later)
    assert out["action"] == "grant-inactive"
    # A new window is a new grant: the expired one is untouched.
    assert lease.load_grant(REPO)["until"] == "2026-10-29T19:00:00+00:00"


def test_write_grant_records_scope_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    cfg = lease.load_config(REPO)
    cfg = {**cfg, "daily_cu_budget": 42, "monthly_cu_budget": 4242, "standing": {**cfg.get("standing", {}), "daily_cu_cap": 2.0}}
    monkeypatch.setattr(lease, "load_config", lambda repo: cfg)
    out = lease.write_grant(REPO, 8, burst_profile="standard", now_dt=T0)
    grant = out["grant"]
    # The grant's granted_at is its envelope id.
    assert grant["granted_at"] == T0.isoformat()
    scope = grant["scope"]
    assert scope["lobby_profile"] == "lobby"
    assert scope["burst_profile"] == "standard"
    assert scope["profiles"] == ["lobby", "standard"]
    assert scope["caps"] == {"daily": 42, "monthly": 4242, "standing_daily": 2.0}
    # state() serves the scope with the grant.
    assert lease.load_grant(REPO)["scope"] == scope


def test_extend_grant_moves_window_only_scope_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    cfg = {**lease.load_config(REPO), "daily_cu_budget": 42}
    monkeypatch.setattr(lease, "load_config", lambda repo: cfg)
    lease.write_grant(REPO, 8, now_dt=T0)
    scope_before = lease.load_grant(REPO)["scope"]
    # Config caps move after the grant: the recorded scope must not.
    cfg2 = {**cfg, "daily_cu_budget": 7, "monthly_cu_budget": 70}
    monkeypatch.setattr(lease, "load_config", lambda repo: cfg2)
    out = lease.extend_grant(make_stub(set()), REPO, 2, now_dt=T0)
    assert out["action"] == "grant-extended"
    grant = lease.load_grant(REPO)
    assert grant["until"] == "2026-10-30T04:00:00+00:00"
    assert grant["scope"] == scope_before
    assert grant["scope"]["caps"]["daily"] == 42


def test_acquire_in_scope_stamps_envelope_and_via(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.write_grant(REPO, 8, now_dt=T0)  # scope: lobby + workload
    alive = set()
    out = lease.acquire(make_stub(alive), REPO, "workload", now_dt=T0, via="xbrl-thread")
    assert out["action"] == "created"
    rec = out["record"]
    assert rec["envelope_id"] == T0.isoformat()
    assert rec["via"] == "xbrl-thread"


def test_acquire_out_of_scope_carries_neither_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.write_grant(REPO, 8, now_dt=T0)  # scope: lobby + workload
    alive = set()
    out = lease.acquire(make_stub(alive), REPO, "standard", now_dt=T0, via="xbrl-thread")
    assert out["action"] == "created"
    assert "envelope_id" not in out["record"]
    assert "via" not in out["record"]


def test_acquire_adopted_in_scope_gains_envelope_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    seed_profile_record("workload", "workload")
    lease.write_grant(REPO, 8, now_dt=T0)
    out = lease.acquire(make_stub({"workload"}), REPO, "workload", now_dt=T0, via="panel")
    assert out["action"] == "adopted"
    assert out["record"]["envelope_id"] == T0.isoformat()
    # An adopted record keeps its creator's attribution: no via stamp.
    assert "via" not in out["record"]


def test_budget_check_caps_override_replaces_live_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    # Explicit caps: the live config's caps are the operator's panel
    # settings and move independently of this test.
    cfg = {**lease.load_config(REPO), "daily_cu_budget": 10, "monthly_cu_budget": 100}
    profile = {"high_mem": True, "rate_cu_per_hour": 0.5}
    # 30 h x 0.5 = 15 projected: refused by the live daily cap...
    out = lease.budget_check(make_stub(set()), REPO, cfg, profile, hours=30, profile_name="odd")
    assert out["ok"] is False and out["reason"] == "daily-budget"
    # ...and allowed under a larger snapshot.
    out = lease.budget_check(make_stub(set()), REPO, cfg, profile, hours=30, profile_name="odd", caps_override={"daily": 100, "monthly": 100, "standing_daily": 2.0})
    assert out["ok"] is True
    # The reverse: a smaller snapshot refuses what live caps allow.
    out = lease.budget_check(make_stub(set()), REPO, cfg, profile, hours=12, profile_name="odd", caps_override={"daily": 5, "monthly": 100, "standing_daily": 2.0})
    assert out["ok"] is False and out["reason"] == "daily-budget" and out["cap"] == 5


def _cfg_holder(monkeypatch, **over):
    holder = {"cfg": {**lease.load_config(REPO), **over}}
    monkeypatch.setattr(lease, "load_config", lambda repo: holder["cfg"])
    return holder


def test_acquire_in_scope_prices_against_snapshot_not_live(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    holder = _cfg_holder(monkeypatch, daily_cu_budget=42)
    lease.write_grant(REPO, 8, now_dt=T0)  # snapshot daily = 42
    holder["cfg"] = {**holder["cfg"], "daily_cu_budget": 1}  # live caps tighten
    out = lease.acquire(make_stub(set()), REPO, "workload", now_dt=T0)
    # 8 h x 0.26 = 2.08 projected: the live cap (1) would refuse;
    # the envelope's snapshot (42) prices it instead.
    assert out["action"] == "created"


def test_acquire_in_scope_refused_by_smaller_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    holder = _cfg_holder(monkeypatch, daily_cu_budget=1)
    lease.write_grant(REPO, 8, now_dt=T0)  # snapshot daily = 1
    holder["cfg"] = {**holder["cfg"], "daily_cu_budget": 1000}  # live caps loosen
    out = lease.acquire(make_stub(set()), REPO, "workload", now_dt=T0)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    assert out["budget"]["cap"] == 1


def test_extend_burst_independent_of_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.write_grant(REPO, 8, now_dt=T0)
    seed_profile_record("workload", "workload")
    out = lease.extend(make_stub({"workload"}), REPO, "workload", 2, now_dt=T0)
    assert out["action"] == "extended"
    assert out["budget"]["projected"] == 0.52  # the burst's own rate x delta
    # The grant neither extends nor blocks: its end is unmoved.
    assert lease.load_grant(REPO)["until"] == "2026-10-30T02:00:00+00:00"


def test_extend_lobby_directly_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.write_grant(REPO, 8, now_dt=T0)
    rec = seed_profile_record("lobby", "lobby", standing=True)
    out = lease.extend(make_stub({"lobby"}), REPO, "lobby", 2, now_dt=T0)
    assert out["action"] == "extend-standing-managed"
    assert lease.load_record(REPO, "lobby")["deadline"] == rec["deadline"]
