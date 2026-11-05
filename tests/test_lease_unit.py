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


@pytest.fixture(autouse=True)
def gate_permissive(monkeypatch):
    """Session shape gate: these units pin lease mechanics against
    the repo's live config (only var is redirected), so they
    declare the permissive fixture state explicitly — the same
    explicit disable a fixture-owned config would carry. The
    gate's own tests live in test_lease_fake."""
    monkeypatch.setattr(lease, "session_gate_policy", lambda repo: {"enabled": False, "agent_classes": ["high_ram"], "bypass_actors": ["panel"], "infra_actor": "standing"})


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
    var = tmp_path / "var"
    var.mkdir(parents=True)
    (var / "shape-rates.json").write_text(json.dumps({"cpu": 0.5}))
    specs = {s["id"]: s for s in lease.specs(tmp_path)}
    # No CLI cache in the scratch repo: the CPU shapes always serve,
    # with their mechanical labels; a measured rate joins from the
    # rate table, an unmeasured shape's rate stays unconfirmed.
    assert specs["cpu"]["label"] == "CPU"
    assert specs["cpu"]["rate_cu_per_hour"] == 0.5
    assert specs["cpu-highram"]["label"] == "CPU High-RAM"
    assert specs["cpu-highram"]["rate_cu_per_hour"] is None


def test_specs_real_catalog_labels():
    labels = {s["id"]: s["label"] for s in lease.specs(REPO)}
    assert labels["cpu"] == "CPU"
    assert labels["cpu-highram"] == "CPU High-RAM"


def test_live_sessions_empty_listing_has_no_phantom():
    """The CLI answers an empty account with '[colab] No active
    sessions.' — its own log prefix is not a session name, and the
    listing must read as empty, never as a session named 'colab'
    (the phantom the access list stamped unattributed on
    2026-11-05)."""

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp("[colab] No active sessions found on server.\n[colab] No active sessions.")
        return cp("")

    assert lease.live_sessions(stub, REPO) == []


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
    # Sized off the live daily cap so the refusal holds whatever cap
    # the operator has set: the extension's projection alone (hours x
    # the standard rate 0.08) clears the cap. (The fixed 200 h form
    # silently stopped refusing when the live cap rose past 16 CU.)
    cap = lease.load_config(REPO)["daily_cu_budget"]
    hours = int(cap / 0.08) + 50
    out = lease.extend(make_extend_stub(["standard"]), REPO, "standard", hours, now_dt=NOW)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    assert out["budget"]["projected"] == round(hours * 0.08, 2)
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
    rec = lease.load_record(REPO, out["new_session"])
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
    rec = lease.load_record(REPO, out["new_session"])
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
    rec = lease.load_record(REPO, "standard-r999")
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
    lobby = [r for r in out["records"] if r["session"] == "lobby"][0]
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
    assert lease.load_record(REPO)["session"].startswith("standard-r")


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
    # Live standard record: 2 h x 0.08 = 0.16 on top of the workload session's 2.0.
    assert out["day_spent"] == 2.16
    assert out["month_spent"] == 2.16
    assert out["sessions"]["workload"] == {"day_spent": 2.0, "month_spent": 2.0}
    assert out["sessions"]["standard"] == {"day_spent": 0.16, "month_spent": 0.16}


