"""Two-tier lease behaviour against the fake colab CLI (no login, no
network): per-session records, the --hours window, the two-tier
grant + standing duty, the standing budget sub-cap, and the `state`
read the UI renders.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import repo_fixture  # noqa: E402
from env_fixtures import lease_env, standing_env  # noqa: E402, F401


def run_lease(env, *args, expect_rc=0):
    r = subprocess.run([sys.executable, str(Path(env["TEST_REPO_ROOT"]) / "bin" / "colab-lease"), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == expect_rc, r.stdout + r.stderr
    return json.loads(r.stdout)


def var(env) -> Path:
    return Path(env["COLAB_RUNNER_VAR"])


def record_files(env):
    return sorted(p for p in var(env).glob("lease-*.json") if p.name not in ("lease-spend.json", "lease-request.json"))


def sole_record(env):
    files = record_files(env)
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def seed_cli_catalog(env):
    var(env).mkdir(parents=True, exist_ok=True)
    (var(env) / "cli-catalog.json").write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "gpu": ["T4"], "tpu": ["v6e1"], "no_high_mem": []}))


def window_hours(rec) -> float:
    start = datetime.fromisoformat(rec["acquired_at"])
    end = datetime.fromisoformat(rec["deadline"])
    return (end - start).total_seconds() / 3600.0


def expire_grant(env) -> None:
    g = json.loads((var(env) / "grant.json").read_text())
    g["until"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (var(env) / "grant.json").write_text(json.dumps(g))


def test_acquire_hours_deadline_and_projection(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "--shape", "cpu", "--hours", "2")
    assert out["action"] == "created"
    assert abs(window_hours(out["record"]) - 2.0) < 0.05
    # Priced by the cpu shape's own rate: 0.08 x 2h.
    assert out["record"]["budget_at_acquire"]["projected"] == 0.16


def test_acquire_hours_honored_over_max(tmp_path):
    """An explicit --hours is the operator's instruction. Until
    2026-11-01 this test pinned the opposite: acquire clamped every
    explicit window to the config max_hours, so an ordered 24 h
    endurance lease silently came up as 8 h. The window is now
    honored as given and priced in full; past the 12 h session cap
    the lease is a window lease from birth (renew flagged)."""
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "--shape", "cpu", "--hours", "100")
    assert out["action"] == "created"
    assert abs(window_hours(out["record"]) - 100.0) < 0.05
    assert out["record"]["budget_at_acquire"]["projected"] == 8.0
    assert out["record"]["renew"] is True


def test_acquire_default_window_unchanged(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "acquire", "--shape", "cpu")
    assert out["action"] == "created"
    assert abs(window_hours(out["record"]) - 8.0) < 0.05
    assert out["record"]["budget_at_acquire"]["projected"] == 0.64


def test_concurrent_records_coexist(tmp_path):
    env = lease_env(tmp_path)
    burst = run_lease(env, "acquire", "--shape", "cpu-highram")
    plain = run_lease(env, "acquire", "--shape", "cpu")
    assert burst["action"] == plain["action"] == "created"
    assert len(record_files(env)) == 2
    out = run_lease(env, "reap")
    assert {r["session"] for r in out["records"]} == {burst["record"]["session"], plain["record"]["session"]}
    assert all(r["action"] == "kept" for r in out["records"])
    # Releasing one lease leaves the other untouched.
    assert run_lease(env, "release", plain["record"]["session"])["action"] == "released"
    assert len(record_files(env)) == 1
    assert sole_record(env)["session"] == burst["record"]["session"]
    assert run_lease(env, "status")["action"] == "active"


def test_grant_burst_shape_recorded(tmp_path):
    env = lease_env(tmp_path)
    out = run_lease(env, "grant", "--hours", "2", "--burst", "cpu")
    assert out["grant"]["burst_shape"] == "cpu"
    run_lease(env, "ungrant")


def test_grant_then_standing_acquires_standing_lease(tmp_path):
    env = standing_env(tmp_path)
    assert run_lease(env, "standing")["action"] == "no-grant"
    granted = run_lease(env, "grant", "--hours", "4", "--burst", "cpu-highram")
    g = granted["grant"]
    assert g["kind"] == "two-tier"
    assert g["standing_shape"] == "cpu"
    assert g["burst_shape"] == "cpu-highram"
    out = run_lease(env, "standing")
    assert out["action"] == "standing-acquired"
    rec = sole_record(env)
    assert rec["standing"] is True
    assert rec["warm"] == "pending"  # standing.warm_on_renew is on
    # Session window is the grant remainder (4h < the 12h session cap).
    assert abs(window_hours(rec) - 4.0) < 0.05
    assert run_lease(env, "standing")["action"] == "standing-kept"
    st = run_lease(env, "state")
    standing = [r for r in st["records"] if r["record"].get("standing")]
    assert standing and standing[0]["warm"] == "pending"
    assert st["grant"]["kind"] == "two-tier"


def test_grant_expiry_standing_releases_standing_lease(tmp_path):
    env = standing_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    assert run_lease(env, "standing")["action"] == "standing-acquired"
    expire_grant(env)
    out = run_lease(env, "standing")
    assert out["action"] == "standing-expired"
    assert out["standing"]["action"] == "released"
    assert record_files(env) == []
    assert not (var(env) / "grant.json").exists()
    assert run_lease(env, "standing")["action"] == "no-grant"
    st = run_lease(env, "state")
    assert st["grant"] is None
    assert st["records"] == []


def test_grant_expiry_reap_releases_standing_lease(tmp_path):
    env = standing_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    assert run_lease(env, "standing")["action"] == "standing-acquired"
    expire_grant(env)
    out = run_lease(env, "reap")
    # Sole record on disk, so its outcome is also the top-level action.
    assert out["action"] == "released-standing-expired"
    assert [r["action"] for r in out["records"]] == ["released-standing-expired"]
    assert record_files(env) == []


def test_standing_budget_refusal_keeps_burst_acquirable(tmp_path):
    env = standing_env(tmp_path)
    var(env).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    # Seed attributed standing spend at (standing cap - 0.1): a
    # journaled standing segment (caller-stamped) covering today
    # so far, rated to total exactly that. A 4h standing window projects
    # 0.32 more and breaches the sub-cap. The cap is the fixture
    # repo's authored value (tests/fixtures/repo_fixture.py).
    standing_cap = repo_fixture.FIXTURE_STANDING_CAP
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max((now - day_start).total_seconds() / 3600.0, 1 / 60)
    seg = {"caller": "standing", "session": "seed", "start": day_start.isoformat(), "end": now.isoformat(), "rate": (standing_cap - 0.1) / hours}
    (var(env) / "lease-spend-journal.jsonl").write_text(json.dumps(seg) + "\n")
    run_lease(env, "grant", "--hours", "4")
    out = run_lease(env, "standing", expect_rc=1)
    assert out["action"] == "standing-refused-budget"
    assert out["budget"]["reason"] == "standing-budget"
    assert record_files(env) == []
    # The burst tier never answers to the standing sub-cap. The
    # acquire rides the suite's test actor claim: under caller
    # attribution a claimless agent actor's fresh start is refused
    # (caller-start-refused), which is not what this test is about.
    env["COLAB_LEASE_ACTOR"] = "test"
    assert run_lease(env, "acquire", "--shape", "cpu-highram")["action"] == "created"


def test_ungrant_releases_standing_lease(tmp_path):
    env = standing_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    assert run_lease(env, "standing")["action"] == "standing-acquired"
    out = run_lease(env, "ungrant")
    assert out["action"] == "ungranted"
    assert out["standing"]["action"] == "released"
    assert not (var(env) / "grant.json").exists()
    assert record_files(env) == []
    assert run_lease(env, "standing")["action"] == "no-grant"


def test_extend_grant_moves_until_and_prices_standing_delta(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "grant", "--hours", "4")
    out = run_lease(env, "extend-grant", "--hours", "2")
    assert out["action"] == "grant-extended"
    delta = datetime.fromisoformat(out["new_until"]) - datetime.fromisoformat(out["old_until"])
    assert delta == timedelta(hours=2)
    # Priced on the standing shape's delta only: 0.08 x 2 h.
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
    granted = run_lease(env, "grant", "--hours", "4", "--burst", "cpu-highram")
    run_lease(env, "request", "--shape", "cpu-highram", "--reason", "burst work", "--caller", "test")
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    assert out["caller"] == "test"
    rec = sole_record(env)
    assert rec["envelope_id"] == granted["grant"]["granted_at"]
    assert rec["caller"] == "test"


def test_demand_out_of_scope_carries_neither_stamp(tmp_path):
    env = lease_env(tmp_path)
    env["COLAB_RUNNER_AUTO_ACQUIRE"] = "1"
    seed_cli_catalog(env)
    run_lease(env, "grant", "--hours", "4", "--burst", "cpu-highram")  # scope: cpu + cpu-highram
    run_lease(env, "request", "--shape", "t4", "--reason", "outside", "--caller", "test")
    out = run_lease(env, "auto")
    assert out["action"] == "auto-acquired"
    rec = sole_record(env)
    assert "envelope_id" not in rec
    assert "via" not in rec
    # Caller attribution is not envelope-scoped: the record carries
    # its caller even outside the envelope (via was the scoped tag).
    assert rec["caller"] == "test"


def test_envelope_ledger_and_state_block(tmp_path):
    """The envelope end to end on the fake CLI: a grant records its
    scope, a demand-started burst is attributed in state and in the
    ledger, its start/release ride the reaper spool as envelope
    kinds, and ungrant closes the ledger with grant-ended."""
    env = lease_env(tmp_path)
    env["COLAB_RUNNER_AUTO_ACQUIRE"] = "1"
    granted = run_lease(env, "grant", "--hours", "4", "--burst", "cpu-highram")
    run_lease(env, "request", "--shape", "cpu-highram", "--reason", "burst work", "--caller", "test")
    run_lease(env, "auto")
    st = run_lease(env, "state")
    block = st["envelope"]
    assert block["id"] == granted["grant"]["granted_at"]
    assert block["scope"]["shapes"] == ["cpu", "cpu-highram"]
    # The served caps are the grant's own snapshot, whatever the
    # live config's caps are at read time.
    assert block["caps"] == granted["grant"]["scope"]["caps"]
    assert [r["shape"] for r in block["records"]] == ["cpu-highram"]
    assert block["records"][0]["caller"] == "test"
    spool = (var(env) / "reaper-events.jsonl").read_text()
    assert "envelope-lease-started" in spool
    assert run_lease(env, "release", sole_record(env)["session"])["action"] == "released"
    spool = (var(env) / "reaper-events.jsonl").read_text()
    assert "envelope-lease-released" in spool
    ledger = [json.loads(line) for line in (var(env) / "grant-ledger.jsonl").read_text().splitlines() if line.strip()]
    assert [e["kind"] for e in ledger] == ["grant-started", "lease-attributed", "lease-released"]
    assert ledger[1]["envelope_id"] == granted["grant"]["granted_at"]
    assert ledger[1]["caller"] == "test"
    run_lease(env, "ungrant")
    ledger = [json.loads(line) for line in (var(env) / "grant-ledger.jsonl").read_text().splitlines() if line.strip()]
    assert ledger[-1]["kind"] == "grant-ended"
    assert run_lease(env, "state")["envelope"] is None


def test_state_specs_and_shape(tmp_path):
    env = lease_env(tmp_path)
    seed_cli_catalog(env)
    st = run_lease(env, "state")
    specs = {s["id"]: s for s in st["specs"]}
    assert specs["cpu-highram"]["rate_cu_per_hour"] == 0.26
    assert specs["cpu"]["rate_cu_per_hour"] == 0.08
    assert specs["t4"]["gpu"] == "T4"
    assert specs["t4"]["rate_cu_per_hour"] is None
    assert set(specs["cpu"]) == {"id", "label", "gpu", "tpu", "high_mem", "rate_cu_per_hour", "agent_startable"}
    assert specs["cpu-highram"]["label"] == "CPU High-RAM"
    assert specs["cpu"]["agent_startable"] is False  # an agent actor may not start plain CPU
    assert st["records"] == []
    assert st["grant"] is None
    assert st["budget"]["balance"] == 500
    # The served standing cap is the live config's (the operator's
    # panel setting), read here rather than pinned to a constant.
    assert st["budget"]["caps"]["standing_daily"] == repo_fixture.FIXTURE_STANDING_CAP
