"""Lease unit tests: attributed spend, budget checks, and the
panel's budget-cap editor, with stub colab callables (no
subprocess)."""

import json
import subprocess
from datetime import datetime, timezone

from unit_harness import NOW, cp, gate_permissive, lease, repo, seed_record  # noqa: F401


def test_budget_snapshot_serves_standing_day_spend(repo, tmp_path, monkeypatch):
    """The grant card renders standing spend vs cap (mock Panel C), so
    state must serve the standing caller's day spend — the guard's
    own attributed figure (journaled segments + live records), never
    a balance delta: the same spend must be served whatever the
    balance did in between (external usage, expirations)."""
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    # A fixed mid-day now: the fixture must not depend on the
    # wall clock — inside the first minute of a UTC day the
    # segment is shorter than the rate floor below assumes and
    # the invariant breaks (a gate that happened to start at
    # 00:00 UTC caught exactly that).
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max((now - day_start).total_seconds() / 3600.0, 1 / 60)
    # One lobby segment covering today so far, rated to total 2.42 CU.
    seg = {"caller": "standing", "session": "lobby", "start": day_start.isoformat(), "end": now.isoformat(), "rate": 2.42 / hours}
    (var / "lease-spend-journal.jsonl").write_text(json.dumps(seg) + "\n")

    out = lease.budget_snapshot(lambda *a, **k: cp("Current balance: 768.38 compute units"), repo, now_dt=now)
    assert out["balance"] == 768.38
    assert out["standing_day_spent"] == 2.42
    assert out["day_spent"] == 2.42
    # The served cap is the fixture config's standing sub-cap,
    # read back from the scratch repo rather than pinned.
    assert out["caps"]["standing_daily"] == lease.load_config(repo)["standing"]["daily_cu_cap"]

    # A balance collapse (the 2026-11-01 expiration step) moves
    # nothing: spend is attributed, not balance-derived.
    out2 = lease.budget_snapshot(lambda *a, **k: cp("Current balance: 699.00 compute units"), repo, now_dt=now)
    assert out2["standing_day_spent"] == 2.42
    assert out2["day_spent"] == 2.42

    # No segments and no records -> 0.0, not a fabricated number.
    (var / "lease-spend-journal.jsonl").write_text("")
    out3 = lease.budget_snapshot(lambda *a, **k: cp("Current balance: 768.38 compute units"), repo)
    assert out3["standing_day_spent"] == 0.0


def test_attributed_spend_splits_segments_at_period_boundaries(repo, tmp_path, monkeypatch):
    """The derivation itself: journaled segments split at the day and
    month boundaries, live records contribute their running segment,
    and unconfirmed-rate work contributes nothing."""
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    segs = [
        # Spans midnight into today: 2 h count today (and this month).
        {"shape": "cpu-highram", "session": "alpha", "start": "2026-10-31T22:00:00+00:00", "end": "2026-11-01T02:00:00+00:00", "rate": 1.0},
        # Last month only: counts for neither period.
        {"shape": "cpu-highram", "session": "alpha", "start": "2026-10-15T00:00:00+00:00", "end": "2026-10-15T10:00:00+00:00", "rate": 1.0},
        # Unconfirmed rate: journaled, never priced.
        {"shape": "cpu", "session": "s", "start": "2026-11-01T00:00:00+00:00", "end": "2026-11-01T05:00:00+00:00", "rate": None},
    ]
    (var / "lease-spend-journal.jsonl").write_text("".join(json.dumps(s) + "\n" for s in segs))
    lease.save_record(repo, {"session": "standard", "shape": "cpu", "shape_spec": {"gpu": None, "tpu": None, "high_mem": False}, "rate_cu_per_hour": 0.08, "acquired_at": "2026-11-01T10:00:00+00:00", "deadline": "2026-11-01T20:00:00+00:00", "last_used_at": "2026-11-01T10:00:00+00:00", "jobs": []})
    out = lease.attributed_spend(repo, now)
    # Live standard record: 2 h x 0.08 = 0.16 on top of the alpha session's 2.0.
    assert out["day_spent"] == 2.16
    assert out["month_spent"] == 2.16
    assert out["sessions"]["alpha"] == {"day_spent": 2.0, "month_spent": 2.0}
    assert out["sessions"]["standard"] == {"day_spent": 0.16, "month_spent": 0.16}


def test_budget_check_balance_floor_refuses_beyond_the_bank(repo, tmp_path, monkeypatch):
    """Caps throttle attributed burn; the balance is the floor: a
    window projecting more than the account holds refuses even with
    zero spend against every cap."""
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    out = lease.budget_check(lambda *a, **k: cp("Current balance: 0.05 compute units"), repo, lease.load_config(repo), {"rate_cu_per_hour": 0.08}, hours=8, now_dt=NOW)
    assert out["ok"] is False
    assert out["reason"] == "balance-insufficient"
    assert out["projected"] == 0.64
    assert out["day_spent"] == 0.0


