"""Lease unit tests: the served state document — torn
records, dial caches, and the jobs projection, with stub
colab callables (no subprocess)."""

import importlib
import json
import sys
from datetime import timedelta
from importlib.machinery import SourceFileLoader

from unit_harness import NOW, REPO, cp, gate_permissive, lease, repo, seed_record  # noqa: F401


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
