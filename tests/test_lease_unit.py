"""Lease module unit tests with stub colab callables (no subprocess).

Pins the 2026-10-29 failure mode: an unreadable sessions list is UNKNOWN,
never death — reap must not clear the record, and a failed release must
keep the record so the lease is never forgotten while it may still bill.
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lease", REPO / "sync" / "lease.py")
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def seed_record(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    rec = {
        "profile": "standard",
        "session": "standard",
        "acquired_at": "2026-10-29T17:00:00+00:00",
        "deadline": "2026-10-30T01:00:00+00:00",
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": [],
    }
    lease.save_record(REPO, rec)
    return rec


def test_reap_keeps_record_when_sessions_unreadable(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.reap(boom, REPO, now_dt=NOW)
    assert out["action"] == "kept-unknown"
    assert lease.load_record(REPO) is not None


def test_reap_clears_only_on_confirmed_absent(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)
    out = lease.reap(lambda *a, **k: cp("[]"), REPO, now_dt=NOW)
    assert out["action"] == "cleared-dead"
    assert lease.load_record(REPO) is None


def test_reap_pulses_kernel_when_due_and_skips_when_fresh(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)  # no heartbeat file: keepalive relaunch path, harmless here
        return cp("")

    out = lease.reap(stub, REPO, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["pulse"] == "ok"
    execs = [c for c in calls if c and c[0] == "exec" and any("lease-pulse" in str(x) for x in c)]
    assert len(execs) == 1
    assert lease.load_record(REPO)["last_pulse_at"] == NOW.isoformat()
    calls.clear()
    out2 = lease.reap(stub, REPO, now_dt=NOW)
    assert out2["pulse"] == "not-due"
    assert not [c for c in calls if c and c[0] == "exec" and any("lease-pulse" in str(x) for x in c)]


def test_release_keeps_record_when_stop_fails(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)

    def flaky(*a, **k):
        # stop times out; sessions keeps listing the session as alive.
        if a and a[0] == "stop":
            raise subprocess.TimeoutExpired("colab", 120)
        return cp('["standard"]')

    out = lease.release(flaky, REPO)
    assert out["action"] == "release-failed"
    assert lease.load_record(REPO) is not None


def test_heartbeat_age_stall_degrades_to_unknown_with_short_bound(tmp_path, monkeypatch):
    """A stalled heartbeat download (the file channel can hang on the
    egress proxy's IPv4 endpoint, diagnosed 2026-11-01) must degrade
    to age-unknown quickly — never wedge a status/state read behind
    the old 120 s bound."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    seen = {}

    def stall(*a, **k):
        seen["args"] = a
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired("colab", k.get("timeout"))

    assert lease.heartbeat_age(stall, REPO, "standard") is None
    assert seen["args"][0] == "download"
    assert seen["timeout"] == lease.HEARTBEAT_READ_TIMEOUT_S
    assert lease.HEARTBEAT_READ_TIMEOUT_S <= 15


def test_heartbeat_age_reads_a_fresh_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))

    def fetch(*a, **k):
        dest = Path(a[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).timestamp() - 42
        dest.write_text(json.dumps({"ts": ts}) + "\n")
        return cp("")

    age = lease.heartbeat_age(fetch, REPO, "standard")
    assert age is not None and 40 <= age <= 45


def test_status_survives_a_stalled_heartbeat_download(tmp_path, monkeypatch):
    """The UI's read path: session live + heartbeat download stalled
    still returns the status, with the age reported as unknown."""
    seed_record(tmp_path, monkeypatch)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        return cp("")

    out = lease.status(stub, REPO)
    assert out["action"] == "active"
    assert out["keepalive_heartbeat_age_s"] is None


def test_ensure_keepalive_stalled_upload_is_a_result_not_an_exception(tmp_path, monkeypatch):
    """The keepalive upload rides the stall-prone file channel: a stall
    must come back as launch-failed (lease stands, caller carries on),
    bounded well under the old 300 s — never as a raised timeout."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    seen = {}

    def stall_upload(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired("colab", k.get("timeout"))

    out = lease.ensure_keepalive(stall_upload, REPO, "standard")
    assert out["keepalive"] == "launch-failed"
    assert out["stage"] == "upload"
    assert seen["timeout"] == lease.KEEPALIVE_UPLOAD_TIMEOUT_S
    assert lease.KEEPALIVE_UPLOAD_TIMEOUT_S <= 30


def test_ensure_keepalive_stalled_launch_is_a_result_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))

    def stall_launch(*a, **k):
        if a and a[0] == "exec":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        return cp("")

    out = lease.ensure_keepalive(stall_launch, REPO, "standard")
    assert out["keepalive"] == "launch-failed"
    assert out["stage"] == "launch"


def test_reap_pulses_even_when_keepalive_relaunch_stalls(tmp_path, monkeypatch):
    """Regression for 2026-11-01: reap died inside the keepalive
    relaunch's stalled upload before ever reaching the kernel pulse,
    leaving the lease with no activity channel at all. The pulse is
    an exec — it must not be starved by a file-channel failure."""
    seed_record(tmp_path, monkeypatch)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)  # no heartbeat: relaunch path
        if a and a[0] == "upload":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        if a and a[0] == "exec" and any("lease-pulse" in str(x) for x in a):
            return cp("lease pulse")
        return cp("")

    out = lease.reap(stub, REPO, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["keepalive"] == "launch-failed"
    assert out["pulse"] == "ok"


def test_specs_serves_catalog_labels_with_id_fallback(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "profiles.json").write_text(json.dumps({
        "default_profile": "a",
        "profiles": {
            "a": {"session": "a", "label": "Alpha", "rate_cu_per_hour": 0.5},
            "b": {"session": "b", "rate_cu_per_hour": None},
        },
    }))
    specs = {s["id"]: s for s in lease.specs(tmp_path)}
    assert specs["a"]["label"] == "Alpha"
    # a profile without a catalog label falls back to its id
    assert specs["b"]["label"] == "b"


def test_specs_real_catalog_labels():
    labels = {s["id"]: s["label"] for s in lease.specs(REPO)}
    assert labels["workload"] == "High-RAM"
    assert labels["standard"] == "Standard"
    assert labels["lobby"] == "Lobby"
    assert labels["t4"] == "T4"


def make_extend_stub(alive_sessions):
    def stub(*a, **k):
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(alive_sessions))
        return cp("")

    return stub


def test_extend_happy_path_moves_deadline_and_prices_delta(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))  # no token here: the kit install reports token-missing, non-fatally
    out = lease.extend(make_extend_stub(["standard"]), REPO, "standard", 2, now_dt=NOW)
    assert out["action"] == "extended"
    assert out["old_deadline"] == "2026-10-30T01:00:00+00:00"
    assert out["new_deadline"] == "2026-10-30T03:00:00+00:00"
    # Delta pricing: the standard rate (0.08) x the added hours only.
    assert out["budget"]["projected"] == 0.16
    rec = lease.load_record(REPO, "standard")
    assert rec["deadline"] == "2026-10-30T03:00:00+00:00"
    assert rec["renew"] is True
    assert rec["jobs"] == []  # the jobs list is untouched


def test_extend_refused_budget_changes_nothing(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)
    # 200 h x 0.08 = 16 CU against the 10 CU daily cap.
    out = lease.extend(make_extend_stub(["standard"]), REPO, "standard", 200, now_dt=NOW)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    assert out["budget"]["projected"] == 16.0
    rec = lease.load_record(REPO, "standard")
    assert rec["deadline"] == "2026-10-30T01:00:00+00:00"
    assert "renew" not in rec


def test_extend_unknown_session_state_refuses(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.extend(boom, REPO, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-unknown"
    assert lease.load_record(REPO, "standard")["deadline"] == "2026-10-30T01:00:00+00:00"


def test_extend_absent_window_lease_applies_to_the_window(tmp_path, monkeypatch):
    rec = seed_record(tmp_path, monkeypatch)
    rec["renew"] = True
    lease.save_record(REPO, rec)
    out = lease.extend(make_extend_stub([]), REPO, "standard", 2, now_dt=NOW)
    assert out["action"] == "extended"
    assert lease.load_record(REPO, "standard")["deadline"] == "2026-10-30T03:00:00+00:00"


def test_extend_absent_without_renew_refuses(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)
    out = lease.extend(make_extend_stub([]), REPO, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-no-lease"
    assert lease.load_record(REPO, "standard")["deadline"] == "2026-10-30T01:00:00+00:00"


def test_extend_without_record_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    out = lease.extend(make_extend_stub(["standard"]), REPO, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-no-lease"


def test_extend_repeated_extensions_accumulate(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = make_extend_stub(["standard"])
    assert lease.extend(stub, REPO, "standard", 2, now_dt=NOW)["action"] == "extended"
    out = lease.extend(stub, REPO, "standard", 3, now_dt=NOW)
    assert out["old_deadline"] == "2026-10-30T03:00:00+00:00"
    assert out["new_deadline"] == "2026-10-30T06:00:00+00:00"
    # Each extension prices its own delta.
    assert out["budget"]["projected"] == 0.24


def test_release_guard_aborts_when_deadline_moved_on_disk(tmp_path, monkeypatch):
    """An extension landing between the caller's read and the stop
    wins: release() re-reads the record immediately before stopping,
    sees the deadline moved into the future, and aborts — no stop is
    issued and the record stands."""
    seed_record(tmp_path, monkeypatch)
    real_load = lease.load_record
    reads = {"n": 0}

    def moved_load(repo, profile=None):
        reads["n"] += 1
        rec = real_load(repo, profile)
        if reads["n"] > 1 and rec is not None:
            rec = dict(rec, deadline="2026-10-30T05:00:00+00:00")
        return rec

    monkeypatch.setattr(lease, "load_record", moved_load)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        return cp('["standard"]')

    out = lease.release(stub, REPO)
    assert out["action"] == "release-aborted"
    assert out["guard"] == "deadline-moved"
    assert not [c for c in calls if c and c[0] == "stop"]
    assert lease.load_record(REPO) is not None


def seed_window_record(tmp_path, monkeypatch, **over):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    rec = {
        "profile": "standard",
        "session": "standard",
        # 12 h before NOW: at the renewal threshold (cap 12 h - 30 min).
        "acquired_at": "2026-10-29T06:00:00+00:00",
        "deadline": "2026-10-30T06:00:00+00:00",  # window still open
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": ["run-1"],
        "renew": True,
    }
    rec.update(over)
    lease.save_record(REPO, rec)
    return rec


def make_renewal_stub(alive, calls, fail_new=None, handover=None):
    def stub(*a, **k):
        calls.append(a)
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(sorted(alive)))
        if cmd == "new":
            if fail_new is not None and fail_new():
                return cp("boom", rc=1)
            alive.add(a[a.index("-s") + 1])
            return cp("created")
        if cmd == "stop":
            alive.discard(a[a.index("-s") + 1])
            return cp("stopped")
        if cmd == "download":
            if handover is not None and str(a[3]).endswith("lease-handover.json"):
                dest = Path(a[-1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(handover))
                return cp("downloaded")
            return cp("", rc=1)  # no heartbeat file in the stub world
        return cp("")

    return stub


def test_renewal_threshold_driver2_swaps_session_keeps_window(tmp_path, monkeypatch):
    seed_window_record(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))  # kit install: token-missing, non-fatal
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), REPO, now_dt=NOW)
    assert out["action"] == "renewed"
    assert out["driver"] == 2
    assert out["old_session"] == "standard"
    rec = lease.load_record(REPO, "standard")
    assert rec["session"] == out["new_session"]
    assert rec["session"].startswith("standard-r")
    # The window is preserved exactly; only the session + its start move.
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    assert rec["jobs"] == ["run-1"]
    assert rec["acquired_at"] == NOW.isoformat()
    assert "standard" not in alive  # parent stopped after the swap
    assert rec["session"] in alive


def test_renewal_absent_session_driver2(tmp_path, monkeypatch):
    seed_window_record(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    alive, calls = set(), []
    out = lease.reap(make_renewal_stub(alive, calls), REPO, now_dt=NOW)
    # A dead session on a window lease renews instead of clearing.
    assert out["action"] == "renewed"
    assert out["driver"] == 2
    rec = lease.load_record(REPO, "standard")
    assert rec is not None
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    assert rec["session"] in alive


def test_renewal_driver1_adopts_handover_and_merges_binding(tmp_path, monkeypatch):
    home = tmp_path / "home"
    sessions_file = home / ".config" / "colab-cli" / "sessions.json"
    sessions_file.parent.mkdir(parents=True)
    sessions_file.write_text(json.dumps({"standard": {"endpoint": "https://parent.invalid"}}))
    monkeypatch.setenv("HOME", str(home))
    seed_window_record(tmp_path, monkeypatch, kit="installed")
    child_entry = {"endpoint": "https://child.invalid", "runtime": "child-runtime"}
    handover = {
        "child_session": "standard-r999",
        "binding": {"standard-r999": child_entry},
        "parent_session": "standard",
        "profile": "standard",
        "deadline": "2026-10-30T06:00:00+00:00",
        "minted_at": "2026-10-29T17:59:00+00:00",
    }
    alive, calls = {"standard", "standard-r999"}, []
    out = lease.reap(make_renewal_stub(alive, calls, handover=handover), REPO, now_dt=NOW)
    assert out["action"] == "renewed"
    assert out["driver"] == 1
    assert out["old_session"] == "standard"
    assert out["new_session"] == "standard-r999"
    rec = lease.load_record(REPO, "standard")
    assert rec["session"] == "standard-r999"
    assert rec["acquired_at"] == "2026-10-29T17:59:00+00:00"  # the child's own start
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    # The binding merge landed in the local sessions file; the parent's
    # entry is untouched and the parent was stopped after the swap.
    merged = json.loads(sessions_file.read_text())
    assert merged["standard-r999"] == child_entry
    assert merged["standard"] == {"endpoint": "https://parent.invalid"}
    assert "standard" not in alive  # the stub's stop discarded the parent
    assert any(c and c[0] == "stop" and "standard" in c for c in calls)


def test_renewal_skips_unflagged_records(tmp_path, monkeypatch):
    rec = seed_window_record(tmp_path, monkeypatch)
    del rec["renew"]
    lease.save_record(REPO, rec)
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), REPO, now_dt=NOW)
    assert out["action"] == "kept"
    assert not [c for c in calls if c and c[0] == "new"]


def test_renewal_skips_standing_records(tmp_path, monkeypatch):
    seed_window_record(tmp_path, monkeypatch, profile="lobby", session="lobby", standing=True)
    lease.write_grant(REPO, 24, now_dt=NOW)
    alive, calls = {"lobby"}, []
    out = lease.reap(make_renewal_stub(alive, calls), REPO, now_dt=NOW)
    lobby = [r for r in out["records"] if r["profile"] == "lobby"][0]
    assert lobby["action"] == "kept"  # standing duty semantics, not renewal
    assert not [c for c in calls if c and c[0] == "new"]


def test_failed_renewal_retries_next_cycle_deadline_untouched(tmp_path, monkeypatch):
    seed_window_record(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    alive, calls = {"standard"}, []
    failing = {"on": True}
    stub = make_renewal_stub(alive, calls, fail_new=lambda: failing["on"])
    out = lease.reap(stub, REPO, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["renewal"] == "failed"
    rec = lease.load_record(REPO, "standard")
    assert rec["session"] == "standard"
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    failing["on"] = False
    out2 = lease.reap(stub, REPO, now_dt=NOW)
    assert out2["action"] == "renewed"
    assert lease.load_record(REPO, "standard")["session"].startswith("standard-r")


def test_budget_snapshot_serves_standing_day_spend(tmp_path, monkeypatch):
    """The grant card renders standing spend vs cap (mock Panel C), so
    state must serve the standing profile's day spend — the guard's
    own attributed figure (journaled segments + live records), never
    a balance delta: the same spend must be served whatever the
    balance did in between (external usage, expirations)."""
    var = tmp_path / "var"
    var.mkdir(parents=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max((now - day_start).total_seconds() / 3600.0, 1 / 60)
    # One lobby segment covering today so far, rated to total 2.42 CU.
    seg = {"profile": "lobby", "session": "lobby", "start": day_start.isoformat(), "end": now.isoformat(), "rate": 2.42 / hours}
    (var / "lease-spend-journal.jsonl").write_text(json.dumps(seg) + "\n")

    out = lease.budget_snapshot(lambda *a, **k: cp("Current balance: 768.38 compute units"), REPO)
    assert out["balance"] == 768.38
    assert out["standing_day_spent"] == 2.42
    assert out["day_spent"] == 2.42
    # The served cap is the live config's (the operator's panel
    # setting), read here rather than pinned to a constant.
    assert out["caps"]["standing_daily"] == lease.load_config(REPO)["standing"]["daily_cu_cap"]

    # A balance collapse (the 2026-11-01 expiration step) moves
    # nothing: spend is attributed, not balance-derived.
    out2 = lease.budget_snapshot(lambda *a, **k: cp("Current balance: 699.00 compute units"), REPO)
    assert out2["standing_day_spent"] == 2.42
    assert out2["day_spent"] == 2.42

    # No segments and no records -> 0.0, not a fabricated number.
    (var / "lease-spend-journal.jsonl").write_text("")
    out3 = lease.budget_snapshot(lambda *a, **k: cp("Current balance: 768.38 compute units"), REPO)
    assert out3["standing_day_spent"] == 0.0


def test_attributed_spend_splits_segments_at_period_boundaries(tmp_path, monkeypatch):
    """The derivation itself: journaled segments split at the day and
    month boundaries, live records contribute their running segment,
    and unconfirmed-rate work contributes nothing."""
    var = tmp_path / "var"
    var.mkdir(parents=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    segs = [
        # Spans midnight into today: 2 h count today (and this month).
        {"profile": "workload", "session": "workload", "start": "2026-10-31T22:00:00+00:00", "end": "2026-11-01T02:00:00+00:00", "rate": 1.0},
        # Last month only: counts for neither period.
        {"profile": "workload", "session": "workload", "start": "2026-10-15T00:00:00+00:00", "end": "2026-10-15T10:00:00+00:00", "rate": 1.0},
        # Unconfirmed rate: journaled, never priced.
        {"profile": "standard", "session": "s", "start": "2026-11-01T00:00:00+00:00", "end": "2026-11-01T05:00:00+00:00", "rate": None},
    ]
    (var / "lease-spend-journal.jsonl").write_text("".join(json.dumps(s) + "\n" for s in segs))
    lease.save_record(REPO, {"profile": "standard", "session": "standard", "acquired_at": "2026-11-01T10:00:00+00:00", "deadline": "2026-11-01T20:00:00+00:00", "last_used_at": "2026-11-01T10:00:00+00:00", "jobs": []})
    out = lease.attributed_spend(REPO, now)
    # Live standard record: 2 h x 0.08 = 0.16 on top of workload's 2.0.
    assert out["day_spent"] == 2.16
    assert out["month_spent"] == 2.16
    assert out["profiles"]["workload"] == {"day_spent": 2.0, "month_spent": 2.0}
    assert out["profiles"]["standard"] == {"day_spent": 0.16, "month_spent": 0.16}


def test_budget_check_balance_floor_refuses_beyond_the_bank(tmp_path, monkeypatch):
    """Caps throttle attributed burn; the balance is the floor: a
    window projecting more than the account holds refuses even with
    zero spend against every cap."""
    var = tmp_path / "var"
    var.mkdir(parents=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    out = lease.budget_check(lambda *a, **k: cp("Current balance: 0.05 compute units"), REPO, lease.load_config(REPO), {"rate_cu_per_hour": 0.08}, hours=8, profile_name="standard", now_dt=NOW)
    assert out["ok"] is False
    assert out["reason"] == "balance-insufficient"
    assert out["projected"] == 0.64
    assert out["day_spent"] == 0.0


def test_release_journals_the_completed_segment(tmp_path, monkeypatch):
    """Release closes the session's segment into the journal, so its
    burn stays counted after the record is gone."""
    seed_record(tmp_path, monkeypatch)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("")

    out = lease.release(stub, REPO, now_dt=NOW)
    assert out["action"] == "released"
    lines = (tmp_path / "var" / "lease-spend-journal.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    seg = json.loads(lines[0])
    assert seg["profile"] == "standard" and seg["rate"] == 0.08
    # Seeded record ran 17:00 -> NOW (18:00): 1 h x 0.08.
    assert lease.attributed_spend(REPO, NOW)["day_spent"] == 0.08


# --- The dynamic spec catalog (2026-11-01) ------------------------------------
# The picker options are parsed from `colab new --help`, never
# hand-listed; config/profiles.json holds only named profiles.

HELP_TEXT = """
 Usage: colab new [OPTIONS]

 Create a new session

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session   -s      <str>  Session name                                      │
│ --tpu               <str>  TPU accelerator variant. Supported: v5e1, v6e1.   │
│ --gpu               <str>  GPU accelerator variant. Supported: T4, L4, G4,   │
│                            H100, A100.                                       │
│                            If omitted (along with --tpu), a CPU runtime is   │
│                            created.                                          │
│ --high-mem                 Request a high-RAM machine shape (CPU, T4, A100,  │
│                            etc.). Requires Colab Pro or Pro+ entitlement.    │
│                            Ignored for accelerators that only offer a single │
│                            shape (L4, v5e1, v6e1).                              │
│ --help      -h             Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
"""


def test_parse_cli_shapes_reads_the_real_help():
    shapes = lease.parse_cli_shapes(HELP_TEXT)
    assert shapes["gpu"] == ["T4", "L4", "G4", "H100", "A100"]
    assert shapes["tpu"] == ["v5e1", "v6e1"]
    assert shapes["no_high_mem"] == ["L4", "v5e1", "v6e1"]


def test_parse_cli_shapes_rejects_unfamiliar_help():
    assert lease.parse_cli_shapes("Usage: colab new [OPTIONS]\nno variants here") is None
    assert lease.parse_cli_shapes("") is None


def _help_stub(help_text=HELP_TEXT):
    def stub(*a, **k):
        if a[:2] == ("new", "--help"):
            return cp(help_text)
        if a and a[0] == "usage":
            return cp("balance: 500 CU (stub)")
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("")

    return stub


def _tmp_catalog(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "profiles.json").write_text(json.dumps({
        "default_profile": "workload",
        "profiles": {
            "workload": {"label": "High-RAM", "high_mem": True, "rate_cu_per_hour": 0.26, "session": "workload", "timeout": 18000},
            "t4": {"label": "T4", "gpu": "T4", "high_mem": False, "rate_cu_per_hour": None, "session": "t4", "timeout": 18000},
        },
    }))
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))


def test_specs_dynamic_catalog_covers_the_cli_space(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    specs = {s["id"]: s for s in lease.specs(tmp_path, _help_stub())}
    # Configured profiles serve as themselves (workload covers CPU
    # high-mem, t4 covers the T4 shape): no dynamic duplicates.
    assert specs["workload"]["label"] == "High-RAM"
    assert "cpu-highram" not in specs
    # Everything the CLI offers beyond the configured catalog is
    # dynamic, with an unconfirmed rate.
    for pid in ("l4", "g4", "h100", "a100", "a100-highram", "t4-highram", "tpu-v5e1", "tpu-v6e1", "cpu"):
        assert pid in specs, pid
        assert specs[pid]["rate_cu_per_hour"] is None
    # L4 is on the CLI's single-shape list: no high-RAM variant exists.
    assert "l4-highram" not in specs
    assert specs["l4"]["gpu"] == "L4" and specs["l4"]["high_mem"] is False
    assert specs["a100-highram"]["gpu"] == "A100" and specs["a100-highram"]["high_mem"] is True
    assert specs["tpu-v6e1"]["tpu"] == "v6e1" and specs["tpu-v6e1"]["gpu"] is None


def test_specs_catalog_is_cached_across_calls(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    calls = []
    base = _help_stub()

    def counting(*a, **k):
        if a[:2] == ("new", "--help"):
            calls.append(a)
        return base(*a, **k)

    lease.specs(tmp_path, counting)
    lease.specs(tmp_path, counting)
    assert len(calls) == 1


def test_state_surfaces_a_catalog_failure(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    out = lease.state(_help_stub("garbage, no variants"), tmp_path)
    assert "catalog_error" in out
    assert {s["id"] for s in out["specs"]} == {"workload", "t4"}


def test_acquire_dynamic_profiles_pass_their_cli_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
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

    out = lease.acquire(stub, REPO, "l4", now_dt=NOW)
    assert out["action"] == "created"
    assert ("--gpu", "L4") in zip(news[0], news[0][1:])
    out2 = lease.acquire(stub, REPO, "tpu-v6e1", now_dt=NOW)
    assert out2["action"] == "created"
    assert ("--tpu", "v6e1") in zip(news[1], news[1][1:])


def _acquire_stub(*a, **k):
    if a and a[0] == "usage":
        return cp("balance: 500 CU (stub)")
    if a and a[0] == "sessions":
        return cp("[]")
    if a and a[0] == "new":
        return cp("created")
    if a and a[0] == "download":
        return cp("", rc=1)
    return cp("")


def test_acquire_honors_an_explicit_window_over_max_hours(tmp_path, monkeypatch):
    """Regression (2026-11-01): acquire silently clamped every
    explicit hours value to the config max_hours, so an ordered 24 h
    endurance window came up as 8 h, unflagged for renewal, and the
    budget priced the clamped window. An explicit window is honored
    as given; past the session cap the lease is a window lease from
    birth (renew flagged, delta priced on the full window)."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))  # no token here: the kit install reports token-missing, non-fatally
    out = lease.acquire(_acquire_stub, REPO, "standard", now_dt=NOW, hours=24)
    assert out["action"] == "created"
    rec = out["record"]
    assert rec["deadline"] == "2026-10-30T18:00:00+00:00"
    assert rec["renew"] is True
    assert rec["budget_at_acquire"]["projected"] == 1.92


def test_acquire_default_window_is_config_max_hours(tmp_path, monkeypatch):
    """Without hours, the window is the config max_hours (8) — the
    pre-existing default behavior is unchanged by the clamp fix."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_acquire_stub, REPO, "standard", now_dt=NOW)
    assert out["action"] == "created"
    assert out["record"]["deadline"] == "2026-10-30T02:00:00+00:00"
    assert "renew" not in out["record"]


def test_acquire_max_hours_cap_still_clamps_its_caller(tmp_path, monkeypatch):
    """The standing duty passes max_hours_cap explicitly for its own
    windows; that parameter — and only that parameter — still
    clamps."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_acquire_stub, REPO, "standard", now_dt=NOW, hours=24, max_hours_cap=12)
    assert out["action"] == "created"
    assert out["record"]["deadline"] == "2026-10-30T06:00:00+00:00"


def test_renewal_spools_a_lease_renewed_event_for_the_outbox(tmp_path, monkeypatch):
    """Renewals happen in reap(), outside the companion server — the
    outbox's only writer — so the core spools the event and the
    server drains it (see drain_reaper_events in ui/server.py)."""
    seed_window_record(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), REPO, now_dt=NOW)
    assert out["action"] == "renewed"
    spool = Path(tmp_path / "var" / "reaper-events.jsonl")
    lines = [json.loads(l) for l in spool.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "lease-renewed"
    assert lines[0]["profile"] == "standard"
    assert lines[0]["driver"] == 2
    assert lines[0]["old_session"] == "standard"
    assert lines[0]["new_session"] == out["new_session"]


def test_session_state_auth_prompt_is_unknown_not_absent(tmp_path, monkeypatch):
    """2026-11-01: with the credential dead, the CLI printed its
    interactive login prompt where a session list was expected. The
    prompt is not a list: the state is unknown, never absent — absent
    is the one reading that deletes records."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    prompt = lambda *a, **k: cp("Enter the authorization code:")  # noqa: E731
    assert lease.session_state(prompt, "standard") == "unknown"


def test_acquire_never_destroys_record_on_unreadable_sessions(tmp_path, monkeypatch):
    """The 2026-11-01 record loss, pinned: a live lease whose session
    list comes back as the auth prompt (or any unreadable output) must
    survive acquire untouched — no stop, no delete, no recreate."""
    rec = seed_record(tmp_path, monkeypatch)
    prompt = lambda *a, **k: cp("Enter the authorization code:")  # noqa: E731
    out = lease.acquire(prompt, REPO, "standard", now_dt=NOW)
    assert out["action"] == "acquire-unknown"
    kept = lease.load_record(REPO, "standard")
    assert kept is not None
    assert kept["deadline"] == rec["deadline"]
    assert kept["acquired_at"] == rec["acquired_at"]


def test_budget_check_maps_auth_required(tmp_path, monkeypatch):
    """The wrapper guard's stop-and-surface signal is its own refusal
    reason, distinct from a stall or a parse failure."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))

    def dead_auth(*a, **k):
        return subprocess.CompletedProcess([], 3, "", "colab auth required: stored token missing or invalid")

    out = lease.budget_check(dead_auth, REPO, lease.load_config(REPO), {"rate_cu_per_hour": 0.08}, hours=1, profile_name="standard")
    assert out == {"ok": False, "reason": "auth-required"}


def test_state_surfaces_auth_required_marker(tmp_path, monkeypatch):
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    assert lease.auth_required(REPO) is None
    (var / "auth-required.json").write_text(json.dumps({"noted_at": "2026-11-01T13:05:58+00:00", "reason": "test"}))

    def stub(*a, **k):
        if a and a[0] == "usage":
            return cp("Current balance: 100 compute units")
        return cp("[]")

    out = lease.state(stub, REPO)
    assert out["auth_required"]["reason"] == "test"


def test_budget_check_usage_timeout_is_a_refusal_not_a_crash(tmp_path, monkeypatch):
    """The usage read rides the stall-prone proxy (a 120 s timeout
    fired live on 2026-11-01 and crashed acquire with a traceback):
    it must come back as a clean refused-budget — the guard cannot
    price without a balance — never as a raised exception."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))

    def stall(*a, **k):
        if a and a[0] == "usage":
            raise subprocess.TimeoutExpired("colab", 120)
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("")

    out = lease.budget_check(stall, REPO, lease.load_config(REPO), {"high_mem": False, "rate_cu_per_hour": 0.08}, hours=1, profile_name="standard")
    assert out["ok"] is False
    assert out["reason"] == "usage-unavailable"
    acq = lease.acquire(stall, REPO, "standard", now_dt=NOW, hours=1)
    assert acq["action"] == "refused-budget"


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


def _auto_stub(alive):
    def stub(*a, **k):
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(sorted(alive)))
        if cmd == "new":
            alive.add(a[a.index("-s") + 1])
            return cp("created")
        if cmd == "download":
            return cp("", rc=1)
        return cp("")

    return stub


def test_auto_timed_start_waits_then_fires(tmp_path, monkeypatch):
    """A demand with a future not_before survives an auto pass
    untouched (not consumed, not failed, no lease), then acquires on
    the first pass after its time — a timed start as data, with the
    request's via tag copied onto the created record."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    alive = set()
    stub = _auto_stub(alive)
    lease.write_request(REPO, reason="timed", profile="standard", now_dt=NOW, via="timer", not_before=NOW + timedelta(hours=1))
    out = lease.auto_acquire(stub, REPO, now_dt=NOW)
    assert out["action"] == "not-due"
    assert lease.load_request(REPO) is not None
    assert lease.load_record(REPO, "standard") is None
    assert alive == set()
    out = lease.auto_acquire(stub, REPO, now_dt=NOW + timedelta(hours=2))
    assert out["action"] == "auto-acquired"
    assert lease.load_request(REPO) is None
    rec = lease.load_record(REPO, "standard")
    assert rec is not None and rec["via"] == "timer"


# --- Parallel instances (plan: .plans/2026-11-03-parallel-cpu-sessions.md) -----
# A low-tier CPU profile may run one extra session next to its primary,
# gated by a per-profile toggle in config/lease.json. Eligibility is the
# core's determination, served ready in each spec's parallel block.


def _seed_parallel_repo(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "profiles.json").write_text(json.dumps({
        "default_profile": "workload",
        "profiles": {
            "workload": {"label": "High-RAM", "high_mem": True, "rate_cu_per_hour": 0.26, "session": "workload", "timeout": 18000},
            "standard": {"label": "Standard", "high_mem": False, "rate_cu_per_hour": 0.08, "session": "standard", "timeout": 3600},
            "t4": {"label": "T4", "gpu": "T4", "high_mem": False, "rate_cu_per_hour": None, "session": "t4", "timeout": 18000},
        },
    }))
    (cfg_dir / "lease.json").write_text(json.dumps({"enabled": True, "daily_cu_budget": 15, "monthly_cu_budget": 150, "max_hours": 8, "keepalive": {"enabled": True}}, sort_keys=True, separators=(",", ":")))
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    return tmp_path


def test_parallel_eligibility_is_served_per_spec(tmp_path, monkeypatch):
    """CPU standard and high-RAM specs serve eligible; every GPU/TPU
    spec serves ineligible; the toggle defaults off everywhere."""
    _seed_parallel_repo(tmp_path, monkeypatch)
    specs = {s["id"]: s for s in lease.specs(tmp_path, _help_stub())}
    assert specs["workload"]["parallel"] == {"eligible": True, "enabled": False, "instance": "workload2"}
    assert specs["standard"]["parallel"]["eligible"] is True
    for pid, spec in specs.items():
        if spec["gpu"] or spec["tpu"]:
            assert spec["parallel"]["eligible"] is False, pid
            assert spec["parallel"]["enabled"] is False, pid
    assert "workload2" not in specs  # nothing registered while the toggle is off


def test_set_parallel_roundtrip_and_instance_registration(tmp_path, monkeypatch):
    """The toggle persists in config/lease.json (other keys preserved),
    registers the derived instance profile in the catalog and the
    served specs, and unregisters on off."""
    _seed_parallel_repo(tmp_path, monkeypatch)
    out = lease.set_parallel(tmp_path, "workload", True)
    assert out == {"action": "parallel-updated", "profile": "workload", "enabled": True, "instance": "workload2", "previous": False}
    cfg = json.loads((tmp_path / "config" / "lease.json").read_text())
    assert cfg["parallel"] == {"workload": True}
    assert cfg["daily_cu_budget"] == 15 and cfg["enabled"] is True
    catalog = lease.catalog_profiles(tmp_path)
    inst = catalog["workload2"]
    assert inst["session"] == "workload2" and inst["high_mem"] is True and inst["rate_cu_per_hour"] == 0.26
    assert inst["parallel_of"] == "workload"
    specs = {s["id"]: s for s in lease.specs(tmp_path)}
    assert specs["workload"]["parallel"]["enabled"] is True
    assert specs["workload2"]["parallel"] == {"eligible": False, "enabled": True, "instance": None, "parallel_of": "workload"}
    out = lease.set_parallel(tmp_path, "workload", False)
    assert out["enabled"] is False and out["previous"] is True
    assert "workload2" not in lease.catalog_profiles(tmp_path)
    cfg = json.loads((tmp_path / "config" / "lease.json").read_text())
    assert cfg["parallel"] == {}


def test_set_parallel_refusals(tmp_path, monkeypatch):
    """Accelerator profiles and instances themselves are ineligible,
    unknown profiles are refused, and toggle-off refuses while the
    instance lease exists — the toggle never stops a session."""
    _seed_parallel_repo(tmp_path, monkeypatch)
    assert lease.set_parallel(tmp_path, "t4", True)["action"] == "parallel-ineligible"
    assert lease.set_parallel(tmp_path, "nope", True)["action"] == "parallel-unknown-profile"
    lease.set_parallel(tmp_path, "workload", True)
    assert lease.set_parallel(tmp_path, "workload2", True)["action"] == "parallel-ineligible"
    rec = {"profile": "workload2", "session": "workload2", "acquired_at": NOW.isoformat(), "deadline": (NOW + timedelta(hours=8)).isoformat(), "last_used_at": NOW.isoformat(), "jobs": []}
    lease.save_record(tmp_path, rec)
    assert lease.set_parallel(tmp_path, "workload", False)["action"] == "parallel-instance-live"
    assert lease.parallel_enabled(tmp_path, "workload") is True  # unchanged by the refusal
    lease.save_record(tmp_path, None, profile="workload2")
    assert lease.set_parallel(tmp_path, "workload", False)["action"] == "parallel-updated"


def test_default_profile_fails_loudly_without_the_config_key(tmp_path):
    """Executable code resolves the default profile only from
    config/profiles.json; a config without the key is an error,
    never a silently substituted workload name (hardcode strip,
    2026-11-03)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "profiles.json").write_text(json.dumps({"profiles": {}}))
    with pytest.raises(ValueError):
        lease.default_profile(tmp_path)
    (cfg_dir / "profiles.json").write_text(json.dumps({"default_profile": "someprofile", "profiles": {}}))
    assert lease.default_profile(tmp_path) == "someprofile"
