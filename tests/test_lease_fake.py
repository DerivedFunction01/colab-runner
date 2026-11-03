"""Lease lifecycle against the fake colab CLI (no login, no network).

Covers the lease model's core promises: one session per profile acquired
once and adopted afterwards, the reaper enforcing the hard deadline, a
record whose session died being cleared, and manual release.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"


def lease_env(tmp_path):
    return dict(
        os.environ,
        COLAB_BIN=str(FAKE),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        # Keepalive: two instant cycles in the sandbox, then exit — an
        # unbounded loop here would leak a process onto this machine.
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
    )


def run_lease(env, *args):
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def record_path(env):
    # Records live per profile since the two-tier build; the legacy
    # lease.json is only the migration fallback.
    var = Path(env["COLAB_RUNNER_VAR"])
    per_profile = var / "lease-standard.json"
    return per_profile if per_profile.exists() else var / "lease.json"


def record(env):
    return json.loads(record_path(env).read_text())


def test_acquire_adopt_status(tmp_path):
    env = lease_env(tmp_path)
    first = run_lease(env, "acquire", "standard")
    assert first["action"] == "created"
    assert first["keepalive"] == "launched"
    assert first["record"]["session"] == "standard"
    sandbox_log = Path(env["FAKE_COLAB_STATE"]) / "sessions" / "standard" / "keepalive.log"
    import time

    for _ in range(50):
        if sandbox_log.exists() and sandbox_log.read_text().strip():
            break
        time.sleep(0.2)
    assert sandbox_log.exists() and json.loads(sandbox_log.read_text().strip().split("\n")[-1])["cycle"] >= 1
    second = run_lease(env, "acquire", "standard")
    assert second["action"] == "adopted"
    assert second["record"]["acquired_at"] == first["record"]["acquired_at"]
    assert run_lease(env, "status")["action"] == "active"


def test_reap_releases_after_deadline(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "standard")
    assert run_lease(env, "reap")["action"] == "kept"
    rec = record(env)
    rec["deadline"] = "2000-01-01T00:00:00+00:00"
    record_path(env).write_text(json.dumps(rec))
    assert run_lease(env, "reap")["action"] == "released-deadline"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_reap_clears_dead_session(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "standard")
    active = Path(env["FAKE_COLAB_STATE"]) / "sessions" / "standard" / "ACTIVE"
    active.unlink()
    assert run_lease(env, "reap")["action"] == "cleared-dead"
    assert run_lease(env, "status")["action"] == "no-lease"


def test_release(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "standard")
    assert run_lease(env, "release")["action"] == "released"
    assert run_lease(env, "status")["action"] == "no-lease"


def dummy_home_env(env, tmp_path):
    """Point HOME at a scratch home with a dummy token, so the
    renewal kit's token staging never touches the real login."""
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    env["HOME"] = str(home)
    return env


def test_extend_moves_deadline_and_flags_renew(tmp_path):
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "standard", "--hours", "2")
    before = record(env)
    out = run_lease(env, "extend", "standard", "--hours", "1")
    assert out["action"] == "extended"
    # The delta is priced at the profile's rate: 0.08 x 1 h.
    assert out["budget"]["projected"] == 0.08
    after = record(env)
    delta = datetime.fromisoformat(after["deadline"]) - datetime.fromisoformat(before["deadline"])
    assert delta == timedelta(hours=1)
    assert after["renew"] is True
    assert after["session"] == "standard"  # the session is untouched


def test_extend_cli_rejects_bad_hours(tmp_path):
    env = lease_env(tmp_path)
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), "extend", "standard", "--hours", "0"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), "extend", "standard"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2


def test_reap_renews_window_lease_with_fresh_session(tmp_path):
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "standard", "--hours", "2")
    rec = record(env)
    now = datetime.now(timezone.utc)
    rec["renew"] = True
    rec["acquired_at"] = (now - timedelta(hours=12)).isoformat()  # past the threshold
    rec["deadline"] = (now + timedelta(hours=12)).isoformat()
    record_path(env).write_text(json.dumps(rec))
    out = run_lease(env, "reap")
    assert out["action"] == "renewed"
    assert out["driver"] == 2  # no kit in the fake world: reaper fallback
    after = record(env)
    assert after["session"].startswith("standard-r")
    assert after["deadline"] == rec["deadline"]  # the window end never moves
    sessions = Path(env["FAKE_COLAB_STATE"]) / "sessions"
    assert (sessions / after["session"] / "ACTIVE").exists()
    assert not (sessions / "standard" / "ACTIVE").exists()