def test_budget_check_balance_floor_refuses_beyond_the_bank(tmp_path, monkeypatch):
    """Caps throttle attributed burn; the balance is the floor: a
    window projecting more than the account holds refuses even with
    zero spend against every cap."""
    var = tmp_path / "var"
    var.mkdir(parents=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    out = lease.budget_check(lambda *a, **k: cp("Current balance: 0.05 compute units"), REPO, lease.load_config(REPO), {"rate_cu_per_hour": 0.08}, hours=8, now_dt=NOW)
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
    assert seg["shape"] == "cpu" and seg["rate"] == 0.08
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
    # The catalog is the CLI space itself, keyed by shape. The
    # legacy profiles file contributes only measured rates, derived
    # per shape from each entry's own fields: the High-RAM CPU
    # entry's rate lands on cpu-highram.
    assert specs["cpu-highram"]["label"] == "CPU High-RAM"
    assert specs["cpu-highram"]["rate_cu_per_hour"] == 0.26
    assert specs["t4"]["rate_cu_per_hour"] is None
    # Everything the CLI offers is served, unconfigured rates
    # unconfirmed.
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
    assert {s["id"] for s in out["specs"]} == {"cpu", "cpu-highram"}


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
    out = lease.acquire(_acquire_stub, REPO, "cpu", now_dt=NOW, hours=24)
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
    out = lease.acquire(_acquire_stub, REPO, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert out["record"]["deadline"] == "2026-10-30T02:00:00+00:00"
    assert "renew" not in out["record"]


def test_acquire_max_hours_cap_still_clamps_its_caller(tmp_path, monkeypatch):
    """The standing duty passes max_hours_cap explicitly for its own
    windows; that parameter — and only that parameter — still
    clamps."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_acquire_stub, REPO, "cpu", now_dt=NOW, hours=24, max_hours_cap=12)
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
    assert lines[0]["shape"] == "cpu"
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
    out = lease.acquire(prompt, REPO, "cpu", now_dt=NOW)
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

    out = lease.budget_check(dead_auth, REPO, lease.load_config(REPO), {"rate_cu_per_hour": 0.08}, hours=1)
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

    out = lease.budget_check(stall, REPO, lease.load_config(REPO), {"high_mem": False, "rate_cu_per_hour": 0.08}, hours=1)
    assert out["ok"] is False
    assert out["reason"] == "usage-unavailable"
    acq = lease.acquire(stall, REPO, "cpu", now_dt=NOW, hours=1)
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
    request's tag (written here in its legacy via spelling) read as
    the caller claim and stamped on the created record."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    alive = set()
    stub = _auto_stub(alive)
    lease.write_request(REPO, reason="timed", shape="cpu", now_dt=NOW, via="timer", not_before=NOW + timedelta(hours=1))
    out = lease.auto_acquire(stub, REPO, now_dt=NOW)
    assert out["action"] == "not-due"
    assert lease.load_request(REPO) is not None
    assert lease.load_record(REPO) is None
    assert alive == set()
    out = lease.auto_acquire(stub, REPO, now_dt=NOW + timedelta(hours=2))
    assert out["action"] == "auto-acquired"
    assert out["caller"] == "timer"
    assert lease.load_request(REPO) is None
    rec = lease.load_record(REPO)
    assert rec is not None and rec["caller"] == "timer"


# --- Concurrent leases (profile retirement) ------------------------------------
# Concurrency is per caller, not per profile: two callers may hold
# leases at the same shape at once, each under its own generated
# session name and record; a caller's own re-acquire adopts its own
# lease and never another caller's. (The retired parallel-instance
# derivation — one extra session per profile, named by rule — is
# gone; this is its successor behaviour.)


def test_two_callers_hold_concurrent_leases_at_one_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))
    alive = set()
    stub = _auto_stub(alive)
    a = lease.acquire(stub, REPO, "cpu", now_dt=NOW, caller="alpha")
    b = lease.acquire(stub, REPO, "cpu", now_dt=NOW, caller="beta")
    assert a["action"] == b["action"] == "created"
    assert a["record"]["session"] != b["record"]["session"]
    assert len(lease.all_records(REPO)) == 2
    again = lease.acquire(stub, REPO, "cpu", now_dt=NOW, caller="alpha")
    assert again["action"] == "adopted"
    assert again["record"]["session"] == a["record"]["session"]
    assert len(lease.all_records(REPO)) == 2


def test_generated_names_never_reuse_a_claimed_name(tmp_path, monkeypatch):
    """A name in the caller-claims journal is never handed out
    again: a stale claim can then never misattribute a later lease
    (the caller-attribution proof's defect, closed by
    construction). Live sessions, records, and the spend journal
    feed the same taken set."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    lease.append_caller_claim(REPO, "amber-otter-42", "alpha", "test")
    assert "amber-otter-42" in lease._taken_session_names(REPO)
    for _ in range(50):
        assert lease.generate_session_name(REPO) != "amber-otter-42"


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


# --- Initiation-time approval (plan 2026-11-03): host on the record ---

HOST_A = "8080-x-a.us-east1.prod.r.colab.dev"
HOST_B = "8080-y-b.asia-east1.prod.r.colab.dev"


def _write_sessions_binding(tmp_path, bindings):
    """A fake local sessions file under a tmp HOME: session name ->
    binding whose url carries the tunnel host (the production shape)."""
    home = tmp_path / "home"
    f = home / ".config" / "colab-cli" / "sessions.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(bindings))
    return home


def _approval_events(tmp_path):
    spool = Path(tmp_path / "var" / "reaper-events.jsonl")
    if not spool.exists():
        return []
    return [e for e in (json.loads(l) for l in spool.read_text().splitlines() if l.strip()) if e.get("kind") == "approval-needed"]


def test_acquire_records_host_and_spools_approval_needed(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "new":
            # The session's name is generated at creation; its
            # binding lands as the session comes up.
            name = a[a.index("-s") + 1]
            f = home / ".config" / "colab-cli" / "sessions.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({name: {"url": f"https://{HOST_A}"}}))
            return cp("created")
        return _acquire_stub(*a, **k)

    out = lease.acquire(stub, REPO, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert out["host_changed"] is True and out["host"] == HOST_A
    rec = lease.load_record(REPO)
    assert rec["host"] == HOST_A
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0] == {"kind": "approval-needed", "shape": "cpu", "session": rec["session"], "host": HOST_A, "previous_host": None, "origin": "acquire"}


def test_acquire_without_binding_records_no_host_and_no_event(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))  # no sessions file at all
    out = lease.acquire(_acquire_stub, REPO, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert "host_changed" not in out
    assert "host" not in lease.load_record(REPO)
    assert _approval_events(tmp_path) == []


def test_renewal_swap_records_new_host_and_spools(tmp_path, monkeypatch):
    child = f"standard-r{int(NOW.timestamp())}"
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_A}"}, child: {"url": f"https://{HOST_B}"}})
    monkeypatch.setenv("HOME", str(home))
    seed_window_record(tmp_path, monkeypatch, host=HOST_A)
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), REPO, now_dt=NOW)
    assert out["action"] == "renewed" and out["driver"] == 2
    rec = lease.load_record(REPO, child)
    assert rec["session"] == child and rec["host"] == HOST_B
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0]["origin"] == "renewal" and events[0]["host"] == HOST_B
    assert events[0]["previous_host"] == HOST_A and events[0]["session"] == child


