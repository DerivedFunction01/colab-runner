"""Lease module unit tests with stub colab callables (no subprocess).

Pins the 2026-10-29 failure mode: an unreadable sessions list is UNKNOWN,
never death — reap must not clear the record, and a failed release must
keep the record so the lease is never forgotten while it may still bill.
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
import sys
spec = importlib.util.spec_from_file_location(
    "lease", REPO / "sync" / "lease" / "__init__.py",
    submodule_search_locations=[str(REPO / "sync" / "lease")])
lease = importlib.util.module_from_spec(spec)
sys.modules["lease"] = lease
spec.loader.exec_module(lease)

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A scratch repo carrying the authored fixture config (see
    tests/fixtures/repo_fixture.py): the suite must pass in a
    bare clone, where the checkout has no operator config."""
    root = repo_fixture.scratch_repo(tmp_path)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    return root


@pytest.fixture(autouse=True)
def gate_permissive(monkeypatch):
    """Session shape gate: these units pin lease mechanics against
    the fixture repo's authored config, so they declare the
    permissive fixture state explicitly — the same explicit
    disable a fixture-owned config would carry. The gate's own
    tests live in test_lease_fake."""
    monkeypatch.setattr(lease.gate, "session_gate_policy", lambda repo: {"enabled": False, "agent_classes": ["high_ram"], "bypass_actors": ["panel"], "infra_actor": "standing"})


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def seed_record(repo):
    rec = {
        "session": "standard",
        "shape": "cpu",
        "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "rate_cu_per_hour": 0.08,
        "acquired_at": "2026-10-29T17:00:00+00:00",
        "deadline": "2026-10-30T01:00:00+00:00",
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": [],
    }
    lease.save_record(repo, rec)
    return rec


def test_reap_keeps_record_when_sessions_unreadable(repo, tmp_path, monkeypatch):
    seed_record(repo)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.reap(boom, repo, now_dt=NOW)
    assert out["action"] == "kept-unknown"
    assert lease.load_record(repo) is not None


def test_reap_clears_only_on_confirmed_absent(repo, tmp_path, monkeypatch):
    seed_record(repo)
    out = lease.reap(lambda *a, **k: cp("[]"), repo, now_dt=NOW)
    assert out["action"] == "cleared-dead"
    assert lease.load_record(repo) is None


