"""Lease unit tests: acquire — windows, callers, and the
concurrent-lease rules, with stub colab callables (no
subprocess)."""

import json
from datetime import timedelta

from unit_harness import HELP_TEXT, NOW, _acquire_stub, _auto_stub, cp, gate_permissive, lease, repo, seed_record  # noqa: F401


def test_acquire_dynamic_profiles_pass_their_cli_flags(repo, tmp_path, monkeypatch):
    news = []

    def stub(*a, **k):
        if a[:2] == ("new", "--help"):
            return cp(HELP_TEXT)
        if a and a[0] == "usage":
            return cp("balance: 500 CU (stub)")
        if a and a[0] == "sessions":
            return cp("[]")
        if a and a[0] == "new":
            news.append(a)
            return cp("created")
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    out = lease.acquire(stub, repo, "l4", now_dt=NOW)
    assert out["action"] == "created"
    assert ("--gpu", "L4") in zip(news[0], news[0][1:])
    out2 = lease.acquire(stub, repo, "tpu-v6e1", now_dt=NOW)
    assert out2["action"] == "created"
    assert ("--tpu", "v6e1") in zip(news[1], news[1][1:])


def test_acquire_honors_an_explicit_window_over_max_hours(repo, tmp_path, monkeypatch):
    """Regression (2026-11-01): acquire silently clamped every
    explicit hours value to the config max_hours, so an ordered 24 h
    endurance window came up as 8 h, unflagged for renewal, and the
    budget priced the clamped window. An explicit window is honored
    as given; past the session cap the lease is a window lease from
    birth (renew flagged, delta priced on the full window)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no token here: the kit install reports token-missing, non-fatally
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW, hours=24)
    assert out["action"] == "created"
    rec = out["record"]
    assert rec["deadline"] == "2026-10-30T18:00:00+00:00"
    assert rec["renew"] is True
    assert rec["budget_at_acquire"]["projected"] == 1.92


def test_acquire_default_window_is_config_max_hours(repo, tmp_path, monkeypatch):
    """Without hours, the window is the config max_hours (8) — the
    pre-existing default behavior is unchanged by the clamp fix."""
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert out["record"]["deadline"] == "2026-10-30T02:00:00+00:00"
    assert "renew" not in out["record"]


def test_acquire_max_hours_cap_still_clamps_its_caller(repo, tmp_path, monkeypatch):
    """The standing duty passes max_hours_cap explicitly for its own
    windows; that parameter — and only that parameter — still
    clamps."""
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW, hours=24, max_hours_cap=12)
    assert out["action"] == "created"
    assert out["record"]["deadline"] == "2026-10-30T06:00:00+00:00"


def test_acquire_never_destroys_record_on_unreadable_sessions(repo, tmp_path, monkeypatch):
    """The 2026-11-01 record loss, pinned: a live lease whose session
    list comes back as the auth prompt (or any unreadable output) must
    survive acquire untouched — no stop, no delete, no recreate."""
    rec = seed_record(repo)
    prompt = lambda *a, **k: cp("Enter the authorization code:")  # noqa: E731
    out = lease.acquire(prompt, repo, "cpu", now_dt=NOW)
    assert out["action"] == "acquire-unknown"
    kept = lease.load_record(repo, "standard")
    assert kept is not None
    assert kept["deadline"] == rec["deadline"]
    assert kept["acquired_at"] == rec["acquired_at"]


def test_acquire_never_clears_record_on_unconfirmed_absence(repo, tmp_path, monkeypatch):
    """The 2026-11-13 record losses, pinned: a candidate whose
    only fault is an absent reading with its deadline ahead is
    skipped, never stopped or deleted — the absence must first
    sustain past the confirmation window (the reap's discipline,
    one shared stamp). Once confirmed, clearing proceeds."""
    seed_record(repo)
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert out["record"]["session"] != "standard"
    assert lease.load_record(repo, "standard") is not None
    stamp = lease.foundation.absent_stamp_path(repo, "standard")
    data = json.loads(stamp.read_text())
    data["first_absent_at"] = (NOW - timedelta(seconds=lease.ABSENT_CONFIRM_S + 1)).isoformat()
    stamp.write_text(json.dumps(data))
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert lease.load_record(repo, "standard") is None


def test_pin_on_unconfirmed_absence_refuses_as_pin_unavailable(repo, tmp_path, monkeypatch):
    """A pin named the record: the pin check's read flickers
    unknown, the candidate loop's read lands absent — an
    unconfirmed absence refuses with the pin's own verdict (and
    the record intact), never the creation gate's."""
    seed_record(repo)
    calls = {"n": 0}

    def flicker(colab, session, *a, **k):
        calls["n"] += 1
        return "unknown" if calls["n"] == 1 else "absent"

    monkeypatch.setattr(lease.sessions, "session_state", flicker)
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW, prefer_session="standard")
    assert out["action"] == "pin-unavailable"
    assert "unconfirmed" in out["reason"]
    assert lease.load_record(repo, "standard") is not None


