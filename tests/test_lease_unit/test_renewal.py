"""Lease unit tests: window renewal at the session cap, with
stub colab callables (no subprocess)."""

import json
from pathlib import Path

from unit_harness import NOW, gate_permissive, lease, make_renewal_stub, repo, seed_window_record  # noqa: F401


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
