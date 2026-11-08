"""Synchronous channel claim unit tests (design of record:
.plans/2026-11-08-sync-channel-design.md, option 3 composite).

The claim core against a scratch fixture repo: grants at sole
occupancy, refusals naming the holder (a live foreign claim, or
foreign activity with no claim), auto-claim on first attach, TTL
and holder-quiet lapse, the block-new-calls-but-claim-survives
transition, and release. Time is injected (now_dt) throughout —
no wall clock, no network.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "lease_mod", REPO / "sync" / "lease" / "__init__.py",
    submodule_search_locations=[str(REPO / "sync" / "lease")])
lease_mod = importlib.util.module_from_spec(_spec)
sys.modules["lease_mod"] = lease_mod
_spec.loader.exec_module(lease_mod)

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)

T0 = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
SESSION = "amber-otter-42"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def repo(tmp_path, monkeypatch):
    root = repo_fixture.scratch_repo(tmp_path)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(root / "var"))
    return root


def save_lease_record(root, caller="alpha", jobs=()):
    rec = {
        "session": SESSION,
        "caller": caller,
        "shape": "cpu-highram",
        "acquired_at": iso(T0),
        "deadline": iso(T0 + timedelta(hours=8)),
        "jobs": list(jobs),
    }
    lease_mod.save_record(root, rec)
    return rec


def write_run(root, run_id, caller, state="running", session=SESSION):
    runs = root / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps(
        {"run_id": run_id, "session": session, "caller": caller, "state": state}))


def write_attach(root, caller, ts, seam="colab-sh"):
    path = lease_mod.caller_claims_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps({"ts": iso(ts), "session": SESSION, "caller": caller, "seam": seam}) + "\n")


def test_claim_granted_at_sole_occupancy_lands_on_the_record(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    out = lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    assert out["action"] == "sync-claimed"
    assert out["claim"]["caller"] == "alpha"
    assert out["claim"]["expires_at"] == iso(T0 + timedelta(minutes=60))
    stored = lease_mod.load_record(root, SESSION)
    assert stored["sync_claim"]["caller"] == "alpha"
    # Record exists: the claim's one home is the record, no file.
    assert not (root / "var" / f"sync-claim-{SESSION}.json").exists()


def test_auto_claim_on_first_attach_recordless_session(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    verdict = lease_mod.sync_rights(root, SESSION, "beta", now_dt=T0)
    assert verdict["allowed"] is True and verdict["auto_claimed"] is True
    claim_file = root / "var" / f"sync-claim-{SESSION}.json"
    assert claim_file.exists()
    claim = lease_mod.read_sync_claim(root, SESSION, now_dt=T0)
    assert claim["caller"] == "beta"
    # The grant is journaled in the caller-claims journal.
    lines = [json.loads(x) for x in lease_mod.caller_claims_path(root).read_text().splitlines() if x.strip()]
    assert any(l["seam"] == "sync-claim" and l["caller"] == "beta" for l in lines)


def test_refusal_names_a_live_foreign_claim(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    verdict = lease_mod.sync_rights(root, SESSION, "beta", now_dt=T0 + timedelta(minutes=1))
    assert verdict["allowed"] is False
    assert verdict["blocked_by"] == "claim"
    assert verdict["holder"] == "alpha"
    assert verdict["expires_at"] == iso(T0 + timedelta(minutes=60))
    out = lease_mod.claim_sync(root, SESSION, "beta", now_dt=T0 + timedelta(minutes=1))
    assert out["action"] == "sync-claim-refused-claim" and out["holder"] == "alpha"


def test_refusal_names_foreign_activity_without_a_claim(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    write_run(root, "run-b1", "beta", state="running")
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False
    assert verdict["blocked_by"] == "activity"
    assert verdict["holder"] == "beta"
    assert verdict["evidence"][0]["kind"] == "run"


def test_job_registered_on_the_record_is_activity(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root, caller="alpha", jobs=["run-b1"])
    write_run(root, "run-b1", "beta", state="running")
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
    kinds = {e["kind"] for e in verdict["evidence"]}
    assert "job" in kinds


def test_attach_inside_the_window_is_activity_outside_is_not(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    write_attach(root, "beta", T0 - timedelta(minutes=10))
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
    assert verdict["evidence"][0]["kind"] == "attach"
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=20))
    assert verdict["allowed"] is True


def test_ttl_lapse_frees_the_session(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    later = T0 + timedelta(minutes=61)
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=later) is None
    out = lease_mod.claim_sync(root, SESSION, "beta", now_dt=later)
    assert out["action"] == "sync-claimed" and out["claim"]["caller"] == "beta"


def test_holder_quiet_lapse_and_evidence_keeps_a_claim_live(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    quiet_at = T0 + timedelta(minutes=20)  # past the 15m window, inside the 60m TTL
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=quiet_at) is None
    verdict = lease_mod.sync_rights(root, SESSION, "beta", now_dt=quiet_at)
    assert verdict["allowed"] is True and verdict["auto_claimed"] is True
    # Same timeline, but the holder has a live run: the claim stands.
    root2 = repo(tmp_path / "second", monkeypatch)
    save_lease_record(root2)
    lease_mod.claim_sync(root2, SESSION, "alpha", now_dt=T0)
    write_run(root2, "run-a1", "alpha", state="running")
    assert lease_mod.read_sync_claim(root2, SESSION, now_dt=quiet_at)["caller"] == "alpha"
    verdict = lease_mod.sync_rights(root2, SESSION, "beta", now_dt=quiet_at)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "claim"


def test_second_caller_blocks_new_calls_claim_survives_and_resumes(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    # Holder attaches again inside the window: allowed, refreshed.
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=5))
    assert verdict["allowed"] is True
    # A second caller becomes active: the holder's NEW sync calls
    # refuse on activity, but the claim itself is not revoked.
    write_run(root, "run-b1", "beta", state="running")
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=7))
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
    assert verdict["holder"] == "beta"
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=T0 + timedelta(minutes=7))["caller"] == "alpha"
    # The other work ends: the claim resumes without re-claiming.
    write_run(root, "run-b1", "beta", state="complete")
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=9))
    assert verdict["allowed"] is True and verdict["auto_claimed"] is False


def test_release_frees_immediately_and_only_the_holder_releases(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    out = lease_mod.release_sync_claim(root, SESSION, caller="beta")
    assert out["action"] == "sync-release-refused" and out["holder"] == "alpha"
    out = lease_mod.release_sync_claim(root, SESSION, caller="alpha")
    assert out["action"] == "sync-released"
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=T0) is None
    verdict = lease_mod.sync_rights(root, SESSION, "beta", now_dt=T0)
    assert verdict["allowed"] is True and verdict["auto_claimed"] is True
    # Operator release (no caller) clears a foreign claim; a
    # release with nothing stored is a no-claim verdict.
    out = lease_mod.release_sync_claim(root, SESSION)
    assert out["action"] == "sync-released"
    out = lease_mod.release_sync_claim(root, SESSION)
    assert out["action"] == "sync-no-claim"


def test_claim_moves_onto_the_record_when_one_appears(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)  # standalone file
    claim_file = root / "var" / f"sync-claim-{SESSION}.json"
    assert claim_file.exists()
    save_lease_record(root)
    # The record-less claim still reads (the file is its home
    # until the next store), and the holder's next attach moves
    # it onto the record and retires the file.
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=T0)["caller"] == "alpha"
    lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=1))
    assert not claim_file.exists()
    assert lease_mod.load_record(root, SESSION)["sync_claim"]["caller"] == "alpha"


def test_unattributed_caller_passes_quiet_and_refuses_contention(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    verdict = lease_mod.sync_rights(root, SESSION, None, now_dt=T0)
    assert verdict["allowed"] is True and verdict["claim"] is None
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    verdict = lease_mod.sync_rights(root, SESSION, None, now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "claim"
    out = lease_mod.claim_sync(root, SESSION, None, now_dt=T0)
    assert out["action"] == "sync-claim-unattributed"


def test_config_knobs_resize_ttl_and_window(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    cfg_path = root / "config" / "lease.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["sync_claim"] = {"ttl_minutes": 30, "attach_window_minutes": 5}
    cfg_path.write_text(json.dumps(cfg))
    out = lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    assert out["claim"]["expires_at"] == iso(T0 + timedelta(minutes=30))
    # A 10-minute-old attach is outside the resized 5m window.
    root2 = repo(tmp_path / "second", monkeypatch)
    cfg2 = json.loads((root2 / "config" / "lease.json").read_text())
    cfg2["sync_claim"] = {"ttl_minutes": 30, "attach_window_minutes": 5}
    (root2 / "config" / "lease.json").write_text(json.dumps(cfg2))
    write_attach(root2, "beta", T0 - timedelta(minutes=10))
    verdict = lease_mod.sync_rights(root2, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is True
