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
    # A scratch HOME with a dummy token: the post-start shape
    # assertion reads the local sessions binding under HOME, and
    # the real binding belongs to live sessions — a CLI test's
    # fresh create must never be judged against it. The empty
    # binding file also blocks the transient mirror's
    # restore-if-missing from planting the real binding here;
    # tests that exercise the assertion write their own binding.
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    (home / ".config" / "colab-cli" / "sessions.json").write_text("{}")
    return dict(
        os.environ,
        HOME=str(home),
        COLAB_BIN=str(FAKE),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        # Session shape gate: the suite's door is the whitelisted
        # test actor claim (the live config's session_gate block
        # lists it); gate-specific tests pop it to run as a plain
        # agent actor.
        COLAB_LEASE_ACTOR="test",
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
    token.parent.mkdir(parents=True, exist_ok=True)
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
    # The subject profile is the High-RAM one: renewal re-checks
    # the shape with no actor bypass (session shape gate), so a
    # Standard window lease's renewal is refused by design — that
    # refusal is covered in the gate tests below.
    env = dummy_home_env(lease_env(tmp_path), tmp_path)
    run_lease(env, "acquire", "workload", "--hours", "2")
    rec_path = Path(env["COLAB_RUNNER_VAR"]) / "lease-workload.json"
    rec = json.loads(rec_path.read_text())
    now = datetime.now(timezone.utc)
    rec["renew"] = True
    rec["acquired_at"] = (now - timedelta(hours=12)).isoformat()  # past the threshold
    rec["deadline"] = (now + timedelta(hours=12)).isoformat()
    rec_path.write_text(json.dumps(rec))
    out = run_lease(env, "reap")
    assert out["action"] == "renewed"
    assert out["driver"] == 2  # no kit in the fake world: reaper fallback
    after = json.loads(rec_path.read_text())
    assert after["session"].startswith("workload-r")
    assert after["deadline"] == rec["deadline"]  # the window end never moves
    sessions = Path(env["FAKE_COLAB_STATE"]) / "sessions"
    assert (sessions / after["session"] / "ACTIVE").exists()
    assert not (sessions / "workload" / "ACTIVE").exists()


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
        # Permissive fixture state for the session shape gate: the
        # general suite predates it; the gate's own tests below run
        # under an enforcing fixture (gate_repo).
        "session_gate": {"enabled": False},
    }, sort_keys=True, separators=(",", ":")))
    (tmp_path / "snippets").symlink_to(REPO / "snippets")
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    # A scratch HOME keeps the local sessions binding (which the
    # post-start shape assertion reads) out of these in-process
    # tests: the real binding belongs to live sessions.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
    assert set(composed) == {"records", "grant", "specs", "budget", "envelope", "bridge", "catalog_error", "session_gate", "access"}
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


def test_acquire_primes_the_initiating_scope_on_a_new_host(tmp_path):
    """Initiation-time approval, Step 2: when the acquire births a
    session on a tunnel host, bin/colab-lease primes its own scope
    with one cheap call and says so in the result."""
    env = lease_env(tmp_path)
    home = tmp_path / "home"
    sessions = home / ".config" / "colab-cli" / "sessions.json"
    sessions.parent.mkdir(parents=True, exist_ok=True)
    host = "8080-fake-a.us-east1.prod.r.colab.dev"
    sessions.write_text(json.dumps({"standard": {"url": f"https://{host}"}}))
    env["HOME"] = str(home)
    first = run_lease(env, "acquire", "standard")
    assert first["action"] == "created"
    assert first["host_changed"] is True and first["host"] == host
    assert first["prime"]["primed"] is True
    assert record(env)["host"] == host
    second = run_lease(env, "acquire", "standard")
    assert second["action"] == "adopted"
    assert "prime" not in second  # host unchanged: nothing to prime


# --- Session shape gate (plan: .plans/2026-11-04-agent-session-shape-gate.md) --
# The gate's own tests run in-process under an ENFORCING fixture
# config (gate_repo), unlike the general suite's permissive doors.
# One CLI test pins the refusal's exit code and the panel bypass
# against the live config.


