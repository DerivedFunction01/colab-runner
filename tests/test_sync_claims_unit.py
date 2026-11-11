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
import os
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
    # The backing record precedes the registration (the runner's
    # own order — and the pruned projection's premise): plant
    # the run first, then save the record that registers it.
    write_run(root, "run-b1", "beta", state="running")
    save_lease_record(root, caller="alpha", jobs=["run-b1"])
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


# --- State serving + CLI verbs (step B) ---------------------------------------


class _FakeColab:
    """A colab callable whose list reads empty: every recorded
    session reads recorded-but-dead, which is all the status
    shape needs — the sync claim rides independent of liveness."""

    def __call__(self, *args, timeout=None):
        import subprocess as _sp

        return _sp.CompletedProcess(args, 0, stdout="", stderr="")


def test_state_status_serves_the_live_claim(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    now = datetime.now(timezone.utc)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=now)
    out = lease_mod.status(_FakeColab(), root, SESSION)
    assert out["sync_claim"]["caller"] == "alpha"
    assert out["sync_claim"]["expires_at"] == iso(now + timedelta(minutes=60))
    lease_mod.release_sync_claim(root, SESSION, caller="alpha")
    out = lease_mod.status(_FakeColab(), root, SESSION)
    assert out["sync_claim"] is None


def _cli_env(tmp_path):
    import os as _os

    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    (home / ".config" / "colab-cli" / "sessions.json").write_text("{}")
    root = repo_fixture.cli_repo(tmp_path / "repo", tmp_path / "var")
    env = dict(
        _os.environ,
        HOME=str(home),
        COLAB_BIN=str(REPO / "tests" / "fixtures" / "fake_colab.py"),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        COLAB_LEASE_ACTOR="test",
    )
    env.pop("COLAB_CALLER", None)
    return env, root


def _run_lease(env, root, *args):
    import subprocess as _sp
    import sys as _sys

    r = _sp.run([_sys.executable, str(root / "bin" / "colab-lease"), *args],
                capture_output=True, text=True, env=env, timeout=120)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else {})


def test_claim_sync_verbs_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("COLAB_RUNNER_VAR", raising=False)
    env, root = _cli_env(tmp_path)
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    (var / f"lease-{SESSION}.json").write_text(json.dumps(
        {"session": SESSION, "caller": "alpha", "shape": "cpu-highram",
         "acquired_at": iso(T0), "deadline": iso(T0 + timedelta(hours=8)), "jobs": []}))
    rc, out = _run_lease(env, root, "claim-sync", SESSION, "--caller", "alpha")
    assert rc == 0 and out["action"] == "sync-claimed"
    # A foreign claim refuses with exit 1 and names the holder.
    rc, out = _run_lease(env, root, "claim-sync", SESSION, "--caller", "beta")
    assert rc == 1 and out["action"] == "sync-claim-refused-claim" and out["holder"] == "alpha"
    # The records state portion serves the claim on the record.
    rc, out = _run_lease(env, root, "state", "--part", "records")
    assert rc == 0
    entry = next(e for e in out["records"] if e["session"] == SESSION)
    assert entry["sync_claim"]["caller"] == "alpha"
    # Only the holder releases by name; then the claim is gone.
    rc, out = _run_lease(env, root, "release-sync", SESSION, "--caller", "beta")
    assert rc == 1 and out["action"] == "sync-release-refused"
    rc, out = _run_lease(env, root, "release-sync", SESSION, "--caller", "alpha")
    assert rc == 0 and out["action"] == "sync-released"
    rc, out = _run_lease(env, root, "release-sync", SESSION, "--caller", "alpha")
    assert rc == 0 and out["action"] == "sync-no-claim"


# --- Duration cap policy and long holds (E3) ---

import pytest


def test_max_sync_seconds_policy_default_override_and_invalid(tmp_path, monkeypatch):
    """E3: the policy resolves max_sync_seconds beside the TTL and
    window — 300 by default, config-overridable, and a non-positive
    value falls back to the default rather than disabling the cap."""
    root = repo(tmp_path, monkeypatch)
    assert lease_mod.sync_claim_policy(root)["max_sync_seconds"] == 300
    cfg = json.loads((root / "config" / "lease.json").read_text())
    cfg["sync_claim"] = {"max_sync_seconds": 42}
    (root / "config" / "lease.json").write_text(json.dumps(cfg))
    assert lease_mod.sync_claim_policy(root)["max_sync_seconds"] == 42
    cfg["sync_claim"] = {"max_sync_seconds": -5}
    (root / "config" / "lease.json").write_text(json.dumps(cfg))
    assert lease_mod.sync_claim_policy(root)["max_sync_seconds"] == 300


