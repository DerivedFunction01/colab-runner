"""Concurrent leases at the module seam (in-process against
FakeColab): riding, parallel creation, the idle guard, budget
pricing of concurrent windows, reap independence and locking,
state composition, and the spend journal."""

import json
from datetime import timedelta
from pathlib import Path

from fake_harness import PNOW, FakeColab, lease_mod, scratch_repo  # noqa: F401

# --- Concurrent leases (profile retirement) ------------------------------------
# The multi-lease lifecycle at the module seam: a stateful
# in-process fake of the colab callable drives lease.acquire/reap/
# release against a scratch repo (config + symlinked snippets), so
# the exact CLI calls issued are assertable.


def test_second_caller_rides_the_existing_lease(tmp_path, monkeypatch):
    """A session is shared capacity (operator ruling, 2026-11-05):
    a second caller at the same shape uses the first caller's live
    lease — no new session is created, the record keeps its
    creator's stamp, and the ride is journaled as the rider's
    claim. Attribution is knowing who called, never a gate on use."""
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    a = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    b = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="beta")
    assert a["action"] == "created"
    assert b["action"] == "adopted"
    assert b["record"]["session"] == a["record"]["session"]
    assert b["riding"] is True and b["owner_caller"] == "alpha"
    assert not [c for c in fake.calls if c[0] == "new" and "-s" in c and c[-1] != a["record"]["session"]]
    assert len(lease_mod.all_records(repo)) == 1
    assert lease_mod.all_records(repo)[0]["caller"] == "alpha"
    claims = lease_mod.load_caller_claims(repo)
    assert claims[a["record"]["session"]]["caller"] == "beta"


def test_create_mints_a_parallel_lease_instead_of_riding(tmp_path, monkeypatch):
    """An explicit create (the panel's Start, operator ruling
    2026-11-05) never adopts: with a live lease at the shape it
    mints a second, parallel session. Plain acquire keeps riding
    — creating is what --create is for; using the current lease
    is extend's job."""
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    a = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    b = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="panel", create=True)
    assert a["action"] == "created"
    assert b["action"] == "created"
    assert b["record"]["session"] != a["record"]["session"]
    assert b["record"]["caller"] == "panel"
    assert len(lease_mod.all_records(repo)) == 2
    c = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="beta")
    assert c["action"] == "adopted"


def test_reap_idle_guard_respects_registered_jobs(tmp_path, monkeypatch):
    """Co-tenancy on the lease side: the idle reap releases a
    session only when its jobs list is empty. A registered run
    holds the session past the idle threshold — however many
    other runs share it — and once the last job finishes, the
    same idle reap releases it."""
    from datetime import timedelta

    repo = scratch_repo(tmp_path, monkeypatch)
    cfg_path = tmp_path / "config" / "lease.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["idle_shutdown_minutes"] = 30
    cfg_path.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    fake = FakeColab()
    a = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    assert a["action"] == "created"
    # Registrations hold through their backing records (the
    # pruned-jobs projection): plant a live record per held
    # run, and a terminal one for the ghost — the ghost's
    # registration must neither hold nor report.
    runs_dir = repo / "results" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for rid, st in (("run-held-1", "running"), ("run-held-2", "running"), ("run-ghost", "complete")):
        (runs_dir / f"{rid}.json").write_text(json.dumps({"run_id": rid, "session": a["record"]["session"], "state": st, "started_at": PNOW.isoformat()}))
    lease_mod.touch_job(repo, "run-held-1", now_dt=PNOW)
    lease_mod.touch_job(repo, "run-held-2", now_dt=PNOW)
    lease_mod.touch_job(repo, "run-ghost", now_dt=PNOW)
    later = PNOW + timedelta(minutes=31)
    out = lease_mod.reap(fake, repo, now_dt=later)
    assert out["action"] == "kept" and sorted(out["jobs"]) == ["run-held-1", "run-held-2"]
    lease_mod.finish_job(repo, "run-held-1", now_dt=later)
    lease_mod.finish_job(repo, "run-held-2", now_dt=later)
    out = lease_mod.reap(fake, repo, now_dt=later + timedelta(minutes=31))
    assert out["action"] == "released-idle"


def test_acquire_unknown_shape_creates_nothing(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "t99", now_dt=PNOW)
    assert out["action"] == "unknown-shape"
    assert not [c for c in fake.calls if c[0] in ("new", "stop") and "-s" in c]  # nothing created or stopped


