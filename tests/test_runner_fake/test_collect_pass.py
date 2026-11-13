"""The standing collector (--collect-stale): a detached run
whose consumer never polls wedges at "polling" forever over a
finished run (formation-laneb-scoped, 2026-11-13 — the gate
completed session-side in a minute and the record sat
uncollected for 36+ minutes). The pass drives stale detached
records through the same poll the verb runs; fresh records
and non-detached records are not its business."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runner_harness import _detach_smoke, _write_run_record, run_verb, runner_env, runs_dir, terminal_events


def _age_the_record(env, run_id, hours=2):
    path = runs_dir(env) / f"{run_id}.json"
    rec = json.loads(path.read_text())
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rec["collector_at"] = old
    rec["last_evidence_at"] = old
    path.write_text(json.dumps(rec))
    return rec


def _wait_session_side_done(env, session, remote_out):
    state = Path(env["FAKE_COLAB_STATE"])
    deadline = time.time() + 60
    while time.time() < deadline:
        hits = list(state.glob(f"sessions/{session}/{remote_out}/checkpoint.json"))
        if hits:
            try:
                if json.loads(hits[0].read_text()).get("completed_units", 0) >= 3:
                    return
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.5)
    raise AssertionError("the smoke payload never completed session-side")


def test_collect_stale_finalizes_a_wedged_detached_run(tmp_path):
    env = runner_env(tmp_path)
    out = _detach_smoke(env)
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    _wait_session_side_done(env, out["session"], rec["remote_out"])
    _age_the_record(env, out["run_id"])
    r = run_verb(env, "--collect-stale")
    assert r.returncode == 0, r.stdout + r.stderr
    summary = json.loads(r.stdout)
    assert summary["checked"] == 1
    assert summary["finalized"] == [out["run_id"]]
    assert summary["errors"] == []
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"
    assert rec["collector"] == "collector-sweep"
    pulled = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert (pulled / "unit-0.bin").exists(), "the pass finalized without pulling the outputs"
    assert len(terminal_events(env, out["run_id"])) == 1


def test_fresh_detached_and_stale_foreground_records_are_untouched(tmp_path):
    env = runner_env(tmp_path)
    out = _detach_smoke(env)
    runs = runs_dir(env)
    _write_run_record(runs, "fg-stale", "some-session", state="polling")
    r = run_verb(env, "--collect-stale")
    assert r.returncode == 0, r.stdout + r.stderr
    summary = json.loads(r.stdout)
    assert summary["checked"] == 0, "a fresh detached record or a stale foreground record was collected"
    assert summary["attended"] == [] and summary["finalized"] == []
    fg = json.loads((runs / "fg-stale.json").read_text())
    assert fg["state"] == "polling"
    rec = json.loads((runs / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "polling"