def test_long_hold_survives_quiet_and_the_original_ttl(tmp_path, monkeypatch):
    """E3 (decision 1, amended): while the hold is unexpired the
    claim is live evidence — it does not lapse when the holder
    goes quiet, and it does not die at the original TTL either;
    a second caller is still refused by it. Past the hold's end
    the claim is gone."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    claim = lease_mod.syncclaims.note_long_hold(root, SESSION, "alpha", 7200, now_dt=T0)
    assert claim["long_hold_until"] == iso(T0 + timedelta(seconds=7200))
    assert claim["expires_at"] == claim["long_hold_until"]
    inside = T0 + timedelta(minutes=90)  # past the 60-minute TTL
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=inside) is not None
    ev = lease_mod.active_callers(root, SESSION, inside)
    assert ev["alpha"][0]["kind"] == "long-hold"
    verdict = lease_mod.sync_rights(root, SESSION, "beta", now_dt=inside)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "claim"
    past = T0 + timedelta(seconds=7201)
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=past) is None


def test_note_long_hold_requires_the_callers_own_claim(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    with pytest.raises(ValueError):
        lease_mod.syncclaims.note_long_hold(root, SESSION, "beta", 7200, now_dt=T0)
    with pytest.raises(ValueError):
        lease_mod.syncclaims.note_long_hold(root, "no-such-session", "alpha", 7200, now_dt=T0)


# --- Refresh degradation (E12, decision 8 amended) ---


def test_refresh_degrades_under_write_denial(tmp_path, monkeypatch):
    """E12: the holder's refresh whose store write fails is
    allowed on the stored claim and journaled as a degradation —
    never a refusal."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)

    def denied(*a, **k):
        raise PermissionError("flap")

    monkeypatch.setattr(lease_mod.records, "save_record", denied)
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=5))
    assert verdict["allowed"] is True and verdict["refresh_degraded"] is True
    assert verdict["claim"]["caller"] == "alpha"
    lines = [json.loads(x) for x in lease_mod.caller_claims_path(root).read_text().splitlines() if x.strip()]
    assert any(
        ln.get("seam") == "sync-claim-refresh-degraded" and ln.get("caller") == "alpha" for ln in lines
    )


def test_grant_stays_strict_under_write_denial(tmp_path, monkeypatch):
    """E12's other half: a grant under the same denial raises —
    an unstored claim is not granted."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)

    def denied(*a, **k):
        raise PermissionError("flap")

    monkeypatch.setattr(lease_mod.records, "save_record", denied)
    with pytest.raises(PermissionError):
        lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=T0) is None


# --- Bounded journal tail (E15b) ---


def test_journal_tail_read_matches_full_read_verdicts(tmp_path, monkeypatch):
    """E15(b): with a journal far larger than the tail bound, the
    verdicts are the full-read verdicts — stale callers beyond the
    window are absent, the fresh attach inside it is evidence."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    stale = T0 - timedelta(days=2)
    path = lease_mod.caller_claims_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(3000):
            fh.write(json.dumps({
                "ts": iso(stale), "seam": "colab-sh", "caller": "gamma",
                "session": SESSION, "action": "attach",
            }) + "\n")
    write_attach(root, "beta", T0 - timedelta(minutes=5))
    assert path.stat().st_size > lease_mod.syncclaims.JOURNAL_TAIL_BYTES
    tail = lease_mod.syncclaims._journal_tail_lines(path)
    assert sum(len(x) for x in tail) < path.stat().st_size
    ev = lease_mod.active_callers(root, SESSION, T0)
    assert "gamma" not in ev
    assert ev["beta"][0]["kind"] == "attach"
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
    assert verdict["holder"] == "beta"


# --- Detached records as evidence (E8) ---


def detached_rec(did, caller="beta", state="running", session=SESSION):
    return {
        "id": did, "session": session, "caller": caller, "state": state,
        "exit_code": None if state == "running" else 0,
        "started_at": iso(T0), "finished_at": None if state == "running" else iso(T0),
    }


def test_detached_record_is_activity_evidence_until_terminal(tmp_path, monkeypatch):
    """E8: a running detached record is a second caller's refusal
    evidence (kind detached); once terminal it evidences nothing."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.syncclaims.write_detached_record(root, detached_rec("d1"))
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=1))
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
    assert verdict["holder"] == "beta" and verdict["evidence"][0]["kind"] == "detached"
    lease_mod.syncclaims.mark_detached_terminal(root, "d1", "complete", 0, now_dt=T0 + timedelta(minutes=2))
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=3))
    assert verdict["allowed"] is True


def test_detached_evidence_dedupes_record_jobs_and_scan(tmp_path, monkeypatch):
    """The same launch seen via the record's jobs list and the
    record scan is one evidence entry, not two."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root, jobs=["detached:d2"])
    lease_mod.syncclaims.write_detached_record(root, detached_rec("d2"))
    ev = lease_mod.active_callers(root, SESSION, T0 + timedelta(minutes=1))
    kinds = [item["kind"] for item in ev["beta"]]
    assert kinds == ["detached"]