# --- Concurrent leases (profile retirement) ------------------------------------
# Use is shared, capacity is per request: any caller's acquire may
# use (ride) a live lease at the shape, whoever created it — the
# ride is journaled as the rider's claim and the record keeps its
# creator's stamp. A second concurrent lease at the same shape
# comes from a capacity request (the demand duty), never from an
# ordinary acquire that could have ridden. (The retired
# parallel-instance derivation — one extra session per profile,
# named by rule — is gone; this is its successor behaviour.)


def test_two_callers_hold_concurrent_leases_at_one_shape(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    alive = set()
    stub = _auto_stub(alive)
    a = lease.acquire(stub, repo, "cpu", now_dt=NOW, caller="alpha")
    assert a["action"] == "created"
    # Beta's ordinary acquire rides alpha's live lease.
    b = lease.acquire(stub, repo, "cpu", now_dt=NOW, caller="beta")
    assert b["action"] == "adopted"
    assert b["record"]["session"] == a["record"]["session"]
    assert b["riding"] is True and b["owner_caller"] == "alpha"
    assert len(lease.all_records(repo)) == 1
    # A capacity request (duty) creates beta's own second lease.
    b2 = lease.acquire(stub, repo, "cpu", now_dt=NOW, caller="beta", duty=True)
    assert b2["action"] == "created"
    assert b2["record"]["session"] != a["record"]["session"]
    assert len(lease.all_records(repo)) == 2
    again = lease.acquire(stub, repo, "cpu", now_dt=NOW, caller="alpha")
    assert again["action"] == "adopted"
    assert again["record"]["session"] == a["record"]["session"]
    assert len(lease.all_records(repo)) == 2


def test_generated_names_never_reuse_a_claimed_name(repo, tmp_path, monkeypatch):
    """A name in the caller-claims journal is never handed out
    again: a stale claim can then never misattribute a later lease
    (the caller-attribution proof's defect, closed by
    construction). Live sessions, records, and the spend journal
    feed the same taken set."""
    lease.append_caller_claim(repo, "amber-otter-42", "alpha", "test")
    assert "amber-otter-42" in lease._taken_session_names(repo)
    for _ in range(50):
        assert lease.generate_session_name(repo) != "amber-otter-42"


def test_default_shape_absent_without_the_config_key(tmp_path):
    """Executable code resolves the default shape only from
    config/lease.json's default_shape; a config without the key
    resolves to None and callers fail loudly rather than
    substituting a shape (hardcode strip, 2026-11-03)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "lease.json").write_text(json.dumps({"enabled": True}))
    assert lease.default_shape(tmp_path) is None
    (cfg_dir / "lease.json").write_text(json.dumps({"default_shape": "cpu-highram"}))
    assert lease.default_shape(tmp_path) == "cpu-highram"