def test_reap_covers_concurrent_leases_independently(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"one", "two"})
    for session in ("one", "two"):
        lease_mod.save_record(repo, {"shape": "cpu", "session": session, "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "last_pulse_at": PNOW.isoformat(), "jobs": []})
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert {r["session"]: r["action"] for r in out["records"]} == {"one": "kept", "two": "kept"}
    # Expire only the second: it alone is released at its deadline.
    rec = lease_mod.load_record(repo, "two")
    rec["deadline"] = (PNOW - timedelta(hours=1)).isoformat()
    lease_mod.save_record(repo, rec)
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert {r["session"]: r["action"] for r in out["records"]} == {"one": "kept", "two": "released-deadline"}
    assert lease_mod.load_record(repo, "one") is not None
    # A confirmed-dead second session is cleared, the first untouched.
    lease_mod.save_record(repo, {"shape": "cpu", "session": "two", "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    fake.alive.discard("two")
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert {r["session"]: r["action"] for r in out["records"]} == {"one": "kept", "two": "cleared-dead"}


def test_budget_guard_prices_concurrent_windows(tmp_path, monkeypatch):
    """Daily cap 1.0: one 8 h cpu window projects 0.64 and fits;
    a capacity request (the duty seam — the route that creates a
    second window rather than riding the live one) is refused
    while that window is live: concurrent windows are priced as
    the sum they commit (1.28), reported as committed_projected.
    An ordinary acquire by another caller simply rides the live
    lease and prices nothing new."""
    repo = scratch_repo(tmp_path, monkeypatch, daily_cap=1.0)
    fake = FakeColab(alive={"one"})
    lease_mod.save_record(repo, {"shape": "cpu", "session": "one", "caller": "alpha", "rate_cu_per_hour": 0.08, "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="beta", duty=True)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    # The other window runs 18:00Z -> 02:00Z: the daily check
    # clips its committed share to the 6 h inside today (0.48);
    # the unclipped sum (0.64) rides as committed_full.
    assert out["budget"]["committed_projected"] == 0.48
    assert out["budget"]["committed_full"] == 0.64
    # Control: with the other record gone the same request fits.
    lease_mod.delete_record(repo, "one")
    fake2 = FakeColab()
    out = lease_mod.acquire(fake2, repo, "cpu", now_dt=PNOW, caller="beta")
    assert out["action"] == "created"
    # And the mirror direction: a third caller's capacity request
    # prices the live window the same way.
    out = lease_mod.acquire(FakeColab(alive={out["record"]["session"]}), repo, "cpu", now_dt=PNOW, caller="gamma", duty=True)
    assert out["action"] == "refused-budget"
    assert out["budget"]["committed_projected"] == 0.48
    assert out["budget"]["committed_full"] == 0.64


def test_budget_daily_check_clips_other_windows_to_today(tmp_path, monkeypatch):
    """The 2026-11-07 refusal, as a fixture: a multi-day keeper
    window (56 h at 0.08) and a same-shape window (9 h at 0.26)
    commit 6.82 unclipped, which with an 8 h window priced at
    2.08 busts a daily cap of 8. Clipped to today (18:00Z ->
    24:00Z = 6 h each) they commit 2.04 and the window fits;
    a tighter cap of 5 still refuses, reporting the clipped
    figure as committed_projected and the full one as
    committed_full."""
    repo = scratch_repo(tmp_path, monkeypatch, daily_cap=8.0)
    fake = FakeColab()
    lease_mod.save_record(repo, {"shape": "cpu", "session": "keeper", "caller": "panel", "rate_cu_per_hour": 0.08, "acquired_at": (PNOW - timedelta(hours=10)).isoformat(), "deadline": (PNOW + timedelta(hours=56)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    lease_mod.save_record(repo, {"shape": "cpu-highram", "session": "sibling", "caller": "alpha", "rate_cu_per_hour": 0.26, "acquired_at": (PNOW - timedelta(hours=2)).isoformat(), "deadline": (PNOW + timedelta(hours=9)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    cfg = lease_mod.load_config(repo)
    shape = {"id": "cpu-highram", "rate_cu_per_hour": 0.26}
    out = lease_mod.budget_check(fake, repo, cfg, shape, hours=8.0, now_dt=PNOW, exclude_session="candidate")
    assert out["ok"] is True
    cfg["daily_cu_budget"] = 5.0
    out = lease_mod.budget_check(fake, repo, cfg, shape, hours=8.0, now_dt=PNOW, exclude_session="candidate")
    assert out["ok"] is False and out["reason"] == "daily-budget"
    assert out["committed_projected"] == 2.04
    assert out["committed_full"] == 6.82


def test_reap_passes_never_overlap(tmp_path, monkeypatch):
    """One reap pass at a time: while the pass lock is held, a
    second reap returns already-running and touches nothing —
    overlapping passes renewing the same lease in parallel
    minted duplicate successor leases (2026-11-08). The lock is
    an flock, so a dead holder releases it: once the fd closes,
    reap proceeds normally."""
    import fcntl
    import os

    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"one"})
    lease_mod.save_record(repo, {"shape": "cpu", "session": "one", "caller": "alpha", "rate_cu_per_hour": 0.08, "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    lockpath = repo / "var" / "reap.lock"
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        out = lease_mod.reap(fake, repo, now_dt=PNOW)
        assert out["action"] == "already-running"
        assert out["records"] == []
    finally:
        os.close(fd)
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert out["action"] != "already-running"
    assert lease_mod.load_record(repo, "one") is not None


def test_reap_classification_uses_the_reap_budget(tmp_path, monkeypatch):
    """A stalling sessions read inside reap classifies unknown
    after the reap budget (2 attempts), not the interactive one
    (3): the pass repeats in minutes, so it buys back its time
    budget and lets the next pass retry — unknown is kept, never
    cleared."""
    import subprocess as sp

    repo = scratch_repo(tmp_path, monkeypatch)
    lease_mod.save_record(repo, {"shape": "cpu", "session": "one", "caller": "alpha", "rate_cu_per_hour": 0.08, "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})

    class StallColab(FakeColab):
        def __call__(self, *a, **k):
            if a and a[0] == "sessions":
                raise sp.TimeoutExpired("colab", k.get("timeout", 0))
            return super().__call__(*a, **k)

    out = lease_mod.reap(StallColab(), repo, now_dt=PNOW)
    assert out["records"][0]["action"] == "kept-unknown"
    assert out["records"][0]["attempts"] == 2
    assert lease_mod.load_record(repo, "one") is not None


def test_state_portions_and_composition(tmp_path, monkeypatch):
    """Progressive loading: each portion of the composed state read
    stands alone with its own keys, and state() composes them into
    exactly the payload it served before the split."""
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    lease_mod.acquire(fake, repo, "cpu-highram", now_dt=PNOW)

    records = lease_mod.state_records(fake, repo)
    assert {r["record"]["shape"] for r in records["records"]} == {"cpu", "cpu-highram"}

    specs = lease_mod.state_specs(fake, repo)
    assert "specs" in specs and isinstance(specs["specs"], list)

    budget = lease_mod.state_budget(fake, repo)
    assert "budget" in budget and "balance" in budget["budget"]

    grant = lease_mod.state_grant(fake, repo)
    assert "grant" in grant and "envelope" in grant

    meta = lease_mod.state_meta(fake, repo)
    assert "bridge" in meta

    guard = lease_mod.state_extend_guard(repo)
    assert "extend_guard" in guard and "policy" in guard["extend_guard"]

    orphans = lease_mod.state_orphans(fake, repo)
    assert orphans == {"orphans": []}  # both fake sessions hold records

    listeners = lease_mod.state_listeners(repo)
    assert set(listeners) == {"listeners"}
    assert set(listeners["listeners"]) == {r["record"]["session"] for r in records["records"]}

    composed = lease_mod.state(fake, repo)
    # The fake's help output parses as no catalog, so the specs
    # portion's catalog_error rides the composed payload exactly as
    # it did before the split.
    assert set(composed) == {"records", "grant", "specs", "budget", "envelope", "bridge", "catalog_error", "session_gate", "access", "extend_guard", "orphans", "torn_records", "listeners"}
    assert composed["orphans"] == orphans["orphans"]
    assert composed["listeners"] == listeners["listeners"]
    assert composed["catalog_error"] == specs["catalog_error"]
    assert composed["records"] == records["records"]
    assert composed["budget"] == budget["budget"]


def test_spend_journal_keys_segments_by_session(tmp_path, monkeypatch):
    """A released lease journals its segment under its own session
    and shape at the record's rate snapshot, caller included."""
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    session = out["record"]["session"]
    out = lease_mod.release(fake, repo, session=session, now_dt=PNOW + timedelta(hours=1))
    assert out["action"] == "released"
    lines = (tmp_path / "var" / "lease-spend-journal.jsonl").read_text().strip().split("\n")
    seg = json.loads(lines[-1])
    assert seg["shape"] == "cpu" and seg["session"] == session and seg["rate"] == 0.08
    assert seg["caller"] == "alpha"


def test_acquire_primes_the_initiating_scope_on_a_new_host(tmp_path, monkeypatch):
    """Initiation-time approval, Step 2: when the acquire births a
    session on a tunnel host, the initiating entry point primes its
    own scope with one cheap call (bin/colab-lease rides
    prime_session_host on the acquire result) and the host persists
    on the record."""
    repo = scratch_repo(tmp_path, monkeypatch)
    host = "8080-fake-a.us-east1.prod.r.colab.dev"

    class BindingColab(FakeColab):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            if a and a[0] == "new" and "-s" in a:
                f = Path(tmp_path) / "home" / ".config" / "colab-cli" / "sessions.json"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps({a[a.index("-s") + 1]: {"url": f"https://{host}"}}))
            return out

    fake = BindingColab()
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    assert out["action"] == "created"
    assert out["host_changed"] is True and out["host"] == host
    assert lease_mod.load_record(repo)["host"] == host
    prime = lease_mod.prime_session_host(fake, repo, out["record"]["session"])
    assert prime["primed"] is True
    out2 = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    assert out2["action"] == "adopted"
    assert "host_changed" not in out2  # host unchanged: nothing to prime