def gate_repo(tmp_path, monkeypatch, gate=None):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "profiles.json").write_text(json.dumps({
        "default_profile": "workload",
        "profiles": {
            "workload": {"label": "High-RAM", "high_mem": True, "rate_cu_per_hour": 0.26, "session": "workload", "timeout": 18000},
            "standard": {"label": "Standard", "high_mem": False, "rate_cu_per_hour": 0.08, "session": "standard", "timeout": 3600},
            "lobby": {"label": "Lobby", "high_mem": False, "rate_cu_per_hour": 0.08, "session": "lobby", "timeout": 3600},
            "t4": {"label": "T4", "gpu": "T4", "high_mem": False, "rate_cu_per_hour": None, "session": "t4", "timeout": 18000},
        },
    }))
    (cfg_dir / "lease.json").write_text(json.dumps({
        "enabled": True, "daily_cu_budget": 15, "monthly_cu_budget": 150, "max_hours": 8,
        "keepalive": {"enabled": True, "interval_seconds": 60, "pulse_seconds": 600, "stale_seconds": 600},
        "standing": {"profile": "lobby", "daily_cu_cap": 4, "max_session_hours": 12},
        "session_gate": gate if gate is not None else {"enabled": True, "bypass_actors": ["panel", "test"], "agent_classes": ["high_ram"], "infra_actor": "standing"},
    }, sort_keys=True, separators=(",", ":")))
    # Caller attribution: the fixture's own scratch registry (fictional
    # thread names only — the machine's local registry never leaks in).
    (cfg_dir / "callers.json").write_text(json.dumps({"callers": ["alpha", "beta"]}))
    (tmp_path / "snippets").symlink_to(REPO / "snippets")
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COLAB_LEASE_ACTOR", raising=False)
    return tmp_path


def gate_journal(repo):
    p = Path(repo) / "var" / "session-gate-journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_gate_refuses_agent_on_standard_and_gpu(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "standard", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    assert out["shape_class"] == "standard" and out["actor"] == "agent"
    out = lease_mod.acquire(fake, repo, "t4", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    assert out["shape_class"] == "gpu"
    # No creation was ever attempted for a refused pair.
    assert not [c for c in fake.calls if c[0] == "new" and "-s" in c]
    assert lease_mod.load_record(repo, "standard") is None
    assert lease_mod.load_record(repo, "t4") is None
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-refused", "shape-refused"]
    assert [e["profile"] for e in journal] == ["standard", "t4"]
    assert all(e["seam"] == "acquire" for e in journal)


def test_gate_agent_high_ram_is_now_a_caller_start_refusal(tmp_path, monkeypatch):
    """Caller attribution reversed the old agent-creation allowance:
    an agent-class actor may no longer create even a High-RAM
    session — creation belongs to the panel, the standing duty,
    and the lease duty consuming demands. The refusal names the
    caller and the seam, attempts no creation, and leaves no record."""
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "workload", now_dt=PNOW, caller="alpha")
    assert out["action"] == "caller-start-refused"
    assert out["shape_class"] == "high_ram" and out["caller"] == "alpha"
    assert out["basis"] == "agent-creation-denied"
    assert not [c for c in fake.calls if c[0] == "new" and "-s" in c]
    assert lease_mod.load_record(repo, "workload") is None
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["caller-start-refused"]
    assert journal[0]["caller"] == "alpha" and journal[0]["seam"] == "acquire"
    # The duty exemption: the same creation on a demand passes.
    out = lease_mod.acquire(fake, repo, "workload", now_dt=PNOW, caller="alpha", duty=True)
    assert out["action"] == "created"
    assert lease_mod.load_record(repo, "workload")["caller"] == "alpha"