def test_reap_pulses_kernel_when_due_and_skips_when_fresh(repo, tmp_path, monkeypatch):
    seed_record(repo)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)  # no heartbeat file: keepalive relaunch path, harmless here
        return cp("")

    out = lease.reap(stub, repo, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["pulse"] == "ok"
    execs = [c for c in calls if c and c[0] == "exec" and any("lease-pulse" in str(x) for x in c)]
    assert len(execs) == 1
    assert lease.load_record(repo)["last_pulse_at"] == NOW.isoformat()
    calls.clear()
    out2 = lease.reap(stub, repo, now_dt=NOW)
    assert out2["pulse"] == "not-due"
    assert not [c for c in calls if c and c[0] == "exec" and any("lease-pulse" in str(x) for x in c)]


def test_release_keeps_record_when_stop_fails(repo, tmp_path, monkeypatch):
    seed_record(repo)

    def flaky(*a, **k):
        # stop times out; sessions keeps listing the session as alive.
        if a and a[0] == "stop":
            raise subprocess.TimeoutExpired("colab", 120)
        return cp('["standard"]')

    out = lease.release(flaky, repo)
    assert out["action"] == "release-failed"
    assert lease.load_record(repo) is not None


def test_heartbeat_age_stall_degrades_to_unknown_with_short_bound(repo, tmp_path, monkeypatch):
    """A stalled heartbeat download (the file channel can hang on the
    egress proxy's IPv4 endpoint, diagnosed 2026-11-01) must degrade
    to age-unknown quickly — never wedge a status/state read behind
    the old 120 s bound."""
    seen = {}

    def stall(*a, **k):
        seen["args"] = a
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired("colab", k.get("timeout"))

    assert lease.heartbeat_age(stall, repo, "standard") is None
    assert seen["args"][0] == "download"
    assert seen["timeout"] == lease.HEARTBEAT_READ_TIMEOUT_S
    assert lease.HEARTBEAT_READ_TIMEOUT_S <= 15


def test_heartbeat_age_reads_a_fresh_heartbeat(repo, tmp_path, monkeypatch):

    def fetch(*a, **k):
        dest = Path(a[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).timestamp() - 42
        dest.write_text(json.dumps({"ts": ts}) + "\n")
        return cp("")

    age = lease.heartbeat_age(fetch, repo, "standard")
    assert age is not None and 40 <= age <= 45


def test_status_survives_a_stalled_heartbeat_download(repo, tmp_path, monkeypatch):
    """The UI's read path: session live + heartbeat download stalled
    still returns the status, with the age reported as unknown."""
    seed_record(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        return cp("")

    out = lease.status(stub, repo)
    assert out["action"] == "active"
    assert out["keepalive_heartbeat_age_s"] is None


def test_ensure_keepalive_stalled_upload_is_a_result_not_an_exception(repo, tmp_path, monkeypatch):
    """The keepalive upload rides the stall-prone file channel: a stall
    must come back as launch-failed (lease stands, caller carries on),
    bounded well under the old 300 s — never as a raised timeout."""
    seen = {}

    def stall_upload(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired("colab", k.get("timeout"))

    out = lease.ensure_keepalive(stall_upload, repo, "standard")
    assert out["keepalive"] == "launch-failed"
    assert out["stage"] == "upload"
    assert seen["timeout"] == lease.KEEPALIVE_UPLOAD_TIMEOUT_S
    assert lease.KEEPALIVE_UPLOAD_TIMEOUT_S <= 30


def test_ensure_keepalive_stalled_launch_is_a_result_not_an_exception(repo, tmp_path, monkeypatch):

    def stall_launch(*a, **k):
        if a and a[0] == "exec":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        return cp("")

    out = lease.ensure_keepalive(stall_launch, repo, "standard")
    assert out["keepalive"] == "launch-failed"
    assert out["stage"] == "launch"


def test_reap_pulses_even_when_keepalive_relaunch_stalls(repo, tmp_path, monkeypatch):
    """Regression for 2026-11-01: reap died inside the keepalive
    relaunch's stalled upload before ever reaching the kernel pulse,
    leaving the lease with no activity channel at all. The pulse is
    an exec — it must not be starved by a file-channel failure."""
    seed_record(repo)

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

    out = lease.reap(stub, repo, now_dt=NOW)
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


def test_specs_real_catalog_labels(repo):
    labels = {s["id"]: s["label"] for s in lease.specs(repo)}
    assert labels["cpu"] == "CPU"
    assert labels["cpu-highram"] == "CPU High-RAM"


def test_live_sessions_empty_listing_has_no_phantom(repo):
    """The CLI answers an empty account with '[colab] No active
    sessions.' — its own log prefix is not a session name, and the
    listing must read as empty, never as a session named 'colab'
    (the phantom the access list stamped unattributed on
    2026-11-05)."""

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp("[colab] No active sessions found on server.\n[colab] No active sessions.")
        return cp("")

    assert lease.live_sessions(stub, repo) == []


def make_extend_stub(alive_sessions):
    def stub(*a, **k):
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(alive_sessions))
        return cp("")

    return stub


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


def seed_window_record(repo, **over):
    rec = {
        "session": "standard",
        "shape": "cpu",
        "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "rate_cu_per_hour": 0.08,
        # 12 h before NOW: at the renewal threshold (cap 12 h - 30 min).
        "acquired_at": "2026-10-29T06:00:00+00:00",
        "deadline": "2026-10-30T06:00:00+00:00",  # window still open
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": ["run-1"],
        "renew": True,
    }
    rec.update(over)
    # run-1's registration holds through its backing record
    # (the pruned projection): plant it live BEFORE the save,
    # or the save's own pruning drops the registration.
    runs = repo / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run-1.json").write_text(json.dumps({"run_id": "run-1", "session": "standard", "state": "running", "started_at": "2026-10-29T06:00:00+00:00"}))
    lease.save_record(repo, rec)
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


def test_renewal_threshold_driver2_swaps_session_keeps_window(repo, tmp_path, monkeypatch):
    seed_window_record(repo)
    monkeypatch.setenv("HOME", str(tmp_path))  # kit install: token-missing, non-fatal
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), repo, now_dt=NOW)
    assert out["action"] == "renewed"
    assert out["driver"] == 2
    assert out["old_session"] == "standard"
    rec = lease.load_record(repo, out["new_session"])
    assert rec["session"] == out["new_session"]
    assert rec["session"].startswith("standard-r")
    # The window is preserved exactly; only the session + its start move.
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    assert rec["jobs"] == ["run-1"]
    assert rec["acquired_at"] == NOW.isoformat()
    assert "standard" not in alive  # parent stopped after the swap
    assert rec["session"] in alive


def test_renewal_absent_session_driver2(repo, tmp_path, monkeypatch):
    seed_window_record(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    alive, calls = set(), []
    out = lease.reap(make_renewal_stub(alive, calls), repo, now_dt=NOW)
    # A dead session on a window lease renews instead of clearing.
    assert out["action"] == "renewed"
    assert out["driver"] == 2
    rec = lease.load_record(repo, out["new_session"])
    assert rec is not None
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    assert rec["session"] in alive


def test_renewal_driver1_adopts_handover_and_merges_binding(repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    sessions_file = home / ".config" / "colab-cli" / "sessions.json"
    sessions_file.parent.mkdir(parents=True)
    sessions_file.write_text(json.dumps({"standard": {"endpoint": "https://parent.invalid"}}))
    monkeypatch.setenv("HOME", str(home))
    seed_window_record(repo, kit="installed")
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
    out = lease.reap(make_renewal_stub(alive, calls, handover=handover), repo, now_dt=NOW)
    assert out["action"] == "renewed"
    assert out["driver"] == 1
    assert out["old_session"] == "standard"
    assert out["new_session"] == "standard-r999"
    rec = lease.load_record(repo, "standard-r999")
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


def test_renewal_skips_unflagged_records(repo, tmp_path, monkeypatch):
    rec = seed_window_record(repo)
    del rec["renew"]
    lease.save_record(repo, rec)
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), repo, now_dt=NOW)
    assert out["action"] == "kept"
    assert not [c for c in calls if c and c[0] == "new"]


def test_renewal_skips_standing_records(repo, tmp_path, monkeypatch):
    seed_window_record(repo, session="lobby", standing=True)
    lease.write_grant(repo, 24, now_dt=NOW)
    alive, calls = {"lobby"}, []
    out = lease.reap(make_renewal_stub(alive, calls), repo, now_dt=NOW)
    lobby = [r for r in out["records"] if r["session"] == "lobby"][0]
    assert lobby["action"] == "kept"  # standing duty semantics, not renewal
    assert not [c for c in calls if c and c[0] == "new"]


def test_failed_renewal_retries_next_cycle_deadline_untouched(repo, tmp_path, monkeypatch):
    seed_window_record(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    alive, calls = {"standard"}, []
    failing = {"on": True}
    stub = make_renewal_stub(alive, calls, fail_new=lambda: failing["on"])
    out = lease.reap(stub, repo, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["renewal"] == "failed"
    rec = lease.load_record(repo, "standard")
    assert rec["session"] == "standard"
    assert rec["deadline"] == "2026-10-30T06:00:00+00:00"
    failing["on"] = False
    out2 = lease.reap(stub, repo, now_dt=NOW)
    assert out2["action"] == "renewed"
    assert lease.load_record(repo)["session"].startswith("standard-r")


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


# --- The dynamic spec catalog (2026-11-01) ------------------------------------
# The picker options are parsed from `colab new --help`, never
# hand-listed; measured rates live in var/shape-rates.json.

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
    # The measured-rate table is the catalog's only rate source:
    # cpu-highram carries its measured 0.26; every other shape is
    # absent, hence unconfirmed.
    var = tmp_path / "var"
    var.mkdir(parents=True)
    (var / "shape-rates.json").write_text(json.dumps({"cpu-highram": 0.26}))
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))


def test_specs_dynamic_catalog_covers_the_cli_space(tmp_path, monkeypatch):
    _tmp_catalog(tmp_path, monkeypatch)
    specs = {s["id"]: s for s in lease.specs(tmp_path, _help_stub())}
    # The catalog is the CLI space itself, keyed by shape; the
    # measured-rate table prices cpu-highram and nothing else here.
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


def test_renewal_spools_a_lease_renewed_event_for_the_outbox(repo, tmp_path, monkeypatch):
    """Renewals happen in reap(), outside the companion server — the
    outbox's only writer — so the core spools the event and the
    server drains it (see drain_reaper_events in ui/server.py)."""
    seed_window_record(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), repo, now_dt=NOW)
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


def test_budget_check_maps_auth_required(repo, tmp_path, monkeypatch):
    """The wrapper guard's stop-and-surface signal is its own refusal
    reason, distinct from a stall or a parse failure."""

    def dead_auth(*a, **k):
        return subprocess.CompletedProcess([], 3, "", "colab auth required: stored token missing or invalid")

    out = lease.budget_check(dead_auth, repo, lease.load_config(repo), {"rate_cu_per_hour": 0.08}, hours=1)
    assert out == {"ok": False, "reason": "auth-required"}


def test_state_surfaces_auth_required_marker(repo, tmp_path, monkeypatch):
    var = tmp_path / "var"
    var.mkdir(exist_ok=True)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var))
    assert lease.auth_required(repo) is None
    (var / "auth-required.json").write_text(json.dumps({"noted_at": "2026-11-01T13:05:58+00:00", "reason": "test"}))

    def stub(*a, **k):
        if a and a[0] == "usage":
            return cp("Current balance: 100 compute units")
        return cp("[]")

    out = lease.state(stub, repo)
    assert out["auth_required"]["reason"] == "test"


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


def test_auto_timed_start_waits_then_fires(repo, tmp_path, monkeypatch):
    """A demand with a future not_before survives an auto pass
    untouched (not consumed, not failed, no lease), then acquires on
    the first pass after its time — a timed start as data, with the
    request's tag (written here in its legacy via spelling) read as
    the caller claim and stamped on the created record."""
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    alive = set()
    stub = _auto_stub(alive)
    lease.write_request(repo, reason="timed", shape="cpu", now_dt=NOW, via="timer", not_before=NOW + timedelta(hours=1))
    out = lease.auto_acquire(stub, repo, now_dt=NOW)
    assert out["action"] == "not-due"
    assert lease.load_request(repo) is not None
    assert lease.load_record(repo) is None
    assert alive == set()
    out = lease.auto_acquire(stub, repo, now_dt=NOW + timedelta(hours=2))
    assert out["action"] == "auto-acquired"
    assert out["caller"] == "timer"
    assert lease.load_request(repo) is None
    rec = lease.load_record(repo)
    assert rec is not None and rec["caller"] == "timer"


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


