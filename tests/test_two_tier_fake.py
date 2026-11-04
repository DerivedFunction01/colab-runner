"""Two-tier lease behaviour against the fake colab CLI (no login, no
network): per-profile records, the legacy-record migration, the
--hours window, the two-tier grant + standing duty, the standing
budget sub-cap, and the `state` read the UI renders.
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
    # Scratch HOME with a dummy token (see test_lease_fake.lease_env:
    # the post-start shape assertion reads the binding under HOME).
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
        # Session shape gate: the suite's test actor claim (see
        # test_lease_fake.lease_env); the standing-duty tests use
        # standing_env instead, so lobby admission rides the gate's
        # infra exemption exactly as the hook runs it.
        COLAB_LEASE_ACTOR="test",
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
    )


def standing_env(tmp_path):
    """lease_env without the test actor claim: the standing duty's
    admission is the infra exemption (actor standing + the
    configured standing profile), never a bypass."""
    env = lease_env(tmp_path)
    env.pop("COLAB_LEASE_ACTOR", None)
    return env


def run_lease(env, *args, expect_rc=0):
    r = subprocess.run([sys.executable, str(REPO / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == expect_rc, r.stdout + r.stderr
    return json.loads(r.stdout)


def var(env) -> Path:
    return Path(env["COLAB_RUNNER_VAR"])


def window_hours(rec) -> float:
    start = datetime.fromisoformat(rec["acquired_at"])
    end = datetime.fromisoformat(rec["deadline"])
    return (end - start).total_seconds() / 3600.0


def expire_grant(env) -> None:
    g = json.loads((var(env) / "grant.json").read_text())
    g["until"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (var(env) / "grant.json").write_text(json.dumps(g))


def test_legacy_record_migration(tmp_path):
    env = lease_env(tmp_path)
    var(env).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    legacy = {
        "profile": "workload",
        "session": "workload",
        "acquired_at": (now - timedelta(hours=1)).isoformat(),
        "deadline": (now + timedelta(hours=7)).isoformat(),
        "last_used_at": now.isoformat(),
        "jobs": [],
    }
    (var(env) / "lease.json").write_text(json.dumps(legacy))
    sess = Path(env["FAKE_COLAB_STATE"]) / "sessions" / "workload"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "ACTIVE").write_text("1")
    # The first post-landing reads must still see the legacy lease:
    # lease #8 was live on this path when the layout changed.
    assert run_lease(env, "status")["action"] == "active"
    assert run_lease(env, "reap")["action"] == "kept"
    adopted = run_lease(env, "acquire", "workload")
    assert adopted["action"] == "adopted"
    assert adopted["record"]["acquired_at"] == legacy["acquired_at"]
    # A save migrated the record to its per-profile path.
    assert (var(env) / "lease-workload.json").exists()
    assert not (var(env) / "lease.json").exists()


def test_acquire_hours_deadline_and_projection(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "standard", "--hours", "2")
    assert out["action"] == "created"
    assert abs(window_hours(out["record"]) - 2.0) < 0.05
    # Priced by the standard profile's own rate: 0.08 x 2h.
    assert out["record"]["budget_at_acquire"]["projected"] == 0.16


def test_acquire_hours_honored_over_max(tmp_path):
    """An explicit --hours is the operator's instruction. Until
    2026-11-01 this test pinned the opposite: acquire clamped every
    explicit window to the config max_hours, so an ordered 24 h
    endurance lease silently came up as 8 h. The window is now
    honored as given and priced in full; past the 12 h session cap
    the lease is a window lease from birth (renew flagged)."""
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "standard", "--hours", "100")
    assert out["action"] == "created"
    assert abs(window_hours(out["record"]) - 100.0) < 0.05
    assert out["record"]["budget_at_acquire"]["projected"] == 8.0
    assert out["record"]["renew"] is True


def test_acquire_default_window_unchanged(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "standard")
    assert out["action"] == "created"
    assert abs(window_hours(out["record"]) - 8.0) < 0.05
    assert out["record"]["budget_at_acquire"]["projected"] == 0.64


def test_per_profile_records_coexist(tmp_path):
    env = lease_env(tmp_path)
    assert run_lease(env, "acquire", "workload")["action"] == "created"
    assert run_lease(env, "acquire", "lobby")["action"] == "created"
    assert (var(env) / "lease-workload.json").exists()
    assert (var(env) / "lease-lobby.json").exists()
    out = run_lease(env, "reap")
    assert out["action"] == "kept"  # top level: the default profile
    assert {r["profile"] for r in out["records"]} == {"workload", "lobby"}
    assert all(r["action"] == "kept" for r in out["records"])
    # Releasing one tier leaves the other untouched.
    assert run_lease(env, "release", "lobby")["action"] == "released"
    assert not (var(env) / "lease-lobby.json").exists()
    assert (var(env) / "lease-workload.json").exists()
    assert run_lease(env, "status")["action"] == "active"


def test_grant_burst_profile_recorded(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "grant", "--hours", "2", "--burst", "standard")
    assert out["grant"]["burst_profile"] == "standard"
    run_lease(env, "ungrant")


def test_grant_then_standing_acquires_lobby(tmp_path):
    env = standing_env(tmp_path)
    assert run_lease(env, "standing")["action"] == "no-grant"
    granted = run_lease(env, "grant", "--hours", "4")
    g = granted["grant"]
    assert g["kind"] == "two-tier"
    assert g["lobby_profile"] == "lobby"
    assert g["burst_profile"] == "workload"
    out = run_lease(env, "standing")
    assert out["action"] == "standing-acquired"
    rec = json.loads((var(env) / "lease-lobby.json").read_text())
    assert rec["standing"] is True
    assert rec["warm"] == "pending"  # standing.warm_on_renew is on
    # Session window is the grant remainder (4h < the 12h session cap).
    assert abs(window_hours(rec) - 4.0) < 0.05
    assert run_lease(env, "standing")["action"] == "standing-kept"
    st = run_lease(env, "state")
    lobby = [r for r in st["records"] if r["profile"] == "lobby"]
    assert lobby and lobby[0]["warm"] == "pending"
    assert st["grant"]["kind"] == "two-tier"


def test_grant_expiry_standing_releases_lobby(tmp_path):
    env = standing_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    assert run_lease(env, "standing")["action"] == "standing-acquired"
    expire_grant(env)
    out = run_lease(env, "standing")
    assert out["action"] == "standing-expired"
    assert out["lobby"]["action"] == "released"
    assert not (var(env) / "lease-lobby.json").exists()
    assert not (var(env) / "grant.json").exists()
    assert run_lease(env, "standing")["action"] == "no-grant"
    st = run_lease(env, "state")
    assert st["grant"] is None
    assert st["records"] == []


def test_grant_expiry_reap_releases_lobby(tmp_path):
    env = standing_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    assert run_lease(env, "standing")["action"] == "standing-acquired"
    expire_grant(env)
    out = run_lease(env, "reap")
    # Sole record on disk, so its outcome is also the top-level action.
    assert out["action"] == "released-standing-expired"
    lobby = [r for r in out["records"] if r["profile"] == "lobby"]
    assert lobby and lobby[0]["action"] == "released-standing-expired"
    assert not (var(env) / "lease-lobby.json").exists()


def test_standing_budget_refusal_keeps_burst_acquirable(tmp_path):
    env = standing_env(tmp_path)
    var(env).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    # Seed attributed lobby spend at (standing cap - 0.1): a
    # journaled lobby segment covering today so far, rated to total
    # exactly that. A 4h standing window projects 0.32 more and
    # breaches the sub-cap. The cap is read from the live config —
    # it is the operator's panel setting, not a test constant.
    standing_cap = json.loads((REPO / "config" / "lease.json").read_text())["standing"]["daily_cu_cap"]
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max((now - day_start).total_seconds() / 3600.0, 1 / 60)
    seg = {"profile": "lobby", "session": "seed", "start": day_start.isoformat(), "end": now.isoformat(), "rate": (standing_cap - 0.1) / hours}
    (var(env) / "lease-spend-journal.jsonl").write_text(json.dumps(seg) + "\n")
    run_lease(env, "grant", "--hours", "4")
    out = run_lease(env, "standing", expect_rc=1)
    assert out["action"] == "standing-refused-budget"
    assert out["budget"]["reason"] == "standing-budget"
    assert not (var(env) / "lease-lobby.json").exists()
    # The burst tier never answers to the standing sub-cap.
    assert run_lease(env, "acquire", "workload")["action"] == "created"


def test_ungrant_releases_lobby(tmp_path):
    env = standing_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    assert run_lease(env, "standing")["action"] == "standing-acquired"
    out = run_lease(env, "ungrant")
    assert out["action"] == "ungranted"
    assert out["lobby"]["action"] == "released"
    assert not (var(env) / "grant.json").exists()
    assert not (var(env) / "lease-lobby.json").exists()
    assert run_lease(env, "standing")["action"] == "no-grant"


def test_extend_grant_moves_until_and_prices_lobby_delta(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    out = run_lease(env, "extend-grant", "--hours", "2")
    assert out["action"] == "grant-extended"
    delta = datetime.fromisoformat(out["new_until"]) - datetime.fromisoformat(out["old_until"])
    assert delta == timedelta(hours=2)
    # Priced on the lobby's delta only: 0.08 x 2 h.
    assert out["budget"]["projected"] == 0.16
    g = json.loads((var(env) / "grant.json").read_text())
    assert g["until"] == out["new_until"]


def test_extend_grant_without_grant_refuses(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "extend-grant", "--hours", "2", expect_rc=1)
    assert out["action"] == "grant-inactive"
    assert not (var(env) / "grant.json").exists()


def test_demand_started_burst_carries_envelope_stamps(tmp_path):
    env = lease_env(tmp_path)
    env["COLAB_RUNNER_AUTO_ACQUIRE"] = "1"
    granted = run_lease(env, "grant", "--hours", "4")
    run_lease(env, "request", "workload", "--reason", "burst work", "--via", "xbrl-thread")
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    assert out["via"] == "xbrl-thread"
    rec = json.loads((var(env) / "lease-workload.json").read_text())
    assert rec["envelope_id"] == granted["grant"]["granted_at"]
    assert rec["via"] == "xbrl-thread"


def test_demand_out_of_scope_carries_neither_stamp(tmp_path):
    env = lease_env(tmp_path)
    env["COLAB_RUNNER_AUTO_ACQUIRE"] = "1"
    run_lease(env, "grant", "--hours", "4")  # scope: lobby + workload
    run_lease(env, "request", "standard", "--reason", "outside", "--via", "xbrl-thread")
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    rec = json.loads((var(env) / "lease-standard.json").read_text())
    assert "envelope_id" not in rec
    assert "via" not in rec


def test_envelope_ledger_and_state_block(tmp_path):
    """The envelope end to end on the fake CLI: a grant records its
    scope, a demand-started burst is attributed in state and in the
    ledger, its start/release ride the reaper spool as envelope
    kinds, and ungrant closes the ledger with grant-ended."""
    env = lease_env(tmp_path)
    env["COLAB_RUNNER_AUTO_ACQUIRE"] = "1"
    granted = run_lease(env, "grant", "--hours", "4")
    run_lease(env, "request", "workload", "--reason", "burst work", "--via", "xbrl-thread")
    run_lease(env, "auto")
    st = run_lease(env, "state")
    block = st["envelope"]
    assert block["id"] == granted["grant"]["granted_at"]
    assert block["scope"]["profiles"] == ["lobby", "workload"]
    # The served caps are the grant's own snapshot, whatever the
    # live config's caps are at read time.
    assert block["caps"] == granted["grant"]["scope"]["caps"]
    assert [r["profile"] for r in block["records"]] == ["workload"]
    assert block["records"][0]["via"] == "xbrl-thread"
    spool = (var(env) / "reaper-events.jsonl").read_text()
    assert "envelope-lease-started" in spool
    assert run_lease(env, "release", "workload")["action"] == "released"
    spool = (var(env) / "reaper-events.jsonl").read_text()
    assert "envelope-lease-released" in spool
    ledger = [json.loads(line) for line in (var(env) / "grant-ledger.jsonl").read_text().splitlines() if line.strip()]
    assert [e["kind"] for e in ledger] == ["grant-started", "lease-attributed", "lease-released"]
    assert ledger[1]["envelope_id"] == granted["grant"]["granted_at"]
    assert ledger[1]["via"] == "xbrl-thread"
    run_lease(env, "ungrant")
    ledger = [json.loads(line) for line in (var(env) / "grant-ledger.jsonl").read_text().splitlines() if line.strip()]
    assert ledger[-1]["kind"] == "grant-ended"
    assert run_lease(env, "state")["envelope"] is None


def test_state_specs_and_shape(tmp_path):
    env = lease_env(tmp_path)
    st = run_lease(env, "state")
    specs = {s["id"]: s for s in st["specs"]}
    assert specs["workload"]["rate_cu_per_hour"] == 0.26
    assert specs["standard"]["rate_cu_per_hour"] == 0.08
    assert specs["lobby"]["rate_cu_per_hour"] == 0.08
    assert specs["lobby"]["session"] == "lobby"
    assert specs["t4"]["gpu"] == "T4"
    assert set(specs["workload"]) == {"id", "label", "session", "gpu", "tpu", "high_mem", "rate_cu_per_hour", "timeout", "parallel", "agent_startable"}
    # The parallel block is served ready (parallel-instances plan):
    # CPU eligible with the live toggle state (workload's is on in the
    # live config), the T4 ineligible.
    live_cfg = json.loads((REPO / "config" / "lease.json").read_text())
    assert specs["workload"]["parallel"] == {"eligible": True, "enabled": bool((live_cfg.get("parallel") or {}).get("workload")), "instance": "workload2"}
    assert specs["t4"]["parallel"]["eligible"] is False
    assert specs["workload"]["label"] == "High-RAM"
    assert st["records"] == []
    assert st["grant"] is None
    assert st["budget"]["balance"] == 500
    # The served standing cap is the live config's (the operator's
    # panel setting), read here rather than pinned to a constant.
    assert st["budget"]["caps"]["standing_daily"] == json.loads((REPO / "config" / "lease.json").read_text())["standing"]["daily_cu_cap"]