# --- Parallel instances (plan: .plans/2026-11-03-parallel-cpu-sessions.md) -----
# The instance lifecycle at the module seam: a stateful in-process fake
# of the colab callable drives lease.acquire/reap/release against a
# scratch repo (config + symlinked snippets), so the exact CLI calls
# issued — above all, the absence of `new`/`stop` on the adopt-foreign
# path — are assertable.

import importlib.util

_spec = importlib.util.spec_from_file_location("lease_mod", REPO / "sync" / "lease.py")
lease_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lease_mod)

PNOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)


class FakeColab:
    """Stateful fake of the injected colab callable: a session set
    plus a full call log."""

    def __init__(self, alive=()):
        self.alive = set(alive)
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(a)
        cmd = a[0] if a else ""
        if cmd == "usage":
            return subprocess.CompletedProcess([], 0, "balance: 500 CU (fake)", "")
        if cmd == "sessions":
            return subprocess.CompletedProcess([], 0, json.dumps(sorted(self.alive)), "")
        if cmd == "new":
            self.alive.add(a[a.index("-s") + 1])
            return subprocess.CompletedProcess([], 0, "created", "")
        if cmd == "stop":
            self.alive.discard(a[a.index("-s") + 1])
            return subprocess.CompletedProcess([], 0, "stopped", "")
        if cmd == "download":
            return subprocess.CompletedProcess([], 1, "", "")
        return subprocess.CompletedProcess([], 0, "", "")

    def verbs(self):
        return [c[0] for c in self.calls if c]


def parallel_repo(tmp_path, monkeypatch, daily_cap=15):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "profiles.json").write_text(json.dumps({
        "default_profile": "standard",
        "profiles": {
            "standard": {"label": "Standard", "high_mem": False, "rate_cu_per_hour": 0.08, "session": "standard", "timeout": 3600},
            "workload": {"label": "High-RAM", "high_mem": True, "rate_cu_per_hour": 0.26, "session": "workload", "timeout": 18000},
        },
    }))
    (cfg_dir / "lease.json").write_text(json.dumps({
        "enabled": True, "daily_cu_budget": daily_cap, "monthly_cu_budget": 150, "max_hours": 8,
        "keepalive": {"enabled": True, "interval_seconds": 60, "pulse_seconds": 600, "stale_seconds": 600},
    }, sort_keys=True, separators=(",", ":")))
    (tmp_path / "snippets").symlink_to(REPO / "snippets")
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    return tmp_path


def test_instance_create_via_derived_profile(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)
    lease_mod.set_parallel(repo, "standard", True)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "standard2", now_dt=PNOW)
    assert out["action"] == "created"
    assert ("new", "-s", "standard2") in fake.calls
    rec = lease_mod.load_record(repo, "standard2")
    assert rec["profile"] == "standard2" and rec["session"] == "standard2"
    assert rec["deadline"] == (PNOW + timedelta(hours=8)).isoformat()
    assert (tmp_path / "var" / "lease-standard2.json").exists()


def test_instance_adopt_foreign_live_session(tmp_path, monkeypatch):
    """The workload2 path: a live session created outside the lease is
    adopted — record written, keepalive ensured, kernel pulsed — with
    no `new` and no `stop` issued against it, ever."""
    repo = parallel_repo(tmp_path, monkeypatch)
    lease_mod.set_parallel(repo, "standard", True)
    fake = FakeColab(alive={"standard2"})  # the foreign session, already running
    out = lease_mod.acquire(fake, repo, "standard2", now_dt=PNOW)
    assert out["action"] == "adopted-foreign"
    # No session is created or stopped. (A `new --help` catalog probe
    # is not a creation: it carries no -s and names no session.)
    assert not [c for c in fake.calls if c[0] == "new" and "-s" in c]
    assert "stop" not in fake.verbs()
    rec = out["record"]
    assert rec["adopted_foreign"] is True
    assert rec["session"] == "standard2"
    assert out["pulse"] == "ok"
    assert out["keepalive"] == "launched"
    assert lease_mod.load_record(repo, "standard2")["session"] == "standard2"


def test_instance_acquire_refused_when_toggle_off(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "standard2", now_dt=PNOW)
    assert out["action"] == "unknown-profile"
    assert not [c for c in fake.calls if c[0] in ("new", "stop") and "-s" in c]  # nothing created or stopped