def test_acquire_records_host_and_spools_approval_needed(repo, tmp_path, monkeypatch):
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

    out = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert out["host_changed"] is True and out["host"] == HOST_A
    rec = lease.load_record(repo)
    assert rec["host"] == HOST_A
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0] == {"kind": "approval-needed", "shape": "cpu", "session": rec["session"], "host": HOST_A, "previous_host": None, "origin": "acquire"}


def test_acquire_without_binding_records_no_host_and_no_event(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no sessions file at all
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "created"
    assert "host_changed" not in out
    assert "host" not in lease.load_record(repo)
    assert _approval_events(tmp_path) == []


def test_renewal_swap_records_new_host_and_spools(repo, tmp_path, monkeypatch):
    child = f"standard-r{int(NOW.timestamp())}"
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_A}"}, child: {"url": f"https://{HOST_B}"}})
    monkeypatch.setenv("HOME", str(home))
    seed_window_record(repo, host=HOST_A)
    alive, calls = {"standard"}, []
    out = lease.reap(make_renewal_stub(alive, calls), repo, now_dt=NOW)
    assert out["action"] == "renewed" and out["driver"] == 2
    rec = lease.load_record(repo, child)
    assert rec["session"] == child and rec["host"] == HOST_B
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0]["origin"] == "renewal" and events[0]["host"] == HOST_B
    assert events[0]["previous_host"] == HOST_A and events[0]["session"] == child


def test_adopted_baselines_host_without_an_event(repo, tmp_path, monkeypatch):
    seed_record(repo)  # a pre-feature record: no host key
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_A}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    out = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "adopted"
    assert lease.load_record(repo, "standard")["host"] == HOST_A
    assert _approval_events(tmp_path) == []


def test_adopted_host_change_spools_once_and_unchanged_is_silent(repo, tmp_path, monkeypatch):
    rec = seed_record(repo)
    rec["host"] = HOST_A
    lease.save_record(repo, rec)
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_B}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    out = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out["action"] == "adopted"
    assert lease.load_record(repo, "standard")["host"] == HOST_B
    events = _approval_events(tmp_path)
    assert len(events) == 1
    assert events[0]["origin"] == "adopted" and events[0]["previous_host"] == HOST_A
    out2 = lease.acquire(stub, repo, "cpu", now_dt=NOW)
    assert out2["action"] == "adopted"
    assert len(_approval_events(tmp_path)) == 1  # unchanged host: no second event


def _foreign_stub(*a, **k):
    if a and a[0] == "usage":
        return cp("balance: 500 CU (stub)")
    if a and a[0] == "sessions":
        return cp("alpha2")
    if a and a[0] == "download":
        return cp("", rc=1)
    return cp("")


