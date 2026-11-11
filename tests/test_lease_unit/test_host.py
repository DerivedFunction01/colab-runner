"""Lease unit tests: initiation-time approval — the host on
the record, with stub colab callables (no subprocess)."""

import json
from pathlib import Path

from unit_harness import NOW, _acquire_stub, cp, gate_permissive, lease, make_renewal_stub, repo, seed_record, seed_window_record  # noqa: F401

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