def test_adopted_baselines_host_without_an_event(tmp_path, monkeypatch):
    seed_record(tmp_path, monkeypatch)  # a pre-feature record: no host key
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_A}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    out = lease.acquire(stub, REPO, "cpu", now_dt=NOW)
    assert out["action"] == "adopted"
    assert lease.load_record(REPO, "standard")["host"] == HOST_A
    assert _approval_events(tmp_path) == []


def test_adopted_host_change_spools_once_and_unchanged_is_silent(tmp_path, monkeypatch):
    rec = seed_record(tmp_path, monkeypatch)
    rec["host"] = HOST_A
    lease.save_record(REPO, rec)
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_B}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    out = lease.acquire(stub, REPO, "cpu", now_dt=NOW)
    assert out["action"] == "adopted"
    assert lease.load_record(REPO, "standard")["host"] == HOST_B
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0]["origin"] == "adopted" and events[0]["previous_host"] == HOST_A
    out2 = lease.acquire(stub, REPO, "cpu", now_dt=NOW)
    assert out2["action"] == "adopted"
    assert len(_approval_events(tmp_path)) == 1  # unchanged host: no second event


def _foreign_stub(*a, **k):
    if a and a[0] == "usage":
        return cp("balance: 500 CU (stub)")
    if a and a[0] == "sessions":
        return cp("workload2")
    if a and a[0] == "download":
        return cp("", rc=1)
    return cp("")