def test_acquire_never_adopts_a_foreign_session(repo, tmp_path, monkeypatch):
    """Successor to the retired adopt-foreign path: a live session
    the lease model did not create is never adopted by an acquire.
    Names are generated unique, so a create mints a fresh session
    beside the foreign one and the record names only its own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    out = lease.acquire(_foreign_stub, repo, "cpu-highram", now_dt=NOW)
    assert out["action"] == "created"
    assert out["record"]["session"] != "alpha2"
    assert lease.load_record(repo, "alpha2") is None


def test_reap_reconciles_a_missing_host_silently(repo, tmp_path, monkeypatch):
    """Regression (2026-11-04): a concurrent writer holding a
    pre-capture copy of the record restored a hostless record after
    the adopt-foreign capture saved — the acquire output was fine
    and nothing re-asserted the host, so the approval gate never
    saw the adopted host. Reap re-derives it from the binding; a
    fill baselines silently (the birth event already spooled)."""
    seed_record(repo)  # no host key
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_A}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    lease.reap(stub, repo, now_dt=NOW)
    assert lease.load_record(repo, "standard")["host"] == HOST_A
    assert _approval_events(tmp_path) == []


def test_reap_reconciles_a_changed_host_and_spools(repo, tmp_path, monkeypatch):
    rec = seed_record(repo)
    rec["host"] = HOST_A
    lease.save_record(repo, rec)
    home = _write_sessions_binding(tmp_path, {"standard": {"url": f"https://{HOST_B}"}})
    monkeypatch.setenv("HOME", str(home))

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)
        return cp("")

    lease.reap(stub, repo, now_dt=NOW)
    assert lease.load_record(repo, "standard")["host"] == HOST_B
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
    monkeypatch.setattr(lease.sessions, "local_sessions_path", lambda: binding)
    monkeypatch.setattr(lease.sessions, "session_alive", lambda c, s: alive)
    monkeypatch.setattr(lease.sessions, "time", SimpleNamespace(sleep=lambda s: None))
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return cp(rc=unassign_rc)

    monkeypatch.setattr(lease.sessions, "subprocess", SimpleNamespace(run=fake_run))

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
    lease_src = "".join(
        p.read_text() for p in sorted((REPO / "sync" / "lease").glob("*.py")))
    assert "client.unassign(" not in lease_src
    for rel in ("probes/_driver.py", "snippets/unassign_orphans.py"):
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
    monkeypatch.setattr(lease.gate, "session_gate_policy", lambda repo: {"enabled": enabled, "agent_classes": ["high_ram"], "bypass_actors": ["panel", "test"], "infra_actor": "standing"})
    monkeypatch.setattr(lease.foundation, "load_config", lambda repo: {"standing": {"shape": "cpu"}})
    monkeypatch.delenv("COLAB_LEASE_ACTOR", raising=False)


def _gate_journal(tmp_path):
    p = tmp_path / "var" / "session-gate-journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_authority_refuses_agent_creation_and_journals(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    out = lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha", seam="acquire")
    assert out is not None and out["action"] == "caller-start-refused"
    assert out["basis"] == "agent-creation-denied" and out["caller"] == "alpha"
    journal = _gate_journal(tmp_path)
    assert [e["kind"] for e in journal] == ["caller-start-refused"]
    assert journal[0]["caller"] == "alpha" and journal[0]["seam"] == "acquire"


def test_authority_shape_refusal_precedes_it(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    out = lease.enforce_session_admission(repo, "plain", shape=STANDARD, caller="alpha")
    assert out["action"] == "shape-refused"
    assert [e["kind"] for e in _gate_journal(tmp_path)] == ["shape-refused"]


def test_authority_non_creation_consult_and_duty_admit(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha", creation=False) is None
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha", duty=True) is None
    assert _gate_journal(tmp_path) == []


def test_authority_panel_two_halves_and_bypass_claim_admit(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "panel")
    # One half alone is not authority: the panel env claim without
    # its via corroboration is refused like any agent.
    out = lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="panel")
    assert out is not None and out["action"] == "caller-start-refused"
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, via="panel", caller="panel") is None
    # A whitelisted non-panel claim bypasses on the env half alone.
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "test")
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM) is None


def test_authority_infra_exemption_and_disabled_gate_admit(repo, tmp_path, monkeypatch):
    _authority_fixture(tmp_path, monkeypatch)
    assert lease.enforce_session_admission(repo, "cpu", shape=STANDARD, via="standing", standing=True) is None
    assert [e["kind"] for e in _gate_journal(tmp_path)] == ["shape-infra"]
    _authority_fixture(tmp_path, monkeypatch, enabled=False)
    assert lease.enforce_session_admission(repo, "main", shape=HIGH_RAM, caller="alpha") is None


def test_state_serves_extend_guard_policy_and_verdicts(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lease.json").write_text(json.dumps({"enabled": True, "extend_guard": {"shapes": {"a100-highram": "active-only"}}}))
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "extend-guard-state.json").write_text(json.dumps({"episodes": {}, "verdicts": {"busy-lease-1": {"action": "extended", "at": "2026-11-06T00:00:00+00:00"}}}))
    out = lease.state_extend_guard(tmp_path)
    assert out["extend_guard"]["policy"]["shapes"] == {"a100-highram": "active-only"}
    assert out["extend_guard"]["policy"]["default_mode"] == "active-or-first"
    assert out["extend_guard"]["verdicts"]["busy-lease-1"]["action"] == "extended"


# --- session_state read resilience (2026-11-07 classification defect) ---


def test_session_state_retries_a_transient_failure(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired("colab", 120)
        return cp("lilac-reef-93 some-host")

    diag = {}
    assert lease.session_state(flaky, "lilac-reef-93", diag) == "alive"
    assert calls["n"] == 2


def test_session_state_unknown_only_after_exhausted_retries(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))

    def dead(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    diag = {}
    assert lease.session_state(dead, "lilac-reef-93", diag) == "unknown"
    assert diag["attempts"] == 3 and "TimeoutExpired" in diag["read_error"]


def test_session_state_empty_output_retries_then_unknown(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    diag = {}
    assert lease.session_state(lambda *a, **k: cp(""), "lilac-reef-93", diag) == "unknown"
    assert diag == {"attempts": 3, "read_error": "empty-output"}


def test_session_state_auth_prompt_is_not_retried(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    calls = {"n": 0}

    def prompt(*a, **k):
        calls["n"] += 1
        return cp("Enter the authorization code:")

    diag = {}
    assert lease.session_state(prompt, "lilac-reef-93", diag) == "unknown"
    assert calls["n"] == 1 and diag["read_error"] == "auth-prompt"


def test_extend_unknown_carries_the_read_error(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    seed_record(repo)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.extend(boom, repo, "standard", 2, now_dt=NOW)
    assert out["action"] == "extend-unknown"
    assert out["attempts"] == 3 and "TimeoutExpired" in out["read_error"]


# --- Orphan evidence and adoption ---------------------------------------------
#
# An orphan is a live session with no lease record. These tests
# pin the derivation priority (renewal-config first, the panel
# outbox's lease-extended chain over it, nothing else carrying a
# deadline), the creator-over-claims caller rule, and the adopt
# refusal classes.


def orphan_stub(names, balance=500):
    def stub(*args, **kwargs):
        if args and args[0] == "usage":
            return cp(f"balance: {balance} CU (stub)")
        if args and args[0] == "sessions":
            return cp(json.dumps(names))
        return cp("")

    return stub


def plant_kit_config(repo, session, *, deadline, acquired_at, gpu=None, tpu=None, high_mem=True):
    cfg = {
        "shape": lease.shape_id_for(gpu, tpu, high_mem),
        "session": session,
        "deadline": deadline.isoformat(),
        "acquired_at": acquired_at.isoformat(),
        "cap_seconds": 3600,
        "margin_seconds": 600,
        "high_mem": high_mem,
        "gpu": gpu,
        "tpu": tpu,
    }
    (repo / "var" / f"renewal-config-{session}.json").write_text(json.dumps(cfg, indent=2))


def append_jsonl(path, obj):
    with path.open("a") as fh:
        fh.write(json.dumps(obj) + "\n")


def test_orphan_derivation_uses_renewal_config(repo):
    plant_kit_config(repo, "lost-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=2))
    entry = lease.derive_orphan(repo, {"session": "lost-1", "endpoint": None, "shape_class": "high_ram"})
    assert entry["derived_deadline"] == (NOW + timedelta(hours=5)).isoformat()
    assert entry["deadline_source"] == "renewal-config"
    assert entry["acquired_at"] == (NOW - timedelta(hours=2)).isoformat()
    assert entry["shape"] == "cpu-highram" and entry["shape_evidenced"] is True
    assert entry["kit_installed"] is True
    assert entry["creator_caller"] is None and entry["conflicts"] == []
    assert entry["deadline_stale_possible"] is False


def test_orphan_derivation_applies_outbox_extension(repo):
    plant_kit_config(repo, "lost-2", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=2))
    later = NOW + timedelta(hours=9)
    append_jsonl(repo / "var" / "ui-outbox.jsonl", {"seq": 1, "ts": NOW.isoformat(), "kind": "lease-extended", "detail": {"session": "lost-2", "hours": 4, "old_deadline": (NOW + timedelta(hours=5)).isoformat(), "new_deadline": later.isoformat()}})
    entry = lease.derive_orphan(repo, {"session": "lost-2", "endpoint": None, "shape_class": "high_ram"})
    assert entry["derived_deadline"] == later.isoformat()
    assert entry["deadline_source"] == "ui-outbox"
    deadline_conflicts = [c for c in entry["conflicts"] if c["field"] == "deadline"]
    assert len(deadline_conflicts) == 1
    assert [v["value"] for v in deadline_conflicts[0]["values"]] == [(NOW + timedelta(hours=5)).isoformat(), later.isoformat()]


def test_orphan_derivation_flags_stale_extension(repo):
    plant_kit_config(repo, "lost-3", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=2))
    append_jsonl(repo / "var" / "ui-outbox.jsonl", {"seq": 1, "ts": NOW.isoformat(), "kind": "lease-extended", "detail": {"session": "lost-3", "hours": 4}})
    entry = lease.derive_orphan(repo, {"session": "lost-3", "endpoint": None, "shape_class": "high_ram"})
    assert entry["derived_deadline"] == (NOW + timedelta(hours=5)).isoformat()
    assert entry["deadline_stale_possible"] is True


def test_orphan_derivation_creator_beats_first_claim(repo):
    append_jsonl(lease.reaper_events_path(repo), {"kind": "approval-needed", "shape": "cpu-highram", "session": "lost-4", "host": "m-hm-x", "origin": "renewal", "caller": "alpha"})
    append_jsonl(lease.caller_claims_path(repo), {"ts": NOW.isoformat(), "session": "lost-4", "caller": "beta", "seam": "acquire"})
    entry = lease.derive_orphan(repo, {"session": "lost-4", "endpoint": None, "shape_class": None})
    assert entry["creator_caller"] == "alpha"
    assert entry["first_claim"] == "beta"
    caller_conflicts = [c for c in entry["conflicts"] if c["field"] == "caller"]
    assert len(caller_conflicts) == 1
    assert [v["value"] for v in caller_conflicts[0]["values"]] == ["alpha", "beta"]


def test_orphan_derivation_no_sources(repo):
    entry = lease.derive_orphan(repo, {"session": "bare-1", "endpoint": None, "shape_class": "high_ram"})
    assert entry["derived_deadline"] is None and entry["deadline_source"] is None
    assert entry["shape"] == "cpu-highram" and entry["shape_evidenced"] is False
    assert entry["shape_candidates"] == ["cpu-highram"]
    assert entry["conflicts"] == [] and entry["jobs_in_flight"] == 0
    assert entry["jobs_in_flight_entries"] == []


def test_adopt_orphan_restores_evidenced_window(repo):
    # A 200-hour evidenced remainder at the high-memory rate
    # would bust the daily cap if it were priced: adoption of
    # the evidenced window is a restore, exempt by ruling.
    plant_kit_config(repo, "reborn", deadline=NOW + timedelta(hours=200), acquired_at=NOW - timedelta(hours=26))
    append_jsonl(lease.reaper_events_path(repo), {"kind": "approval-needed", "shape": "cpu-highram", "session": "reborn", "host": "m-hm-x", "origin": "renewal", "caller": "alpha"})
    out = lease.adopt_orphan(orphan_stub(["reborn"]), repo, "reborn", caller="beta", now_dt=NOW)
    assert out["action"] == "adopted"
    rec = out["record"]
    assert rec["caller"] == "alpha"  # the evidenced creator, not the operator
    assert rec["acquired_at"] == (NOW - timedelta(hours=26)).isoformat()
    assert rec["deadline"] == (NOW + timedelta(hours=200)).isoformat()
    assert rec["deadline_source"] == "renewal-config" and out["deadline_source"] == "renewal-config"
    assert rec["adopted"] is True and rec["adopted_at"] == NOW.isoformat()
    assert rec["renew"] is True and rec["kit"] == "installed"
    assert rec["rate_cu_per_hour"] == 0.26 and rec["jobs"] == []
    assert "budget_at_adopt" not in rec and "budget" not in out
    claims = [json.loads(line) for line in lease.caller_claims_path(repo).read_text().splitlines()]
    assert any(c["caller"] == "beta" and c["seam"] == "adopt" for c in claims)


def test_adopt_orphan_seeds_inflight_jobs(repo):
    plant_kit_config(repo, "busy-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    runs = repo / "results" / "runs"
    runs.mkdir(parents=True)
    (runs / "run-a.json").write_text(json.dumps({"run_id": "run-a", "session": "busy-1", "state": "running", "caller": "alpha"}))
    (runs / "run-b.json").write_text(json.dumps({"run_id": "run-b", "session": "busy-1", "state": "complete", "caller": "alpha"}))
    out = lease.adopt_orphan(orphan_stub(["busy-1"]), repo, "busy-1", now_dt=NOW)
    assert out["action"] == "adopted"
    assert out["record"]["jobs"] == ["run-a"]


def test_adopt_orphan_past_derived_deadline_refuses(repo):
    plant_kit_config(repo, "stale-1", deadline=NOW - timedelta(hours=1), acquired_at=NOW - timedelta(hours=9))
    out = lease.adopt_orphan(orphan_stub(["stale-1"]), repo, "stale-1", now_dt=NOW)
    assert out["action"] == "adopt-deadline-past"
    assert out["deadline_source"] == "renewal-config"
    assert lease.load_record(repo, "stale-1") is None


def test_adopt_orphan_absent_refuses(repo):
    out = lease.adopt_orphan(orphan_stub([]), repo, "ghost-1", deadline=NOW + timedelta(hours=3), now_dt=NOW)
    assert out["action"] == "adopt-absent"


def test_adopt_orphan_unknown_when_list_unreadable(repo):
    def boom(*args):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.adopt_orphan(boom, repo, "ghost-2", deadline=NOW + timedelta(hours=3), now_dt=NOW)
    assert out["action"] == "adopt-unknown"


def test_adopt_orphan_unknown_at_recheck_carries_diag(repo):
    plant_kit_config(repo, "gone-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        if args and args[0] == "sessions":
            calls["n"] += 1
            if calls["n"] == 1:
                return cp(json.dumps(["gone-1"]))
            raise subprocess.TimeoutExpired("colab", 120)
        if args and args[0] == "usage":
            return cp("balance: 500 CU (stub)")
        return cp("")

    out = lease.adopt_orphan(flaky, repo, "gone-1", now_dt=NOW)
    assert out["action"] == "adopt-unknown"
    assert out["attempts"] == 3 and "TimeoutExpired" in out["read_error"]
    assert lease.load_record(repo, "gone-1") is None


def test_adopt_orphan_record_appearing_before_save_refuses(repo):
    plant_kit_config(repo, "taken-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    calls = {"n": 0}

    def interloper(*args, **kwargs):
        if args and args[0] == "sessions":
            calls["n"] += 1
            if calls["n"] == 2:
                lease.session_record_path(repo, "taken-1").write_text(json.dumps({"session": "taken-1", "marker": "interloper"}))
            return cp(json.dumps(["taken-1"]))
        if args and args[0] == "usage":
            return cp("balance: 500 CU (stub)")
        return cp("")

    out = lease.adopt_orphan(interloper, repo, "taken-1", now_dt=NOW)
    assert out["action"] == "adopt-has-record"
    saved = lease.load_record(repo, "taken-1")
    assert saved["marker"] == "interloper" and "adopted" not in saved


def test_adopt_orphan_manual_beyond_evidenced_prices_delta_only(repo):
    plant_kit_config(repo, "delta-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    out = lease.adopt_orphan(orphan_stub(["delta-1"]), repo, "delta-1", deadline=NOW + timedelta(hours=9), now_dt=NOW)
    assert out["action"] == "adopted"
    assert out["deadline_source"] == "manual"
    # Only the 4 extra hours price, at the evidenced shape's rate.
    assert out["record"]["budget_at_adopt"]["projected"] == pytest.approx(4 * 0.26)
    assert out["budget"]["projected"] == pytest.approx(4 * 0.26)


def test_adopt_orphan_manual_beyond_budget_refusal_writes_nothing(repo):
    plant_kit_config(repo, "delta-2", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    out = lease.adopt_orphan(orphan_stub(["delta-2"]), repo, "delta-2", deadline=NOW + timedelta(hours=105), now_dt=NOW)
    assert out["action"] == "refused-budget"
    assert lease.load_record(repo, "delta-2") is None


def test_adopt_orphan_manual_at_evidenced_adopts_exempt(repo):
    plant_kit_config(repo, "delta-3", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    out = lease.adopt_orphan(orphan_stub(["delta-3"]), repo, "delta-3", deadline=NOW + timedelta(hours=5), now_dt=NOW)
    assert out["action"] == "adopted"
    assert "budget_at_adopt" not in out["record"]


def test_adopt_deadline_shortens_refuses_deadline_form(repo):
    plant_kit_config(repo, "keep-1", deadline=NOW + timedelta(hours=10), acquired_at=NOW - timedelta(hours=1))
    out = lease.adopt_orphan(orphan_stub(["keep-1"]), repo, "keep-1", deadline=NOW + timedelta(hours=9), now_dt=NOW)
    assert out["action"] == "adopt-deadline-shortens"
    assert out["evidenced_deadline"] == (NOW + timedelta(hours=10)).isoformat()
    assert lease.load_record(repo, "keep-1") is None


def test_adopt_deadline_shortens_refuses_hours_form(repo):
    plant_kit_config(repo, "keep-2", deadline=NOW + timedelta(hours=10), acquired_at=NOW - timedelta(hours=1))
    out = lease.adopt_orphan(orphan_stub(["keep-2"]), repo, "keep-2", hours=9, now_dt=NOW)
    assert out["action"] == "adopt-deadline-shortens"
    assert lease.load_record(repo, "keep-2") is None


def seed_accelerator_catalog(repo):
    (repo / "var" / "cli-catalog.json").write_text(json.dumps({"gpu": ["T4"], "tpu": ["v5e1"], "no_high_mem": [], "fetched_at": "2027-01-01T00:00:00+00:00"}))


def plant_highram_binding(tmp_path, monkeypatch, session):
    binding = tmp_path / "sessions-binding.json"
    binding.write_text(json.dumps({session: {"endpoint": "https://m-hm-fake.prod.r.colab.dev"}}))
    monkeypatch.setattr(lease.sessions, "local_sessions_path", lambda: binding)


def test_adopt_orphan_shape_ambiguous_refuses_with_candidates(repo, tmp_path, monkeypatch):
    seed_accelerator_catalog(repo)
    plant_highram_binding(tmp_path, monkeypatch, "wide-1")
    out = lease.adopt_orphan(orphan_stub(["wide-1"]), repo, "wide-1", deadline=NOW + timedelta(hours=3), now_dt=NOW)
    assert out["action"] == "adopt-shape-ambiguous"
    assert {"cpu-highram", "t4-highram"} <= set(out["shape_candidates"])
    assert lease.load_record(repo, "wide-1") is None


def test_adopt_orphan_shape_override_adopts(repo, tmp_path, monkeypatch):
    seed_accelerator_catalog(repo)
    plant_highram_binding(tmp_path, monkeypatch, "wide-2")
    out = lease.adopt_orphan(orphan_stub(["wide-2"]), repo, "wide-2", deadline=NOW + timedelta(hours=3), shape="t4-highram", now_dt=NOW)
    assert out["action"] == "adopted"
    assert out["record"]["shape"] == "t4-highram"
    # No measured or catalog rate exists for the shape: the
    # restore proceeds with the rate unconfirmed, projecting
    # nothing (the shape_rate contract).
    assert out["record"]["rate_cu_per_hour"] is None


def test_adopt_orphan_shape_unknown_refuses(repo, tmp_path, monkeypatch):
    seed_accelerator_catalog(repo)
    plant_highram_binding(tmp_path, monkeypatch, "wide-3")
    out = lease.adopt_orphan(orphan_stub(["wide-3"]), repo, "wide-3", deadline=NOW + timedelta(hours=3), shape="nope", now_dt=NOW)
    assert out["action"] == "adopt-shape-unknown"
    assert lease.load_record(repo, "wide-3") is None


def test_adopt_orphan_shape_conflicting_with_evidence_refuses(repo):
    plant_kit_config(repo, "exact-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    out = lease.adopt_orphan(orphan_stub(["exact-1"]), repo, "exact-1", shape="cpu", now_dt=NOW)
    assert out["action"] == "adopt-shape-conflict"
    assert out["evidenced_shape"] == "cpu-highram" and out["shape"] == "cpu"
    assert lease.load_record(repo, "exact-1") is None


def test_adopt_orphan_bridge_and_handover_refused(repo):
    out = lease.adopt_orphan(orphan_stub(["bridge-x-1"]), repo, "bridge-x-1", deadline=NOW + timedelta(hours=3), now_dt=NOW)
    assert out["action"] == "adopt-bridge-managed"
    (repo / "var" / "lease-handover.json").write_text(json.dumps({"child_session": "child-1", "minted_at": NOW.isoformat()}))
    out = lease.adopt_orphan(orphan_stub(["child-1"]), repo, "child-1", deadline=NOW + timedelta(hours=3), now_dt=NOW)
    assert out["action"] == "adopt-handover-pending"
    assert lease.load_record(repo, "child-1") is None


def test_adopt_orphan_lease_disabled(repo):
    (repo / "config" / "lease.json").write_text(json.dumps({"enabled": False}))
    out = lease.adopt_orphan(orphan_stub(["off-1"]), repo, "off-1", deadline=NOW + timedelta(hours=3), now_dt=NOW)
    assert out["action"] == "lease-disabled"


def test_state_orphans_portion_lists_unrecorded_live_sessions(repo):
    seed_record(repo)  # session "standard" holds a record
    plant_kit_config(repo, "stray-1", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    portion = lease.state_orphans(orphan_stub(["standard", "stray-1", "bridge-x-9", "bare-2"]), repo)
    # Recorded and bridge-prefixed sessions stay out; the rest
    # are served with their derived evidence.
    assert [o["session"] for o in portion["orphans"]] == ["stray-1", "bare-2"]
    stray = portion["orphans"][0]
    assert stray["derived_deadline"] == (NOW + timedelta(hours=5)).isoformat()
    assert stray["deadline_source"] == "renewal-config"
    assert portion["orphans"][1]["derived_deadline"] is None


def test_state_orphans_portion_unreadable(repo):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("colab", 120)

    assert lease.state_orphans(boom, repo) == {"orphans": [], "unreadable": True}


# --- Shorten --------------------------------------------------------------------
#
# The operator's explicit deadline edit, extend's mirror. The
# stamp discipline: while shortened_at stands, the extend
# guard skips the lease; an operator extend clears the stamp
# (the latest action wins) and the guard treats the lease
# normally again.


def test_shorten_moves_deadline_earlier_and_stamps(repo):
    seed_record(repo)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=2, caller="alpha", now_dt=NOW)
    assert out["action"] == "shortened"
    assert out["old_deadline"] == "2026-10-30T01:00:00+00:00"
    assert out["new_deadline"] == "2026-10-29T23:00:00+00:00"
    assert out["hours"] == 2 and out["caller"] == "alpha"
    rec = lease.load_record(repo, "standard")
    assert rec["deadline"] == "2026-10-29T23:00:00+00:00"
    assert rec["shortened_at"] == NOW.isoformat()
    assert rec["shortened_by"] == "alpha"
    assert rec["shortened_from"] == "2026-10-30T01:00:00+00:00"


def test_shorten_absolute_deadline_form(repo):
    seed_record(repo)
    target = NOW + timedelta(hours=2)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=target, now_dt=NOW)
    assert out["action"] == "shortened"
    assert out["new_deadline"] == target.isoformat()
    assert "hours" not in out


def test_shorten_not_earlier_refuses(repo):
    seed_record(repo)
    later = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=NOW + timedelta(hours=9), now_dt=NOW)
    assert later["action"] == "shorten-not-earlier"
    same = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc), now_dt=NOW)
    assert same["action"] == "shorten-not-earlier"
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-30T01:00:00+00:00"


def test_shorten_deadline_past_refuses(repo):
    seed_record(repo)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=NOW - timedelta(hours=1), now_dt=NOW)
    assert out["action"] == "shorten-deadline-past"
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=100, now_dt=NOW)
    assert out["action"] == "shorten-deadline-past"
    rec = lease.load_record(repo, "standard")
    assert rec["deadline"] == "2026-10-30T01:00:00+00:00" and "shortened_at" not in rec


def test_shorten_no_record_disabled_and_no_target(repo):
    out = lease.shorten(orphan_stub([]), repo, "ghost-9", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-no-lease"
    seed_record(repo)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", now_dt=NOW)
    assert out["action"] == "shorten-no-target"
    (repo / "config" / "lease.json").write_text(json.dumps({"enabled": False}))
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "lease-disabled"


def test_shorten_unknown_and_absent(repo):
    seed_record(repo)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.shorten(boom, repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-unknown"
    assert out["attempts"] == 3 and "TimeoutExpired" in out["read_error"]
    out = lease.shorten(orphan_stub([]), repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-no-lease"  # absent, and the seed is no window


def test_shorten_standing_managed_refuses(repo):
    rec = seed_record(repo)
    rec["standing"] = True
    lease.save_record(repo, rec)
    lease.write_grant(repo, 24, now_dt=NOW)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-standing-managed"


def test_shorten_kit_reinstall_reported(repo, tmp_path, monkeypatch):
    rec = seed_record(repo)
    rec["kit"] = "installed"
    rec["renew"] = True
    lease.save_record(repo, rec)
    monkeypatch.setenv("HOME", str(tmp_path))  # no token here: the kit reinstall reports token-missing, non-fatally
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=2, now_dt=NOW)
    assert out["action"] == "shortened"
    assert out["kit"] == {"kit": "install-failed", "stage": "token-missing"}
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-29T23:00:00+00:00"


def test_extend_clears_shorten_stamp(repo):
    rec = seed_record(repo)
    rec["shortened_at"] = NOW.isoformat()
    rec["shortened_by"] = "alpha"
    rec["shortened_from"] = "2026-10-30T03:00:00+00:00"
    lease.save_record(repo, rec)
    out = lease.extend(orphan_stub(["standard"]), repo, "standard", 2, caller="alpha", now_dt=NOW)
    assert out["action"] == "extended"
    saved = lease.load_record(repo, "standard")
    assert "shortened_at" not in saved and "shortened_by" not in saved and "shortened_from" not in saved


def test_shorten_extend_shorten_restamps(repo):
    seed_record(repo)
    stub = orphan_stub(["standard"])
    first = lease.shorten(stub, repo, "standard", hours=2, caller="alpha", now_dt=NOW)
    assert first["action"] == "shortened"
    ext = lease.extend(stub, repo, "standard", 1, caller="alpha", now_dt=NOW)
    assert ext["action"] == "extended"
    mid = lease.load_record(repo, "standard")
    assert mid["deadline"] == "2026-10-30T00:00:00+00:00" and "shortened_at" not in mid
    second = lease.shorten(stub, repo, "standard", hours=1, caller="beta", now_dt=NOW)
    assert second["action"] == "shortened"
    saved = lease.load_record(repo, "standard")
    assert saved["deadline"] == "2026-10-29T23:00:00+00:00"
    assert saved["shortened_from"] == "2026-10-30T00:00:00+00:00"
    assert saved["shortened_by"] == "beta" and saved["shortened_at"] == NOW.isoformat()


# --- Record damage, session evidence, detached census, state dials -------------
#
# The sync-coupling plan's Step 5 (E1, E2) plus the owner
# review's E14 and the census half of E8: a torn record is a
# served finding, quarantined by the first writer that needs
# its path with its bytes preserved; the session-evidence
# projection carries an evidenced deadline through total
# record + journal + kit-config loss; a live detached record
# counts as a job in flight; and state's two account dials
# (usage, the live list) cache on a short TTL.

_records_mod = sys.modules["lease.records"]
_state_mod = sys.modules["lease.state"]


def test_state_torn_records_portion_serves_findings(repo):
    seed_record(repo)  # a healthy record coexists and is not a finding
    torn_payload = b'{"session": "tor'
    (repo / "var" / "lease-torn-1.json").write_bytes(b"")
    (repo / "var" / "lease-torn-2.json").write_bytes(torn_payload)
    # The handover marker shares the lease- prefix and is
    # never a record, torn or otherwise.
    (repo / "var" / "lease-handover.json").write_text(json.dumps({"child_session": "kid-1", "minted_at": NOW.isoformat()}))
    portion = _state_mod.state_torn_records(repo)
    assert portion == {"torn_records": [
        {"session": "torn-1", "file": "lease-torn-1.json", "bytes": 0},
        {"session": "torn-2", "file": "lease-torn-2.json", "bytes": len(torn_payload)},
    ]}


def test_save_record_quarantines_torn_file_preserving_bytes(repo):
    torn = b'{"session": "standard", "deadli'
    lease.session_record_path(repo, "standard").write_bytes(torn)
    seed_record(repo)  # save_record is the writer that needs the path
    quarantined = sorted((repo / "var").glob("lease-standard.json.torn-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == torn
    assert lease.load_record(repo, "standard")["session"] == "standard"
    assert _records_mod.torn_records(repo) == []


def test_adopt_orphan_quarantines_torn_record_and_preserves_bytes(repo):
    plant_kit_config(repo, "reborn-torn", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    torn = b'{"session": "reborn-torn", "shape": "cpu-hi'
    lease.session_record_path(repo, "reborn-torn").write_bytes(torn)
    out = lease.adopt_orphan(orphan_stub(["reborn-torn"]), repo, "reborn-torn", now_dt=NOW)
    assert out["action"] == "adopted"
    quarantined = sorted((repo / "var").glob("lease-reborn-torn.json.torn-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == torn
    assert lease.load_record(repo, "reborn-torn")["adopted"] is True


def test_orphan_derivation_falls_back_to_session_evidence(repo, tmp_path, monkeypatch):
    """E2's proof: acquire + extend write the evidence file as
    a side effect of the lifecycle itself (no renewal kit —
    the window never crosses the session cap at acquire, and
    the extend's kit install fails on the missing token
    without planting a config). Delete the record, every
    journal, and any renewal-config: derivation still returns
    the evidenced (extended) deadline, from the evidence file
    alone."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no token: kit installs report token-missing, non-fatally
    out = lease.acquire(_acquire_stub, repo, "cpu", now_dt=NOW, caller="alpha")
    assert out["action"] == "created"
    rec = out["record"]
    session = rec["session"]
    ext = lease.extend(make_extend_stub([session]), repo, session, 2, now_dt=NOW)
    assert ext["action"] == "extended"
    var = repo / "var"
    evidence = json.loads((var / f"session-evidence-{session}.json").read_text())
    assert evidence["deadline"] == ext["new_deadline"]
    assert evidence["acquired_at"] == rec["acquired_at"] and evidence["caller"] == "alpha"
    assert evidence["shape"] == "cpu" and evidence["spec"] == {"gpu": None, "tpu": None, "high_mem": False}
    lease.session_record_path(repo, session).unlink()
    (var / f"renewal-config-{session}.json").unlink(missing_ok=True)
    for name in ("ui-outbox.jsonl", "reaper-events.jsonl"):
        (var / name).unlink(missing_ok=True)
    lease.caller_claims_path(repo).unlink(missing_ok=True)
    entry = lease.derive_orphan(repo, {"session": session, "endpoint": None, "shape_class": "standard"})
    assert entry["derived_deadline"] == ext["new_deadline"]
    assert entry["deadline_source"] == "session-evidence"
    assert entry["acquired_at"] == rec["acquired_at"]
    assert entry["creator_caller"] == "alpha"
    assert entry["shape"] == "cpu" and entry["shape_evidenced"] is True
    assert entry["kit_installed"] is False