def test_release_journals_the_completed_segment(repo, tmp_path, monkeypatch):
    """Release closes the session's segment into the journal, so its
    burn stays counted after the record is gone."""
    seed_record(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("")

    out = lease.release(stub, repo, now_dt=NOW)
    assert out["action"] == "released"
    lines = (tmp_path / "var" / "lease-spend-journal.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    seg = json.loads(lines[0])
    assert seg["shape"] == "cpu" and seg["rate"] == 0.08
    # Seeded record ran 17:00 -> NOW (18:00): 1 h x 0.08.
    assert lease.attributed_spend(repo, NOW)["day_spent"] == 0.08


def _seed_caps_config(tmp_path, cfg=None):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg or {"enabled": True, "daily_cu_budget": 10, "monthly_cu_budget": 100, "max_hours": 8, "standing": {"daily_cu_cap": 2.0, "profile": "lobby", "warm_on_renew": True}}
    (cfg_dir / "lease.json").write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    return cfg


def test_set_budget_caps_updates_and_preserves(tmp_path):
    """The panel's cap editor rides set_budget_caps: the three caps
    update, every other config key (and the standing block's other
    keys) survives, and the result reports before and after in the
    served caps shape."""
    _seed_caps_config(tmp_path)
    out = lease.set_budget_caps(tmp_path, daily=25, monthly=250.5, standing_daily=3)
    assert out["action"] == "caps-updated"
    assert out["caps"] == {"daily": 25, "monthly": 250.5, "standing_daily": 3}
    assert out["previous"] == {"daily": 10, "monthly": 100, "standing_daily": 2.0}
    cfg = json.loads((tmp_path / "config" / "lease.json").read_text())
    assert cfg["daily_cu_budget"] == 25
    assert cfg["monthly_cu_budget"] == 250.5
    assert cfg["standing"] == {"daily_cu_cap": 3, "profile": "lobby", "warm_on_renew": True}
    assert cfg["max_hours"] == 8


def test_set_budget_caps_null_clears_and_omission_keeps(tmp_path):
    """Null is the guard's own no-cap value; an omitted cap is left
    exactly as it was."""
    _seed_caps_config(tmp_path)
    out = lease.set_budget_caps(tmp_path, monthly=None)
    assert out["caps"] == {"daily": 10, "monthly": None, "standing_daily": 2.0}
    cfg = json.loads((tmp_path / "config" / "lease.json").read_text())
    assert cfg["monthly_cu_budget"] is None
    assert cfg["daily_cu_budget"] == 10


def test_set_budget_caps_rejects_bad_values_without_writing(tmp_path):
    _seed_caps_config(tmp_path)
    before = (tmp_path / "config" / "lease.json").read_bytes()
    for bad in (-1, float("nan"), float("inf"), "10", True):
        out = lease.set_budget_caps(tmp_path, daily=bad)
        assert out["action"] == "caps-invalid", bad
    assert (tmp_path / "config" / "lease.json").read_bytes() == before


def test_set_budget_caps_unreadable_config(tmp_path):
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "lease.json").write_text("{not json")
    out = lease.set_budget_caps(tmp_path, daily=5)
    assert out["action"] == "caps-invalid"


def test_budget_check_maps_auth_required(repo, tmp_path, monkeypatch):
    """The wrapper guard's stop-and-surface signal is its own refusal
    reason, distinct from a stall or a parse failure."""

    def dead_auth(*a, **k):
        return subprocess.CompletedProcess([], 3, "", "colab auth required: stored token missing or invalid")

    out = lease.budget_check(dead_auth, repo, lease.load_config(repo), {"rate_cu_per_hour": 0.08}, hours=1)
    assert out == {"ok": False, "reason": "auth-required"}


def test_budget_check_usage_timeout_is_a_refusal_not_a_crash(repo, tmp_path, monkeypatch):
    """The usage read rides the stall-prone proxy (a 120 s timeout
    fired live on 2026-11-01 and crashed acquire with a traceback):
    it must come back as a clean refused-budget — the guard cannot
    price without a balance — never as a raised exception."""

    def stall(*a, **k):
        if a and a[0] == "usage":
            raise subprocess.TimeoutExpired("colab", 120)
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("")

    out = lease.budget_check(stall, repo, lease.load_config(repo), {"high_mem": False, "rate_cu_per_hour": 0.08}, hours=1)
    assert out["ok"] is False
    assert out["reason"] == "usage-unavailable"
    acq = lease.acquire(stall, repo, "cpu", now_dt=NOW, hours=1)
    assert acq["action"] == "refused-budget"
