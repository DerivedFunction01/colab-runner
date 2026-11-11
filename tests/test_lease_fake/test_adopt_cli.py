"""Orphan adoption and shorten at the CLI, against the fake
colab CLI: an orphan is a session the fake CLI created
directly, so a live session and its binding exist with no
lease record."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fake_harness import lease_env, make_orphan, plant_cli_kit_config, record, record_files, run_lease, run_lease_rc  # noqa: F401

# --- Orphan adoption at the CLI -------------------------------------------------
#
# An orphan in the fake world: a session created by invoking the
# fake colab CLI directly, so a live session and its binding
# exist with no lease record. Deadlines are relative to the real
# clock — the CLI path runs on real now, not PNOW.


def test_adopt_orphan_via_cli_restores_record(tmp_path):
    env = lease_env(tmp_path)
    make_orphan(env, "stray-cli-1")
    now = datetime.now(timezone.utc)
    plant_cli_kit_config(env, "stray-cli-1", deadline=now + timedelta(hours=6), acquired_at=now - timedelta(hours=1))
    census = run_lease(env, "state", "--part", "orphans")
    assert [o["session"] for o in census["orphans"]] == ["stray-cli-1"]
    assert census["orphans"][0]["deadline_source"] == "renewal-config"
    out = run_lease(env, "adopt", "stray-cli-1", "--caller", "alpha")
    assert out["action"] == "adopted"
    rec = record(env)
    assert rec["session"] == "stray-cli-1" and rec["shape"] == "cpu-highram"
    assert rec["deadline"] == (now + timedelta(hours=6)).isoformat()
    assert rec["acquired_at"] == (now - timedelta(hours=1)).isoformat()
    assert rec["adopted"] is True and rec["deadline_source"] == "renewal-config"
    # No creator evidence exists, so the operator's validated
    # caller fills the record (the extend fill rule).
    assert rec["caller"] == "alpha"
    # The adopted session leaves the census. (The census may
    # also list a "default" session: the fake CLI answers the
    # catalog probe (new --help) by creating one — a fake-world
    # artifact, not an adoption miss.)
    remaining = [o["session"] for o in run_lease(env, "state", "--part", "orphans")["orphans"]]
    assert "stray-cli-1" not in remaining


def test_adopt_past_deadline_via_cli_exits_1(tmp_path):
    env = lease_env(tmp_path)
    make_orphan(env, "stray-cli-2")
    now = datetime.now(timezone.utc)
    plant_cli_kit_config(env, "stray-cli-2", deadline=now - timedelta(hours=1), acquired_at=now - timedelta(hours=9))
    rc, out = run_lease_rc(env, "adopt", "stray-cli-2", "--caller", "alpha")
    assert rc == 1 and out["action"] == "adopt-deadline-past"
    assert record_files(env) == []


def test_adopt_unregistered_caller_via_cli_exits_2(tmp_path):
    env = lease_env(tmp_path)
    make_orphan(env, "stray-cli-3")
    now = datetime.now(timezone.utc)
    plant_cli_kit_config(env, "stray-cli-3", deadline=now + timedelta(hours=6), acquired_at=now - timedelta(hours=1))
    rc, _ = run_lease_rc(env, "adopt", "stray-cli-3", "--caller", "nobody")
    assert rc == 2
    assert record_files(env) == []


def test_adopt_deadline_shortens_via_cli_exits_1(tmp_path):
    env = lease_env(tmp_path)
    make_orphan(env, "stray-cli-4")
    now = datetime.now(timezone.utc)
    plant_cli_kit_config(env, "stray-cli-4", deadline=now + timedelta(hours=10), acquired_at=now - timedelta(hours=1))
    earlier = (now + timedelta(hours=9)).isoformat()
    rc, out = run_lease_rc(env, "adopt", "stray-cli-4", "--deadline", earlier, "--caller", "alpha")
    assert rc == 1 and out["action"] == "adopt-deadline-shortens"
    assert record_files(env) == []


def test_adopt_shape_ambiguity_via_cli(tmp_path):
    env = lease_env(tmp_path)
    make_orphan(env, "stray-cli-5")
    (Path(env["COLAB_RUNNER_VAR"]) / "cli-catalog.json").write_text(json.dumps({"gpu": ["T4"], "tpu": ["v5e1"], "no_high_mem": [], "fetched_at": "2027-01-01T00:00:00+00:00"}))
    deadline = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    rc, out = run_lease_rc(env, "adopt", "stray-cli-5", "--deadline", deadline, "--caller", "alpha")
    assert rc == 1 and out["action"] == "adopt-shape-ambiguous"
    assert "t4-highram" in out["shape_candidates"]
    assert record_files(env) == []
    out = run_lease(env, "adopt", "stray-cli-5", "--deadline", deadline, "--caller", "alpha", "--shape", "t4-highram")
    assert out["action"] == "adopted"
    assert record(env)["shape"] == "t4-highram"


def test_shorten_via_cli(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "--shape", "cpu", "--hours", "4")
    before = record(env)
    out = run_lease(env, "shorten", before["session"], "--hours", "2", "--caller", "alpha")
    assert out["action"] == "shortened"
    after = record(env)
    delta = datetime.fromisoformat(before["deadline"]) - datetime.fromisoformat(after["deadline"])
    assert delta == timedelta(hours=2)
    assert after["shortened_from"] == before["deadline"]
    assert after["shortened_by"] == "alpha" and "shortened_at" in after


def test_shorten_refusals_via_cli(tmp_path):
    env = lease_env(tmp_path)
    run_lease(env, "acquire", "--shape", "cpu", "--hours", "4")
    before = record(env)
    session = before["session"]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rc, out = run_lease_rc(env, "shorten", session, "--deadline", past)
    assert rc == 1 and out["action"] == "shorten-deadline-past"
    later = (datetime.fromisoformat(before["deadline"]) + timedelta(hours=1)).isoformat()
    rc, out = run_lease_rc(env, "shorten", session, "--deadline", later)
    assert rc == 1 and out["action"] == "shorten-not-earlier"
    rc, _ = run_lease_rc(env, "shorten", session)
    assert rc == 2  # exactly one target is required
    rc, _ = run_lease_rc(env, "shorten", session, "--deadline", later, "--hours", "1")
    assert rc == 2
    assert record(env)["deadline"] == before["deadline"]