def test_mirror_manifest_lists_the_session_evidence_class():
    spec = importlib.util.spec_from_loader("mirror_transient", SourceFileLoader("mirror_transient", str(REPO / "ops" / "mirror_transient.py")))
    mirror = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mirror)
    assert mirror.var_file_class("session-evidence-lost-1.json") == "session-evidence"
    assert "session-evidence" in {name for _prefix, _suffix, name in mirror.VAR_FILE_CLASSES}
    assert mirror.var_file_class("ui-outbox.jsonl") is None


def test_state_dials_cache_within_ttl_and_redial_past_it(repo):
    """E14's proof: inside the TTL, two budget reads make one
    usage call and two census reads make one list call against
    a counting stub; past the TTL both re-dial."""
    calls = {"usage": 0, "sessions": 0}

    def stub(*a, **k):
        if a and a[0] == "usage":
            calls["usage"] += 1
            return cp("balance: 500 CU (stub)")
        if a and a[0] == "sessions":
            calls["sessions"] += 1
            return cp(json.dumps(["stray-1"]))
        return cp("")

    ttl = _state_mod.STATE_DIAL_CACHE_TTL_S
    first = lease.budget_snapshot(stub, repo, now_dt=NOW)
    assert first["balance"] == 500
    again = lease.budget_snapshot(stub, repo, now_dt=NOW + timedelta(seconds=10))
    assert again["balance"] == 500 and calls["usage"] == 1
    past = lease.budget_snapshot(stub, repo, now_dt=NOW + timedelta(seconds=ttl + 60))
    assert past["balance"] == 500 and calls["usage"] == 2

    census = lease.state_orphans(stub, repo, now_dt=NOW)
    assert [o["session"] for o in census["orphans"]] == ["stray-1"]
    census2 = lease.state_orphans(stub, repo, now_dt=NOW + timedelta(seconds=10))
    assert [o["session"] for o in census2["orphans"]] == ["stray-1"] and calls["sessions"] == 1
    lease.state_orphans(stub, repo, now_dt=NOW + timedelta(seconds=ttl + 60))
    assert calls["sessions"] == 2