def test_gate_panel_bypass_needs_via_and_env(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    # The env claim alone, without the panel via, is not a bypass.
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "panel")
    out = lease_mod.acquire(fake, repo, "standard", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    # Both halves together bypass, and the bypass is journaled.
    out = lease_mod.acquire(fake, repo, "standard", now_dt=PNOW, via="panel")
    assert out["action"] == "created"
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-refused", "shape-bypass"]
    assert journal[1]["via"] == "panel" and journal[1]["env_actor"] == "panel"


def test_gate_via_panel_alone_is_not_a_bypass(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    out = lease_mod.acquire(FakeColab(), repo, "standard", now_dt=PNOW, via="panel")
    assert out["action"] == "shape-refused"


def test_gate_test_claim_bypasses_and_journals(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_LEASE_ACTOR", "test")
    out = lease_mod.acquire(FakeColab(), repo, "standard", now_dt=PNOW)
    assert out["action"] == "created"
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-bypass"]
    assert journal[0]["env_actor"] == "test"


def test_gate_standing_exemption_is_exact(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    # The infra actor admitting the configured standing profile.
    out = lease_mod.acquire(fake, repo, "lobby", now_dt=PNOW, via="standing")
    assert out["action"] == "created"
    assert [e["kind"] for e in gate_journal(repo)] == ["shape-infra"]
    # The same actor is gated like any agent on any other profile.
    out = lease_mod.acquire(fake, repo, "standard", now_dt=PNOW, via="standing")
    assert out["action"] == "shape-refused"


def test_gate_disabled_opens_everything(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch, gate={"enabled": False})
    out = lease_mod.acquire(FakeColab(), repo, "standard", now_dt=PNOW)
    assert out["action"] == "created"
    assert gate_journal(repo) == []


def test_gate_absent_block_enforces_defaults(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch, gate=None)
    # Rewrite the fixture config without the block at all: the
    # built-in defaults enforce (fail-closed).
    cfg_path = repo / "config" / "lease.json"
    cfg = json.loads(cfg_path.read_text())
    del cfg["session_gate"]
    cfg_path.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    out = lease_mod.acquire(FakeColab(), repo, "standard", now_dt=PNOW)
    assert out["action"] == "shape-refused"


def test_gate_demand_refusal_clears_and_journals(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    fake = FakeColab()
    # An attributed demand for a blocked shape is refused at write.
    out = lease_mod.write_request(repo, reason="burst", profile="standard", caller="alpha", now_dt=PNOW)
    assert out["action"] == "shape-refused"
    assert lease_mod.load_request(repo) is None
    # An unattributed demand defers to consumption, which refuses,
    # clears the demand, and journals the disposition.
    out = lease_mod.write_request(repo, reason="burst", profile="standard", now_dt=PNOW)
    assert out["action"] == "requested"
    out = lease_mod.auto_acquire(fake, repo, now_dt=PNOW)
    assert out["action"] == "skipped-shape"
    assert lease_mod.load_request(repo) is None
    assert lease_mod.load_record(repo, "standard") is None
    kinds = [e["kind"] for e in gate_journal(repo)]
    assert kinds == ["shape-refused", "shape-refused", "demand-shape-refused"]


def test_gate_renewal_refuses_standard_window(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    fake = FakeColab()  # nothing alive: both sessions read absent
    for profile in ("standard", "workload"):
        lease_mod.save_record(repo, {"profile": profile, "session": profile, "acquired_at": (PNOW - timedelta(hours=12)).isoformat(), "deadline": (PNOW + timedelta(hours=12)).isoformat(), "last_used_at": PNOW.isoformat(), "jobs": [], "renew": True})
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    actions = {r["profile"]: r["action"] for r in out["records"]}
    # The Standard window is not recreated; its record is kept and
    # the reap surfaces renewal-failed. The High-RAM window renews.
    assert actions == {"standard": "renewal-failed", "workload": "renewed"}
    assert lease_mod.load_record(repo, "standard")["session"] == "standard"
    assert lease_mod.load_record(repo, "workload")["session"].startswith("workload-r")
    journal = gate_journal(repo)
    assert [e["kind"] for e in journal] == ["shape-refused"]
    assert journal[0]["seam"] == "renewal" and journal[0]["profile"] == "standard"


def test_gate_cli_refusal_exit_code_and_panel_bypass(tmp_path):
    """Against the live config: an agent actor (no claim in env) is
    refused with exit 1 on Standard and GPU profiles; the panel's
    two halves together start the Standard profile."""
    env = lease_env(tmp_path)
    env.pop("COLAB_LEASE_ACTOR", None)

    def run(*args):
        return subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)

    r = run("acquire", "standard")
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["action"] == "shape-refused" and out["shape_class"] == "standard"
    r = run("acquire", "t4")
    assert r.returncode == 1
    assert json.loads(r.stdout)["shape_class"] == "gpu"
    assert not (Path(env["COLAB_RUNNER_VAR"]) / "lease-standard.json").exists()
    # A bare --via panel, still without the env claim, is refused.
    r = run("acquire", "standard", "--via", "panel")
    assert r.returncode == 1
    assert json.loads(r.stdout)["action"] == "shape-refused"
    # Both halves: created.
    env["COLAB_LEASE_ACTOR"] = "panel"
    r = run("acquire", "standard", "--via", "panel")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["action"] == "created"


def test_gate_post_start_shape_mismatch_stops_and_refuses(tmp_path):
    """Step 5: the binding says the fresh workload session came up on a
    Standard endpoint (m-s-) under a High-RAM profile — the acquire
    stops it, writes no lease record, and reports shape-mismatch."""
    env = lease_env(tmp_path)
    binding = Path(env["HOME"]) / ".config" / "colab-cli" / "sessions.json"
    binding.write_text(json.dumps({"workload": {"endpoint": "https://m-s-usw3b6-fake.prod.r.colab.dev"}}))
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), "acquire", "workload"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["action"] == "shape-mismatch"
    assert out["actual_class"] == "standard" and out["declared_high_mem"] is True
    assert out["stop"] == "stopped"
    assert not (Path(env["COLAB_RUNNER_VAR"]) / "lease-workload.json").exists()
    assert not (Path(env["FAKE_COLAB_STATE"]) / "sessions" / "workload" / "ACTIVE").exists()
    journal = [json.loads(line) for line in (Path(env["COLAB_RUNNER_VAR"]) / "session-gate-journal.jsonl").read_text().splitlines() if line.strip()]
    assert [e["kind"] for e in journal] == ["shape-mismatch"]
    assert journal[0]["seam"] == "acquire" and journal[0]["profile"] == "workload"
    # The agreeing binding (m-hm- for the High-RAM profile) lets
    # the next acquire through untouched.
    binding.write_text(json.dumps({"workload": {"endpoint": "https://m-hm-usw3b6-fake.prod.r.colab.dev"}}))
    out = run_lease(env, "acquire", "workload")
    assert out["action"] == "created"
    assert (Path(env["COLAB_RUNNER_VAR"]) / "lease-workload.json").exists()


# --- Caller attribution seams (plan: .plans/2026-11-04-caller-attribution.md) --
# The core stamps the caller it is given (the wrapper seams own
# validation); these tests pin the stamping, the demand carry, the
# extend fill, and the CLI's claim-time refusal.


def test_caller_stamps_created_record_and_adopt_fills(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "standard", now_dt=PNOW, caller="alpha")
    assert out["action"] == "created"
    assert lease_mod.load_record(repo, "standard")["caller"] == "alpha"
    # Adopt keeps the creator's caller even when another calls.
    out = lease_mod.acquire(fake, repo, "standard", now_dt=PNOW, caller="beta")
    assert out["action"] == "adopted"
    assert lease_mod.load_record(repo, "standard")["caller"] == "alpha"
    # A pre-attribution record gains the adopting call's caller.
    out = lease_mod.acquire(fake, repo, "workload", now_dt=PNOW)
    assert out["action"] == "created"
    assert "caller" not in lease_mod.load_record(repo, "workload")
    out = lease_mod.acquire(fake, repo, "workload", now_dt=PNOW, caller="beta")
    assert out["action"] == "adopted"
    assert lease_mod.load_record(repo, "workload")["caller"] == "beta"


def test_caller_extend_fills_only_when_absent(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    lease_mod.acquire(fake, repo, "standard", now_dt=PNOW)
    out = lease_mod.extend(fake, repo, "standard", 1, now_dt=PNOW, caller="alpha")
    assert out["action"] == "extended" and out["caller"] == "alpha"
    assert lease_mod.load_record(repo, "standard")["caller"] == "alpha"
    out = lease_mod.extend(fake, repo, "standard", 1, now_dt=PNOW, caller="beta")
    assert lease_mod.load_record(repo, "standard")["caller"] == "alpha"


def test_caller_demand_consumed_stamps_the_record(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    fake = FakeColab()
    # A High-RAM demand from a registered caller is askable (the
    # write-time check is shape-only) and the duty creates it,
    # stamping the demand's caller on the record and the result.
    out = lease_mod.write_request(repo, reason="burst", profile="workload", caller="alpha", now_dt=PNOW)
    assert out["action"] == "requested"
    assert lease_mod.load_request(repo)["caller"] == "alpha"
    out = lease_mod.auto_acquire(fake, repo, now_dt=PNOW)
    assert out["action"] == "auto-acquired" and out["caller"] == "alpha"
    assert lease_mod.load_record(repo, "workload")["caller"] == "alpha"


def test_caller_cli_claim_refusal_and_stamp(tmp_path):
    """Against the live config: an unregistered --caller is refused
    at claim time (exit 2, nothing created); a machinery caller
    claims cleanly and is stamped on the record."""
    env = lease_env(tmp_path)

    def run(*args):
        return subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)

    r = run("acquire", "standard", "--caller", "no-such-thread")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no-such-thread" in r.stderr
    assert not (Path(env["COLAB_RUNNER_VAR"]) / "lease-standard.json").exists()
    out = run_lease(env, "acquire", "standard", "--caller", "test")
    assert out["action"] == "created"
    assert record(env)["caller"] == "test"


def test_access_list_attributes_each_class(tmp_path, monkeypatch):
    """Step 5: the access list serves every live session with its
    attribution — record-backed from the record, claim-only from
    the claims journal, the rest unattributed with the detection
    stamp's first_seen."""
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"ghost", "stray"})
    lease_mod.acquire(fake, repo, "standard", now_dt=PNOW, caller="alpha")
    lease_mod.append_caller_claim(repo, "ghost", "beta", "colab-sh")
    lease_mod.save_unattributed(repo, {"stray": {"first_seen": "2026-11-05T01:02:03+00:00", "endpoint": None, "shape_class": "standard"}})
    access = lease_mod.access_list(fake, repo)
    by_session = {e["session"]: e for e in access}
    assert set(by_session) == {"standard", "ghost", "stray"}
    assert by_session["standard"]["caller"] == "alpha"
    assert by_session["standard"]["first_seen"] == lease_mod.load_record(repo, "standard")["acquired_at"]
    assert "unattributed" not in by_session["standard"]
    assert by_session["ghost"]["caller"] == "beta" and by_session["ghost"]["first_seen"]
    assert by_session["stray"]["caller"] is None and by_session["stray"]["unattributed"] is True
    assert by_session["stray"]["first_seen"] == "2026-11-05T01:02:03+00:00"
    assert by_session["stray"]["shape_class"] == "standard"


def test_access_list_unreadable_listing_serves_empty(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("list failed")

    assert lease_mod.live_sessions(boom, repo) is None
    assert lease_mod.access_list(boom, repo) == []


def test_release_session_stops_strays_and_refuses_records(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"stray"})
    lease_mod.acquire(fake, repo, "standard", now_dt=PNOW)
    lease_mod.save_unattributed(repo, {"stray": {"first_seen": PNOW.isoformat(), "endpoint": None, "shape_class": None}})
    monkeypatch.setattr(lease_mod, "stop_session_safe", lambda colab, session: "stopped")
    out = lease_mod.release_session(fake, repo, "stray")
    assert out["action"] == "session-released" and out["stop"] == "stopped"
    assert lease_mod.load_unattributed(repo) == {}
    out = lease_mod.release_session(fake, repo, "standard")
    assert out["action"] == "session-has-record"


def _spool_kinds(repo, kind):
    p = Path(repo) / "var" / "reaper-events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip() and json.loads(line).get("kind") == kind]


def test_reap_detects_unattributed_once_and_clears_on_claim(tmp_path, monkeypatch):
    """Step 6: a live session with no record and no claim is
    stamped and spooled exactly once; a later claim clears it with
    its own event. Record-backed and claimed sessions never flag."""
    repo = parallel_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"stray", "ghost"})
    lease_mod.acquire(fake, repo, "standard", now_dt=PNOW)
    lease_mod.append_caller_claim(repo, "ghost", "beta", "colab-sh")
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert out["unattributed"] == {"detected": ["stray"], "cleared": []}
    stamps = lease_mod.load_unattributed(repo)
    assert stamps["stray"]["first_seen"] == PNOW.isoformat()
    events = _spool_kinds(repo, "unattributed-session")
    assert len(events) == 1 and events[0]["session"] == "stray"
    # A second pass re-spools nothing and keeps the first_seen.
    out = lease_mod.reap(fake, repo, now_dt=PNOW + timedelta(minutes=5))
    assert out["unattributed"] == {"detected": [], "cleared": []}
    assert len(_spool_kinds(repo, "unattributed-session")) == 1
    assert lease_mod.load_unattributed(repo)["stray"]["first_seen"] == PNOW.isoformat()
    # The stray gains a claim: the next pass clears the stamp.
    lease_mod.append_caller_claim(repo, "stray", "alpha", "colab-sh")
    out = lease_mod.reap(fake, repo, now_dt=PNOW + timedelta(minutes=10))
    assert out["unattributed"] == {"detected": [], "cleared": ["stray"]}
    assert lease_mod.load_unattributed(repo) == {}
    cleared = _spool_kinds(repo, "unattributed-cleared")
    assert len(cleared) == 1 and cleared[0]["session"] == "stray"


def test_detect_unattributed_never_acts_on_an_unreadable_list(tmp_path, monkeypatch):
    repo = parallel_repo(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("list failed")

    assert lease_mod.detect_unattributed(boom, repo, PNOW) == {"detected": [], "cleared": []}
    assert lease_mod.load_unattributed(repo) == {}
