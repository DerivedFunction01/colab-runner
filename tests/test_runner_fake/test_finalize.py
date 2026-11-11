"""Pull truthfulness and finalize accounting, against the
fake colab CLI: a download that delivers fewer bytes than
the reader sizes the remote output at is not a pull, a held
0-byte pull is named at finalize when the remote has
content, and declared outputs that never arrived are
reported as missing — never a silent short pull."""

import json
import subprocess
import sys
from pathlib import Path

from runner_harness import FAKE, _fire_smoke, run_bin, run_verb, runner_env, runs_dir  # noqa: F401


def install_partial_job(env, name):
    """A bare job (install_bare_job's shape) that writes only
    one of its two declared outputs and exits 0."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["present.txt", "absent.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def _truncating_download_wrapper(tmp_path):
    """A COLAB_BIN wrapper whose downloads report success and
    deliver an empty file — the s4diag2 manufacture (2026-11-11:
    a completed job's 1,762-byte heartbeat and 38-byte checkpoint
    were pulled as 0-byte entries and the run read complete)."""
    wrapper = tmp_path / "truncating_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "rc = subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode\n"
        "if 'download' in args and rc == 0:\n"
        "    Path(args[-1]).write_bytes(b'')\n"
        "sys.exit(rc)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def test_a_download_that_delivers_empty_is_not_a_pull(tmp_path):
    """Pull truthfulness (2026-11-11): when the channel delivers
    fewer bytes than the reader sizes the remote output at, the
    pull is refused — no entry is certified, the error names the
    mismatch, and the shortfall surfaces at finalize as missing
    or empty, never as a clean complete on 0-byte artifacts."""
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(_truncating_download_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    surfaced = set(out.get("missing_outputs", [])) | set(out.get("empty_outputs", []))
    assert "heartbeat.log" in surfaced, out
    assert "checkpoint.json" in surfaced, out
    for entry in rec.get("pulled", []):
        if entry["bytes"] == 0:
            assert entry["path"] in set(rec.get("empty_outputs", [])), f"a 0-byte pull of {entry['path']} was certified silently"
    assert "landed 0 bytes against a remote size of" in (rec.get("last_pull_error") or ""), rec.get("last_pull_error")
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert not list(results.glob("*.pulling")), "a refused pull left a staging file behind"


def test_finalize_names_a_held_empty_pull_against_remote_content(tmp_path):
    """The belt (2026-11-11): a record already holding a 0-byte
    pulled entry (landed before the size cross-check existed)
    whose remote output has content is named in empty_outputs
    at finalize — the terminal record never reads clean over a
    pulled-but-empty artifact."""
    env = runner_env(tmp_path)
    first = _fire_smoke(env)
    runs = runs_dir(env)
    first_rec = json.loads((runs / f"{first['run_id']}.json").read_text())
    seeded_id = "seeded-empty-20261111T000000Z"
    seeded = runs / f"{seeded_id}.json"
    seeded.write_text(json.dumps({
        "run_id": seeded_id,
        "job": "smoke",
        "session": first_rec["session"],
        "state": "polling",
        "started_at": "2026-11-11T00:00:00+00:00",
        "remote_out": first_rec["remote_out"],
        "pulled": [{"path": "heartbeat.log", "bytes": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "route": "single"}],
    }))
    env.update(COLAB_BIN=str(_truncating_download_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r = run_verb(env, "--poll", seeded_id)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(seeded.read_text())
    assert rec["state"] == "complete", rec
    assert rec.get("empty_outputs") == ["heartbeat.log"], rec.get("empty_outputs")
    out = json.loads(r.stdout)
    assert out.get("empty_outputs") == ["heartbeat.log"], out


def install_stray_job(env, name):
    """A bare job whose payload writes one declared output to
    its out dir and the other to its cwd root — the
    cwd-relative addressing slip behind the 2026-11-11
    missing-outputs specimens."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "Path('stray.txt').write_text('stray\\n')\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["present.txt", "stray.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_finalize_names_an_output_that_sits_at_the_cwd_root(tmp_path):
    """Misplaced naming (2026-11-11): a declared output the
    payload wrote to its cwd root instead of its out dir is
    reported missing AND named in misplaced_outputs with its
    path and size, on the record and in the result — and it is
    named, not pulled: the out-dir contract stands."""
    env = runner_env(tmp_path)
    install_stray_job(env, "stray")
    r = subprocess.run([sys.executable, str(run_bin(env)), "stray"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert out.get("missing_outputs") == ["stray.txt"], out
    misplaced = out.get("misplaced_outputs")
    assert misplaced and len(misplaced) == 1, out
    assert misplaced[0]["name"] == "stray.txt" and misplaced[0]["bytes"] == 6
    assert misplaced[0]["path"].endswith("/stray.txt")
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec.get("misplaced_outputs") == misplaced
    assert [e["path"] for e in rec["pulled"]] == ["present.txt"]
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert not (results / "stray.txt").exists()


def test_finalize_reports_declared_outputs_that_never_arrived(tmp_path):
    """Declared-vs-pulled accounting (the pull-gap): a payload
    that exits 0 without writing one of its declared outputs
    still completes — the exit code is the outcome truth — but
    the shortfall is recorded on the run and returned in the
    result, never a silent short pull."""
    env = runner_env(tmp_path)
    install_partial_job(env, "partial")
    r = subprocess.run([sys.executable, str(run_bin(env)), "partial"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert out.get("missing_outputs") == ["absent.txt"], out
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec.get("missing_outputs") == ["absent.txt"]
    assert [e["path"] for e in rec["pulled"]] == ["present.txt"]
