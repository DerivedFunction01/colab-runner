"""Lease unit tests: orphan evidence derivation and
adoption, with stub colab callables (no subprocess)."""

import json
import subprocess
from datetime import timedelta

import pytest

from unit_harness import NOW, _acquire_stub, cp, gate_permissive, lease, make_extend_stub, orphan_stub, plant_kit_config, repo, seed_record  # noqa: F401

# --- Orphan evidence and adoption ---------------------------------------------
#
# An orphan is a live session with no lease record. These tests
# pin the derivation priority (renewal-config first, the panel
# outbox's lease-extended chain over it, nothing else carrying a
# deadline), the creator-over-claims caller rule, and the adopt
# refusal classes.


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