def test_access_list_rides_the_live_list_dial_cache(repo):
    """E14 residual: the records portion's access list dialed
    the live list on every read even when the orphans portion
    had just cached it. access_list now consults the same
    state-cache-live.json — one dial inside the TTL, shared
    with the census, and a re-dial past it."""
    calls = {"sessions": 0}

    def stub(*a, **k):
        if a and a[0] == "sessions":
            calls["sessions"] += 1
            return cp(json.dumps(["stray-1"]))
        return cp("")

    first = lease.access_list(stub, repo, now_dt=NOW)
    assert [e["session"] for e in first] == ["stray-1"] and calls["sessions"] == 1
    again = lease.access_list(stub, repo, now_dt=NOW + timedelta(seconds=10))
    assert [e["session"] for e in again] == ["stray-1"] and calls["sessions"] == 1
    census = lease.state_orphans(stub, repo, now_dt=NOW + timedelta(seconds=10))
    assert [o["session"] for o in census["orphans"]] == ["stray-1"] and calls["sessions"] == 1
    lease.access_list(stub, repo, now_dt=NOW + timedelta(seconds=_state_mod.STATE_DIAL_CACHE_TTL_S + 60))
    assert calls["sessions"] == 2


def test_orphan_census_counts_live_detached_work(repo):
    """E8's census half: a live detached record for the
    session counts in jobs_in_flight under its detached:<id>
    entry and seeds the adopted record's jobs list; a
    finished one and another session's do not."""
    plant_kit_config(repo, "busy-det", deadline=NOW + timedelta(hours=5), acquired_at=NOW - timedelta(hours=1))
    det_dir = repo / "results" / "detached"
    det_dir.mkdir(parents=True)
    (det_dir / "det-1.json").write_text(json.dumps({"id": "det-1", "session": "busy-det", "caller": "alpha", "state": "running", "exit_code": None, "started_at": NOW.isoformat(), "finished_at": None}))
    (det_dir / "det-2.json").write_text(json.dumps({"id": "det-2", "session": "busy-det", "caller": "alpha", "state": "complete", "exit_code": 0, "started_at": NOW.isoformat(), "finished_at": NOW.isoformat()}))
    (det_dir / "det-3.json").write_text(json.dumps({"id": "det-3", "session": "other-1", "caller": "beta", "state": "running", "exit_code": None, "started_at": NOW.isoformat(), "finished_at": None}))
    entry = lease.derive_orphan(repo, {"session": "busy-det", "endpoint": None, "shape_class": "high_ram"})
    assert entry["jobs_in_flight"] == 1
    assert entry["jobs_in_flight_entries"] == [{"id": "detached:det-1", "kind": "detached", "state": "running", "started_at": NOW.isoformat()}]
    out = lease.adopt_orphan(orphan_stub(["busy-det"]), repo, "busy-det", now_dt=NOW)
    assert out["action"] == "adopted"
    assert out["record"]["jobs"] == ["detached:det-1"]


