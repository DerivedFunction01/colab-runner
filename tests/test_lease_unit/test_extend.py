"""Lease unit tests: extension and the release guard, with stub
colab callables (no subprocess)."""

import subprocess

from unit_harness import NOW, cp, gate_permissive, lease, make_extend_stub, repo, seed_record  # noqa: F401


def test_extend_happy_path_moves_deadline_and_prices_delta(repo, tmp_path, monkeypatch):
    seed_record(repo)
    monkeypatch.setenv("HOME", str(tmp_path))  # no token here: the kit install reports token-missing, non-fatally
    out = lease.extend(make_extend_stub(["standard"]), repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extended"
    assert out["old_deadline"] == "2026-10-30T01:00:00+00:00"
    assert out["new_deadline"] == "2026-10-30T03:00:00+00:00"
    # Delta pricing: the standard rate (0.08) x the added hours only.
    assert out["budget"]["projected"] == 0.16
    rec = lease.load_record(repo, "standard")
    assert rec["deadline"] == "2026-10-30T03:00:00+00:00"
    assert rec["renew"] is True
    assert rec["jobs"] == []  # the jobs list is untouched


def test_extend_refused_budget_changes_nothing(repo, tmp_path, monkeypatch):
    seed_record(repo)
    # Sized off the live daily cap so the refusal holds whatever cap
    # the operator has set: the extension's projection alone (hours x
    # the standard rate 0.08) clears the cap. (The fixed 200 h form
    # silently stopped refusing when the live cap rose past 16 CU.)
    cap = lease.load_config(repo)["daily_cu_budget"]
    hours = int(cap / 0.08) + 50
    out = lease.extend(make_extend_stub(["standard"]), repo, "standard", hours, now_dt=NOW)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    assert out["budget"]["projected"] == round(hours * 0.08, 2)
    rec = lease.load_record(repo, "standard")
    assert rec["deadline"] == "2026-10-30T01:00:00+00:00"
    assert "renew" not in rec


def test_extend_unknown_session_state_refuses(repo, tmp_path, monkeypatch):
    seed_record(repo)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.extend(boom, repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-unknown"
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-30T01:00:00+00:00"


def test_extend_absent_window_lease_applies_to_the_window(repo, tmp_path, monkeypatch):
    rec = seed_record(repo)
    rec["renew"] = True
    lease.save_record(repo, rec)
    out = lease.extend(make_extend_stub([]), repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extended"
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-30T03:00:00+00:00"


def test_extend_absent_without_renew_refuses(repo, tmp_path, monkeypatch):
    seed_record(repo)
    out = lease.extend(make_extend_stub([]), repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-no-lease"
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-30T01:00:00+00:00"


def test_extend_without_record_refuses(repo, tmp_path, monkeypatch):
    out = lease.extend(make_extend_stub(["standard"]), repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-no-lease"


def test_extend_repeated_extensions_accumulate(repo, tmp_path, monkeypatch):
    seed_record(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = make_extend_stub(["standard"])
    assert lease.extend(stub, repo, "standard", 2, now_dt=NOW)["action"] == "extended"
    out = lease.extend(stub, repo, "standard", 3, now_dt=NOW)
    assert out["old_deadline"] == "2026-10-30T03:00:00+00:00"
    assert out["new_deadline"] == "2026-10-30T06:00:00+00:00"
    # Each extension prices its own delta.
    assert out["budget"]["projected"] == 0.24


def test_release_guard_aborts_when_deadline_moved_on_disk(repo, tmp_path, monkeypatch):
    """An extension landing between the caller's read and the stop
    wins: release() re-reads the record immediately before stopping,
    sees the deadline moved into the future, and aborts — no stop is
    issued and the record stands."""
    seed_record(repo)
    real_load = lease.load_record
    reads = {"n": 0}

    def moved_load(repo, profile=None):
        reads["n"] += 1
        rec = real_load(repo, profile)
        if reads["n"] > 1 and rec is not None:
            rec = dict(rec, deadline="2026-10-30T05:00:00+00:00")
        return rec

    monkeypatch.setattr(lease.records, "load_record", moved_load)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        return cp('["standard"]')

    out = lease.release(stub, repo)
    assert out["action"] == "release-aborted"
    assert out["guard"] == "deadline-moved"
    assert not [c for c in calls if c and c[0] == "stop"]
    assert lease.load_record(repo) is not None


def test_extend_unknown_carries_the_read_error(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    seed_record(repo)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.extend(boom, repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-unknown"
    assert out["attempts"] == 3 and "TimeoutExpired" in out["read_error"]
