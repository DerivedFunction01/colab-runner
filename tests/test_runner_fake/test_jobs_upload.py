"""Bare (checkpoint-less) jobs and manifest uploads, against
the fake colab CLI: the exit stamp is the outcome truth for a
job outside the checkpoint protocol, a failed manifest upload
fails the run at upload time, an oversize upload moves
chunked and verified, and job scripts land under run-scoped
remote names."""

import json
import time
from pathlib import Path

from runner_harness import FAKE, cli_calls, install_bare_job, logging_wrapper, run_verb, runner_env, runs_dir, upload_fail_wrapper, upload_sleep_wrapper  # noqa: F401


def test_checkpoint_less_job_completes_on_its_exit_stamp(tmp_path):
    """D2 (2026-11-08 remainder report): a job that writes its
    outputs and exits 0 without the checkpoint protocol records
    complete — the launch wrapper's exit stamp is the outcome
    truth the death branch used to override with 'failed'."""
    env = runner_env(tmp_path)
    install_bare_job(env, "bareok", 0)
    r = run_verb(env, "bareok")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert any(p["path"] == "result.txt" for p in out["pulled"])
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "complete"


def test_checkpoint_less_job_failure_records_its_exit_code(tmp_path):
    """The same job exiting non-zero records failed with the
    payload's own code — not the generic 'exited before
    completing' the death branch used to stamp on it."""
    env = runner_env(tmp_path)
    install_bare_job(env, "barebad", 3)
    r = run_verb(env, "barebad")
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    assert "job payload exited 3" in out["error"]
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed" and "job payload exited 3" in rec["error"]


def test_manifest_upload_failure_fails_the_run_at_upload_time(tmp_path):
    """D1 (2026-11-08 remainder report): a manifest upload whose
    transfer fails ends the run AT the upload — failed record,
    the upload named — instead of launching a job that discovers
    the missing file itself (or never does)."""
    env = runner_env(tmp_path)
    install_bare_job(env, "bareup", 0, uploads=["var/payload-data.bin"])
    root = Path(env["TEST_REPO_ROOT"])
    (root / "var").mkdir(exist_ok=True)
    (root / "var" / "payload-data.bin").write_bytes(b"payload-data")
    state = Path(env["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "uploadfail.json").write_text(json.dumps({"fail_upload_named": "payload-data.bin"}))
    env.update(COLAB_BIN=str(upload_fail_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r = run_verb(env, "bareup")
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed"
    assert "manifest upload failed" in out["error"] and "payload-data.bin" in out["error"]
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["state"] == "failed" and "remote_out" not in rec


def test_small_upload_dead_channel_confesses_on_the_scaled_budget(tmp_path):
    """Size-scaled upload timeout (improvement commissioned
    2026-11-12): the runner script is a small file, so a
    channel that never answers must fail its launch on the
    small budget the size prices — not the flat 600 s the
    largest payloads carry. The wrapper sleeps the script
    upload for 60 s; with the floor shrunk to 5 s the launch
    fails in seconds, naming the upload stage and its
    timeout, exactly the named-stage record the flat budget
    produced at twelve minutes."""
    env = runner_env(tmp_path)
    install_bare_job(env, "bareslow", 0)
    state = Path(env["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "uploadsleep.json").write_text(json.dumps({"sleep_upload_named": "bareslow", "sleep_seconds": 60}))
    env.update(COLAB_BIN=str(upload_sleep_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE), COLAB_RUN_UPLOAD_FLOOR_S="5")
    started = time.monotonic()
    r = run_verb(env, "bareslow")
    elapsed = time.monotonic() - started
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed" and "upload timed out" in out["error"], out
    assert elapsed < 45, elapsed


def test_oversize_manifest_upload_moves_chunked_and_verified(tmp_path):
    """An upload over the single-file ceiling routes through
    the chunked transfer (hash-verified) and lands whole on the
    session — the ~100 MB bundle shape that silently failed
    through the plain upload."""
    env = runner_env(tmp_path)
    install_bare_job(env, "barebig", 0, uploads=["var/big.bin"])
    root = Path(env["TEST_REPO_ROOT"])
    (root / "var").mkdir(exist_ok=True)
    big = root / "var" / "big.bin"
    with open(big, "wb") as fh:
        fh.seek(51 * 1024 * 1024 - 1)
        fh.write(b"\x01")
    r = run_verb(env, "barebig")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    landed = Path(env["FAKE_COLAB_STATE"]) / "sessions" / rec["session"] / "big.bin"
    assert landed.stat().st_size == 51 * 1024 * 1024


def test_job_scripts_upload_to_run_scoped_remote_names(tmp_path):
    """Foreign-payload regression (2026-11-08 remainder lanes):
    every lane's gate script is named run.py, and the runner's
    /content/<basename> slot made runs on a shared session
    execute whichever payload landed last — one run carried a
    foreign job's label and another died on its foreign args.
    The remote script name now carries the run id: two jobs
    whose scripts share a basename land in different slots,
    neither of them the bare shared name."""
    env = runner_env(tmp_path)
    install_bare_job(env, "scopeone", 0)
    install_bare_job(env, "scopetwo", 0)
    env.update(COLAB_BIN=str(logging_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r1 = run_verb(env, "scopeone")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run_verb(env, "scopetwo")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    uploads = [c for c in cli_calls(env) if "upload" in c]
    script_dests = [c[-1] for c in uploads if c[-1].endswith("payload.py")]
    assert len(script_dests) == 2, uploads
    assert "scopeone" in script_dests[0] and "scopetwo" in script_dests[1]
    assert script_dests[0] != script_dests[1]
    assert all(d != "/content/payload.py" for d in script_dests)
