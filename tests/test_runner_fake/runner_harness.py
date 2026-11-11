"""Shared harness for the runner fake leaves (the split of the
former tests/test_runner_fake.py, consolidation Lane A).

Owns the fixtures import (the CLI environment builder lives in
tests/fixtures/), the deterministic control hash, the CLI
driver helpers, the wrapper factories (logging, flaky,
upload-fail), and the bare-job installer several leaves share.
Subject helpers used by a single leaf live in that leaf.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"

sys.path.insert(0, str(REPO / "tests" / "fixtures"))
from env_fixtures import runner_env  # noqa: E402, F401


def control_sha(seed: str, unit: int) -> str:
    payload = hashlib.sha256(f"{seed}:{unit}".encode()).hexdigest().encode() * 64
    return hashlib.sha256(payload).hexdigest()


def run_bin(env):
    return Path(env["TEST_REPO_ROOT"]) / "bin" / "colab-run"


def runs_dir(env):
    return Path(env["TEST_REPO_ROOT"]) / "results" / "runs"


def run_verb(env, *argv):
    return subprocess.run([sys.executable, str(run_bin(env)), *argv], capture_output=True, text=True, env=env, timeout=300)


def outbox_events(env):
    path = Path(env["COLAB_RUNNER_VAR"]) / "ui-outbox.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def terminal_events(env, run_id):
    return [e for e in outbox_events(env) if e.get("kind") == "run-terminal" and e.get("detail", {}).get("run_id") == run_id]


def logging_wrapper(tmp_path):
    """A COLAB_BIN wrapper that journals every invocation's argv
    (one JSON list per line) and delegates to the real fake."""
    wrapper = tmp_path / "wrapper_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "with (state / 'cli-calls.log').open('a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *sys.argv[1:]]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def cli_calls(env):
    log = Path(env["FAKE_COLAB_STATE"]) / "cli-calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _fire_smoke(env):
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def _write_run_record(runs, run_id, session, state="running"):
    p = runs / f"{run_id}.json"
    p.write_text(json.dumps({"run_id": run_id, "job": "smoke", "session": session, "state": state, "started_at": "2000-01-01T00:00:00+00:00"}))
    return p


def flaky_wrapper(tmp_path):
    """A COLAB_BIN wrapper with a test-controlled failure budget,
    steered by a JSON control file in the fake state dir:
    {"fail_reads": N} makes the next N reader execs exit 1;
    {"sessions_mode": "auth"} makes `sessions` answer with the
    CLI's auth prompt (the lease core's definitive-unknown read).
    Everything else delegates to the real fake."""
    wrapper = tmp_path / "flaky_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "ctl_path = state / 'flaky.json'\n"
        "ctl = json.loads(ctl_path.read_text()) if ctl_path.exists() else {}\n"
        "args = sys.argv[1:]\n"
        "if 'sessions' in args and ctl.get('sessions_mode') == 'auth':\n"
        "    print('Enter the authorization code:')\n"
        "    sys.exit(0)\n"
        "if 'exec' in args and any('read-' in a for a in args):\n"
        "    left = int(ctl.get('fail_reads', 0))\n"
        "    if left > 0:\n"
        "        ctl['fail_reads'] = left - 1\n"
        "        ctl_path.write_text(json.dumps(ctl))\n"
        "        print('flaky wrapper: reader refused', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def flaky_env(tmp_path, ctl=None):
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(flaky_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    if ctl is not None:
        state = Path(env["FAKE_COLAB_STATE"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "flaky.json").write_text(json.dumps(ctl))
    return env


def set_flaky_ctl(env, ctl):
    state = Path(env["FAKE_COLAB_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "flaky.json").write_text(json.dumps(ctl))


def _detach_smoke(env):
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke", "--detach"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def install_bare_job(env, name, exit_code, uploads=None):
    """Replace the fixture repo's jobs symlink with a real dir
    holding one checkpoint-less job: its payload writes its
    declared output and exits with the given code, speaking
    none of the runner's completion protocol (no checkpoint,
    no 'complete' line) — the shape the 2026-11-08 remainder
    gate jobs had when green runs recorded failed."""
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
        "(out / 'result.txt').write_text('bare-result\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["result.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    if uploads:
        manifest["uploads"] = uploads
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def upload_fail_wrapper(tmp_path):
    """A COLAB_BIN wrapper that fails uploads of one named file
    (ctl {"fail_upload_named": "name"} in the fake state dir),
    delegating everything else to the real fake — flaky_wrapper's
    shape, aimed at the upload verb."""
    wrapper = tmp_path / "uploadfail_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "state = Path(os.environ['FAKE_COLAB_STATE'])\n"
        "ctl_path = state / 'uploadfail.json'\n"
        "ctl = json.loads(ctl_path.read_text()) if ctl_path.exists() else {}\n"
        "args = sys.argv[1:]\n"
        "if 'upload' in args and ctl.get('fail_upload_named') and any(ctl['fail_upload_named'] in a for a in args):\n"
        "    print('uploadfail wrapper: upload refused', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "sys.exit(subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode)\n"
    )
    wrapper.chmod(0o755)
    return wrapper