def mark_active(root, *names):
    """Rewrite the scratch registry with an "active" marking
    (the fixture writes the base shape; the marking is the
    registry's optional sibling list)."""
    path = root / "config" / "callers.json"
    data = json.loads(path.read_text())
    data["active"] = list(names)
    path.write_text(json.dumps(data))


def test_active_caller_refused_sync_on_a_quiet_session(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    mark_active(root, "alpha")
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False
    assert verdict["action"] == "sync-refused-active-caller"
    assert verdict["blocked_by"] == "active-caller"
    assert verdict["holder"] == "alpha"
    assert verdict["evidence"] == [{"kind": "active-caller", "caller": "alpha"}]
    # No claim is granted to an active caller, even at sole
    # occupancy — the refusal is the standing marking, not a
    # contention outcome.
    assert lease_mod.read_sync_claim(root, SESSION, now_dt=T0) is None
    out = lease_mod.claim_sync(root, SESSION, "alpha", now_dt=T0)
    assert out["action"] == "sync-claim-refused-active-caller" and out["holder"] == "alpha"
    # A lane of the marked base is refused under the same rule.
    verdict = lease_mod.sync_rights(root, SESSION, "alpha:lane-1", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "active-caller"
    assert verdict["evidence"] == [{"kind": "active-caller", "caller": "alpha"}]


def test_active_refusal_outranks_claim_and_activity(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    lease_mod.claim_sync(root, SESSION, "beta", now_dt=T0)
    mark_active(root, "alpha")
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0 + timedelta(minutes=1))
    # Beta's live claim stands, but alpha's refusal names its own
    # marking first — that verdict never lifts with occupancy.
    assert verdict["allowed"] is False and verdict["blocked_by"] == "active-caller"
    assert verdict["claim"]["caller"] == "beta"


def test_unmarked_caller_syncs_as_before(tmp_path, monkeypatch):
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    mark_active(root, "alpha")
    verdict = lease_mod.sync_rights(root, SESSION, "beta", now_dt=T0)
    assert verdict["allowed"] is True and verdict["auto_claimed"] is True
    assert verdict["claim"]["caller"] == "beta"


def _set_mtime(path, dt):
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def _write_lease_record_raw(root, caller="alpha", jobs=()):
    """The lease record written without save_record's pruning —
    the shape a record that has not been re-saved since its
    runs closed still carries on disk."""
    rec = {
        "session": SESSION,
        "caller": caller,
        "shape": "cpu-highram",
        "acquired_at": iso(T0),
        "deadline": iso(T0 + timedelta(hours=8)),
        "jobs": list(jobs),
    }
    path = root / "var" / f"lease-{SESSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    return rec


def test_stale_run_record_is_not_activity(tmp_path, monkeypatch):
    """Run-record evidence carries a freshness bound: a
    non-terminal record nothing has re-saved inside the bound
    is a dead launcher's residue and does not hold its caller
    active (2026-11-11: the consol lane's dead feat records
    refused other lanes' sync plants for hours). A freshly
    tended record of the same shape is activity (control)."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    write_run(root, "run-dead", "beta", state="running")
    path = root / "results" / "runs" / "run-dead.json"
    _set_mtime(path, T0 - timedelta(hours=2))
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is True
    _set_mtime(path, T0 - timedelta(minutes=5))
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"


def test_registered_job_with_closed_or_missing_backing_is_not_activity(tmp_path, monkeypatch):
    """Registrations resolve through the pruned projection: a
    job id whose backing run is terminal — or has no record at
    all — is not evidence, however the record carrying it was
    written. A live backing is (control)."""
    root = repo(tmp_path, monkeypatch)
    write_run(root, "run-done", "beta", state="complete")
    _write_lease_record_raw(root, caller="alpha", jobs=["run-done", "run-ghost"])
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is True
    write_run(root, "run-live", "beta", state="running")
    _write_lease_record_raw(root, caller="alpha", jobs=["run-live"])
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
    kinds = {e["kind"] for e in verdict["evidence"]}
    assert "job" in kinds


def test_detached_record_older_than_a_session_lifetime_is_not_activity(tmp_path, monkeypatch):
    """A free detached record stays evidence at most one
    session lifetime (12 h): nothing outlives the cap of the
    session it runs on, so an older 'running' detached record
    is residue, not activity."""
    root = repo(tmp_path, monkeypatch)
    save_lease_record(root)
    rec = detached_rec("d-old")
    rec["started_at"] = iso(T0 - timedelta(hours=13))
    lease_mod.syncclaims.write_detached_record(root, rec)
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is True
    rec["started_at"] = iso(T0 - timedelta(hours=1))
    lease_mod.syncclaims.write_detached_record(root, rec)
    verdict = lease_mod.sync_rights(root, SESSION, "alpha", now_dt=T0)
    assert verdict["allowed"] is False and verdict["blocked_by"] == "activity"