def test_acquire_never_adopts_a_foreign_session(tmp_path, monkeypatch):
    """Successor to the retired adopt-foreign path: a live session
    the lease model did not create is never adopted by an acquire.
    Names are generated unique, so a create mints a fresh session
    beside the foreign one and the record names only its own."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_foreign_stub, REPO, "cpu-highram", now_dt=NOW)
    assert out["action"] == "created"
    assert out["record"]["session"] != "workload2"
    assert lease.load_record(REPO, "workload2") is None


def test_reap_reconciles_a_missing_host_silently(tmp_path, monkeypatch):
    """Regression (2026-11-04): a concurrent writer holding a
    pre-capture copy of the record restored a hostless record after
    the adopt-foreign capture saved — the acquire output was fine
    and nothing re-asserted the host, so the approval gate never
    saw the adopted host. Reap re-derives it from the binding; a
    fill baselines silently (the birth event already spooled)."""
    seed_record(tmp_path, monkeypatch)  # no host key
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_A}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    lease.reap(stub, REPO, now_dt=NOW)
    assert lease.load_record(REPO, "standard")["host"] == HOST_A
    assert _approval_events(tmp_path) == []


def test_reap_reconciles_a_changed_host_and_spools(tmp_path, monkeypatch):
    rec = seed_record(tmp_path, monkeypatch)
    rec["host"] = HOST_A
    lease.save_record(REPO, rec)
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_B}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    lease.reap(stub, REPO, now_dt=NOW)
    assert lease.load_record(REPO, "standard")["host"] == HOST_B
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0]["origin"] == "reconcile" and events[0]["previous_host"] == HOST_A


def _stop_harness(monkeypatch, tmp_path, *, stop_rc, alive, unassign_rc):
    """Wire stop_session_safe's seams to stubs: the colab callable,
    the local binding, the fallback subprocess, and the server-side
    alive read. Returns (colab, fallback_argv_list)."""
    from types import SimpleNamespace

    binding = tmp_path / "sessions.json"
    binding.write_text(json.dumps({"sess": {"endpoint": "m-s-fake-endpoint"}}))
    monkeypatch.setattr(lease, "local_sessions_path", lambda: binding)
    monkeypatch.setattr(lease, "session_alive", lambda c, s: alive)
    monkeypatch.setattr(lease, "time", SimpleNamespace(sleep=lambda s: None))
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return cp(rc=unassign_rc)

    monkeypatch.setattr(lease, "subprocess", SimpleNamespace(run=fake_run))

    def colab(*args, **kwargs):
        return cp(rc=stop_rc)

    return colab, calls


def test_stop_session_safe_stopped_via_cli(monkeypatch, tmp_path):
    colab, calls = _stop_harness(monkeypatch, tmp_path, stop_rc=0, alive=False, unassign_rc=0)
    assert lease.stop_session_safe(colab, "sess") == "stopped"
    assert calls == []


def test_stop_session_safe_fallback_verdict_verified(monkeypatch, tmp_path):
    colab, calls = _stop_harness(monkeypatch, tmp_path, stop_rc=1, alive=False, unassign_rc=0)
    assert lease.stop_session_safe(colab, "sess") == "unassigned-fallback"
    script = calls[0][2]
    assert 'data=b"{}"' in script and "GetUnassignRequest" in script


def test_stop_session_safe_fallback_still_alive_is_failed(monkeypatch, tmp_path):
    colab, _ = _stop_harness(monkeypatch, tmp_path, stop_rc=1, alive=True, unassign_rc=0)
    assert lease.stop_session_safe(colab, "sess") == "stop-failed"


def test_stop_session_safe_fallback_call_failed_is_failed(monkeypatch, tmp_path):
    colab, _ = _stop_harness(monkeypatch, tmp_path, stop_rc=1, alive=False, unassign_rc=1)
    assert lease.stop_session_safe(colab, "sess") == "stop-failed"


def test_no_raw_unassign_call_remains():
    """The bodiless-POST defect (411, 2026-11-04) lived in three raw
    call sites; none may return. The sanctioned form replays the
    client's two-step flow with a body attached."""
    for rel in ("sync/lease.py", "probes/_driver.py", "snippets/unassign_orphans.py"):
        assert "client.unassign(" not in (REPO / rel).read_text(), rel
    assert 'data=b"{}"' in lease.UNASSIGN_FALLBACK_SCRIPT


