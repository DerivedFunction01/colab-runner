"""The checkpoint exit_code as a terminal leg in both poll
paths (specimens commissioned 2026-11-12): gate payloads that
write their checkpoint as their last act while their process
lingers — a booted server holding the process open, so the
wrapper's exit stamp never lands and no 'complete' line is
ever spoken — read "polling" forever even though finalize
has always treated the checkpoint's exit_code as the
authoritative outcome. Three live specimens sat at
last_checkpoint_units 1 with exit_code 0 and 92 written.
The payload shape below is theirs exactly: checkpoint with
an exit_code, then sleep."""

import json
import time
from pathlib import Path

from runner_harness import run_verb, runner_env, runs_dir


def install_checkpoint_linger_job(env, name, exit_code):
    """A job whose payload writes its checkpoint (carrying
    the given exit_code) into its out dir, then sleeps — the
    process stays alive, no wrapper stamp ever lands."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, json, time\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        f"(out / 'checkpoint.json').write_text(json.dumps({{'completed_units': 1, 'exit_code': {exit_code}}}))\n"
        "(out / 'result.txt').write_text('linger-result\\n')\n"
        "time.sleep(120)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["result.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_poll_finalizes_failed_on_a_lingering_checkpoint_exit_code(tmp_path):
    """Detached specimen (exit 92's shape): one --poll sees
    the checkpoint's exit_code and finalizes the run failed
    with the payload's code, process still alive."""
    env = runner_env(tmp_path)
    install_checkpoint_linger_job(env, "lingerbad", 7)
    r = run_verb(env, "lingerbad", "--detach")
    assert r.returncode == 0, r.stdout + r.stderr
    run_id = json.loads(r.stdout)["run_id"]
    p = run_verb(env, "--poll", run_id)
    assert p.returncode == 1, p.stdout + p.stderr
    rec = json.loads((runs_dir(env) / f"{run_id}.json").read_text())
    assert rec["state"] == "failed" and "job payload exited 7" in rec["error"], rec


def test_poll_finalizes_complete_on_a_lingering_checkpoint_zero(tmp_path):
    """The exit-0 specimen: a green gate whose checkpoint was
    its last act stayed 'polling' after successful polls; the
    leg completes it on the first poll that reads the code."""
    env = runner_env(tmp_path)
    install_checkpoint_linger_job(env, "lingerok", 0)
    r = run_verb(env, "lingerok", "--detach")
    assert r.returncode == 0, r.stdout + r.stderr
    run_id = json.loads(r.stdout)["run_id"]
    p = run_verb(env, "--poll", run_id)
    assert p.returncode == 0, p.stdout + p.stderr
    rec = json.loads((runs_dir(env) / f"{run_id}.json").read_text())
    assert rec["state"] == "complete", rec


def test_foreground_loop_breaks_on_the_checkpoint_exit_code(tmp_path):
    """The foreground poll loop carries the same leg: the run
    finalizes seconds after the checkpoint lands instead of
    waiting out the lingering process (or the timeout)."""
    env = runner_env(tmp_path)
    install_checkpoint_linger_job(env, "lingerfg", 7)
    started = time.monotonic()
    r = run_verb(env, "lingerfg")
    elapsed = time.monotonic() - started
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "failed" and "job payload exited 7" in out["error"], out
    assert elapsed < 60, elapsed