def test_orphan_census_run_entries_kinded(repo):
    """The census serves run work the same way: a run record
    in a non-terminal state counts with its kind, state, and
    start; a terminal one does not count at all."""
    runs_dir = repo / "results" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run-live.json").write_text(json.dumps({"run_id": "run-live", "session": "busy-run", "caller": "alpha", "state": "polling", "started_at": NOW.isoformat()}))
    (runs_dir / "run-done.json").write_text(json.dumps({"run_id": "run-done", "session": "busy-run", "caller": "alpha", "state": "complete", "started_at": NOW.isoformat()}))
    entry = lease.derive_orphan(repo, {"session": "busy-run", "endpoint": None, "shape_class": "high_ram"})
    assert entry["jobs_in_flight"] == 1
    assert entry["jobs_in_flight_entries"] == [{"id": "run-live", "kind": "run", "state": "polling", "started_at": NOW.isoformat()}]


def test_pruned_jobs_projection_and_save_record(repo):
    """One projection: a registration counts only while its
    backing record exists and is non-terminal. save_record
    persists the pruned list, so the store self-heals; the
    state document serves the projection, not the raw list."""
    runs_dir = repo / "results" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run-live.json").write_text(json.dumps({"run_id": "run-live", "session": "s-prune", "state": "running", "started_at": NOW.isoformat()}))
    (runs_dir / "run-done.json").write_text(json.dumps({"run_id": "run-done", "session": "s-prune", "state": "failed", "started_at": NOW.isoformat()}))
    (runs_dir / "run-torn.json").write_text("{torn")
    det_dir = repo / "results" / "detached"
    det_dir.mkdir(parents=True)
    (det_dir / "det-live.json").write_text(json.dumps({"id": "det-live", "session": "s-prune", "state": "running", "started_at": NOW.isoformat(), "finished_at": None}))
    rec = {"session": "s-prune", "caller": "alpha", "jobs": ["run-live", "run-done", "run-missing", "run-torn", "detached:det-live", "detached:det-gone", "bare-name"]}
    live = lease.live_jobs(repo, rec)
    assert [e["id"] for e in live] == ["run-live", "run-torn", "detached:det-live"]
    assert live[0]["kind"] == "run" and live[2]["kind"] == "detached"
    assert live[1]["state"] == "unknown"  # torn backing: unknown, never dead
    lease.save_record(repo, rec)
    assert rec["jobs"] == ["run-live", "run-torn", "detached:det-live"]
    on_disk = json.loads((repo / "var" / "lease-s-prune.json").read_text())
    assert on_disk["jobs"] == ["run-live", "run-torn", "detached:det-live"]
    served = lease.status(lambda *a, **k: cp(""), repo, session="s-prune")
    assert served["record"]["jobs"] == ["run-live", "run-torn", "detached:det-live"]