# --- Caller creation authority (caller-attribution plan, Step 3) -----
# The refusal matrix over enforce_session_admission: past the shape
# check, an agent-class actor's fresh creation is refused on any
# shape; the panel's two halves, a bypass claim, the infra
# exemption, the duty marker, a non-creation consult, and a
# disabled gate each admit. The gate policy is pinned per test;
# the journal lands in the scratched var dir.

HIGH_RAM = {"high_mem": True}
STANDARD = {"high_mem": False}


def _authority_fixture(tmp_path, monkeypatch, enabled=True):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setattr(lease, "session_gate_policy", lambda repo: {"enabled": enabled, "agent_classes": ["high_ram"], "bypass_actors": ["panel", "test"], "infra_actor": "standing"})
    monkeypatch.setattr(lease, "load_config", lambda repo: {"standing": {"shape": "cpu"}})
    monkeypatch.delenv("COLAB_LEASE_ACTOR", raising=False)


def _gate_journal(tmp_path):
    p = tmp_path / "var" / "session-gate-journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_authority_refuses_agent_creation_and_journals(tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    out = lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM, caller="alpha", seam="acquire")
    assert out is not None and out["action"] == "caller-start-refused"
    assert out["basis"] == "agent-creation-denied" and out["caller"] == "alpha"
    journal = _gate_journal(tmp_path)
    assert [e["kind"] for e in journal] == ["caller-start-refused"]
    assert journal[0]["caller"] == "alpha" and journal[0]["seam"] == "acquire"


def test_authority_shape_refusal_precedes_it(tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    out = lease.enforce_session_admission(REPO, "plain", shape=STANDARD, caller="alpha")
    assert out["action"] == "shape-refused"
    assert [e["kind"] for e in _gate_journal(tmp_path)] == ["shape-refused"]


def test_authority_non_creation_consult_and_duty_admit(tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    assert lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM, caller="alpha", creation=False) is None
    assert lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM, caller="alpha", duty=True) is None
    assert _gate_journal(tmp_path) == []


def test_authority_panel_two_halves_and_bypass_claim_admit(tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "panel")
    # One half alone is not authority: the panel env claim without
    # its via corroboration is refused like any agent.
    out = lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM, caller="panel")
    assert out is not None and out["action"] == "caller-start-refused"
    assert lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM, via="panel", caller="panel") is None
    # A whitelisted non-panel claim bypasses on the env half alone.
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "test")
    assert lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM) is None


def test_authority_infra_exemption_and_disabled_gate_admit(tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    assert lease.enforce_session_admission(REPO, "cpu", shape=STANDARD, via="standing", standing=True) is None
    assert [e["kind"] for e in _gate_journal(tmp_path)] == ["shape-infra"]
    _authority_fixture(tmp_path, monkeypatch, enabled=False)
    assert lease.enforce_session_admission(REPO, "main", shape=HIGH_RAM, caller="alpha") is None