def test_reap_covers_instances_independently(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)
    lease_mod.set_parallel(repo, "standard", True)
    fake = FakeColab(alive={"standard", "standard2"})
    for profile, deadline in (("standard", PNOW + timedelta(hours=8)), ("standard2", PNOW + timedelta(hours=8))):
        lease_mod.save_record(repo, {"profile": profile, "session": profile, "acquired_at": PNOW.isoformat(), "deadline": deadline.isoformat(), "last_used_at": PNOW.isoformat(), "last_pulse_at": PNOW.isoformat(), "jobs": []})
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert {r["profile"]: r["action"] for r in out["records"]} == {"standard": "kept", "standard2": "kept"}
    # Expire only the instance: it alone is released at its deadline.
    rec = lease_mod.load_record(repo, "standard2")
    rec["deadline"] = (PNOW - timedelta(hours=1)).isoformat()
    lease_mod.save_record(repo, rec)
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert {r["profile"]: r["action"] for r in out["records"]} == {"standard": "kept", "standard2": "released-deadline"}
    assert lease_mod.load_record(repo, "standard") is not None
    # A confirmed-dead instance session is cleared, the base untouched.
    lease_mod.save_record(repo, {"profile": "standard2", "session": "standard2", "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert {r["profile"]: r["action"] for r in out["records"]} == {"standard": "kept", "standard2": "cleared-dead"}


def test_budget_guard_prices_the_sibling_window(tmp_path, monkeypatch):
    """Daily cap 1.0: one 8 h standard window projects 0.64 and fits;
    with the sibling's identical window live, either side's acquire
    is refused — the pair is priced as the sum it commits (1.28)."""
    repo = parallel_repo(tmp_path, monkeypatch, daily_cap=1.0)
    lease_mod.set_parallel(repo, "standard", True)
    fake = FakeColab(alive={"standard"})
    lease_mod.save_record(repo, {"profile": "standard", "session": "standard", "acquired_at": PNOW.isoformat(), "deadline": (PNOW + timedelta(hours=8)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": []})
    out = lease_mod.acquire(fake, repo, "standard2", now_dt=PNOW)
    assert out["action"] == "refused-budget"
    assert out["budget"]["reason"] == "daily-budget"
    assert out["budget"]["sibling_projected"] == 0.64
    # Control: with the sibling record gone the same acquire fits.
    lease_mod.save_record(repo, None, profile="standard")
    fake2 = FakeColab()
    out = lease_mod.acquire(fake2, repo, "standard2", now_dt=PNOW)
    assert out["action"] == "created"
    # And the mirror direction: the base prices the instance's window.
    rec = lease_mod.load_record(repo, "standard2")
    assert rec["deadline"] == (PNOW + timedelta(hours=8)).isoformat()
    out = lease_mod.acquire(FakeColab(alive={"standard2"}), repo, "standard", now_dt=PNOW)
    assert out["action"] == "refused-budget"
    assert out["budget"]["sibling_projected"] == 0.64


def test_state_portions_and_composition(tmp_path, monkeypatch):
    """Progressive loading: each portion of the composed state read
    stands alone with its own keys, and state() composes them into
    exactly the payload it served before the split."""
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    lease_mod.acquire(fake, repo, "standard", now_dt=PNOW)
    lease_mod.acquire(fake, repo, "workload", now_dt=PNOW)

    records = lease_mod.state_records(fake, repo)
    assert [r["profile"] for r in records["records"]] == ["standard", "workload"]

    specs = lease_mod.state_specs(fake, repo)
    assert "specs" in specs and isinstance(specs["specs"], list)

    budget = lease_mod.state_budget(fake, repo)
    assert "budget" in budget and "balance" in budget["budget"]

    grant = lease_mod.state_grant(fake, repo)
    assert "grant" in grant and "envelope" in grant

    meta = lease_mod.state_meta(fake, repo)
    assert "bridge" in meta

    composed = lease_mod.state(fake, repo)
    # The fake's help output parses as no catalog, so the specs
    # portion's catalog_error rides the composed payload exactly as
    # it did before the split.
    assert set(composed) == {"records", "grant", "specs", "budget", "envelope", "bridge", "catalog_error"}
    assert composed["catalog_error"] == specs["catalog_error"]
    assert composed["records"] == records["records"]
    assert composed["budget"] == budget["budget"]


def test_spend_journal_keys_instance_segments_by_session(tmp_path, monkeypatch):
    """No journal change was needed: a released instance journals its
    segment under its own profile and session at the shared rate."""
    repo = parallel_repo(tmp_path, monkeypatch)
    lease_mod.set_parallel(repo, "standard", True)
    fake = FakeColab()
    lease_mod.acquire(fake, repo, "standard2", now_dt=PNOW)
    out = lease_mod.release(fake, repo, profile="standard2", now_dt=PNOW + timedelta(hours=1))
    assert out["action"] == "released"
    lines = (tmp_path / "var" / "lease-spend-journal.jsonl").read_text().strip().split("\n")
    seg = json.loads(lines[-1])
    assert seg["profile"] == "standard2" and seg["session"] == "standard2" and seg["rate"] == 0.08
