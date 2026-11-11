"""shell_job.py functional tests, run locally (bash + stdlib only).

The snippet's contract: a heredoc on stdin (or --script file) is
executed as a shell script — not Python — under the runner's
heartbeat contract: job.sh preserved verbatim, heartbeat.log streamed
and closed with a "complete" line, checkpoint.json ends at
completed_units 1 with the shell exit code, and the launcher's exit
code is the shell script's.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SNIPPET = REPO / "snippets" / "shell_job.py"


def run_job(tmp_path, stdin_text=None, extra=()):
    out = tmp_path / "job"
    r = subprocess.run(
        [sys.executable, str(SNIPPET), "--out", str(out), "--cwd", str(tmp_path), *extra],
        input=stdin_text, capture_output=True, text=True, timeout=60,
    )
    return r, out


def test_heredoc_runs_as_shell_and_propagates_exit_code(tmp_path):
    r, out = run_job(tmp_path, stdin_text="echo hello-from-shell\nexit 3\n")
    assert r.returncode == 3
    assert (out / "job.sh").read_text() == "echo hello-from-shell\nexit 3\n"
    log = (out / "heartbeat.log").read_text()
    assert "hello-from-shell" in log
    assert "complete exit_code=3" in log
    ck = json.loads((out / "checkpoint.json").read_text())
    assert ck == {"completed_units": 1, "exit_code": 3}


def test_heredoc_is_not_executed_as_python(tmp_path):
    # Python syntax in the payload must reach bash untouched: a shell
    # comment and a variable expansion prove the interpreter is bash.
    r, out = run_job(tmp_path, stdin_text="X=41\necho value=$((X + 1))\n")
    assert r.returncode == 0
    assert "value=42" in (out / "heartbeat.log").read_text()


def test_script_file_variant(tmp_path):
    payload = tmp_path / "payload.sh"
    payload.write_text("echo from-file\n")
    r, out = run_job(tmp_path, extra=("--script", str(payload)))
    assert r.returncode == 0
    assert "from-file" in (out / "heartbeat.log").read_text()


def test_empty_script_is_refused(tmp_path):
    r, _ = run_job(tmp_path, stdin_text="\n")
    assert r.returncode == 2
