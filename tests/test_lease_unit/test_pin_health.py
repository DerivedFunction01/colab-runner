"""Lease unit tests: the session pin and launch-health
demotion (funnel hardening), with stub colab callables
(no subprocess)."""

import json
from datetime import timedelta

from unit_harness import NOW, _auto_stub, gate_permissive, lease, repo  # noqa: F401

# --- Session pin + launch health (funnel-hardening, 2026-11-11) ----------------
# The pin is the runner's --session seam: adopt the named lease
# or refuse pin-unavailable — never substitute, never clear the
# named record. Launch health demotes a session within its
# caller tier when the funnel's recent launch operations failed
# on it; it never reorders tiers and never blocks adoption.


def _seed_named(repo, session, deadline_h, caller=None):
    rec = {
        "session": session,
        "shape": "cpu",
        "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "rate_cu_per_hour": 0.08,
        "acquired_at": NOW.isoformat(),
        "deadline": (NOW + timedelta(hours=deadline_h)).isoformat(),
        "last_used_at": NOW.isoformat(),
        "jobs": [],
    }
    if caller is not None:
        rec["caller"] = caller
    lease.save_record(repo, rec)
    return rec


def _journal_health(repo, session, outcomes, minutes_ago=10):
    path = lease.launch_health_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = NOW - timedelta(minutes=minutes_ago)
    with path.open("a") as fh:
        for i, ok in enumerate(outcomes):
            fh.write(json.dumps({"ts": (base + timedelta(seconds=i)).isoformat(), "session": session, "stage": "probe", "ok": ok, "detail": "test"}) + "\n")


def test_pin_adopts_the_named_lease_over_the_deadline_winner(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    alive = {"pin-late-01", "pin-early-02"}
    stub = _auto_stub(alive)
    _seed_named(repo, "pin-late-01", 6)
    _seed_named(repo, "pin-early-02", 4)
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="pin-early-02")
    assert out["action"] == "adopted"
    assert out["record"]["session"] == "pin-early-02"


def test_pin_refuses_a_missing_record_and_creates_nothing(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = _auto_stub(set())
    _seed_named(repo, "pin-present-01", 6)
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="pin-absent-99")
    assert out["action"] == "pin-unavailable"
    assert [r["session"] for r in lease.all_records(repo)] == ["pin-present-01"]


def test_pin_refuses_a_dead_session_and_keeps_its_record(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = _auto_stub(set())  # nothing alive
    _seed_named(repo, "pin-dead-01", 6)
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="pin-dead-01")
    assert out["action"] == "pin-unavailable"
    assert lease.load_record(repo, "pin-dead-01") is not None, "a refused pin destroyed the named record"


def test_pin_refuses_a_shape_mismatch(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = _auto_stub({"pin-other-01"})
    rec = _seed_named(repo, "pin-other-01", 6)
    rec["shape"] = "gpu-x"
    lease.save_record(repo, rec)
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="pin-other-01")
    assert out["action"] == "pin-unavailable"
    assert "shape" in out["reason"]


def test_health_demotes_a_failing_session_within_its_tier(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    alive = {"health-late-01", "health-early-02"}
    stub = _auto_stub(alive)
    _seed_named(repo, "health-late-01", 6)
    _seed_named(repo, "health-early-02", 4)
    # Without health evidence the later deadline wins (control).
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out["record"]["session"] == "health-late-01"
    # Two probe failures inside the window demote it below the
    # healthy session despite the later deadline.
    _journal_health(repo, "health-late-01", [False, False])
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "adopted"
    assert out["record"]["session"] == "health-early-02"


def test_all_degraded_pool_still_adopts_and_flags_it(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    alive = {"health-late-01", "health-early-02"}
    stub = _auto_stub(alive)
    _seed_named(repo, "health-late-01", 6)
    _seed_named(repo, "health-early-02", 4)
    _journal_health(repo, "health-late-01", [False])
    _journal_health(repo, "health-early-02", [False])
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "adopted"
    assert out["record"]["session"] == "health-late-01", "health reordered past the deadline rule with no healthy candidate"
    assert out["health_degraded"] is True


def test_launch_health_ignores_outcomes_outside_the_window(repo, tmp_path, monkeypatch):
    _journal_health(repo, "health-old-01", [False, False], minutes_ago=120)
    assert lease.launch_health(repo, NOW) == {}
    _journal_health(repo, "health-old-01", [True])
    health = lease.launch_health(repo, NOW)
    assert health["health-old-01"]["degraded"] is False
    assert health["health-old-01"]["fails"] == 0
