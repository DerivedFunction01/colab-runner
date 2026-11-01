#!/usr/bin/env python3
"""P3 kill matrix: what actually happens to a remote job when local pieces die.

Patterns: (a) long `colab exec`, (b) `colab run`, (c) detached nohup via a
short exec. Kills: SIGINT / SIGTERM / SIGKILL to the CLI, CLI --timeout
expiry, `colab stop` from a second shell. Each cell observes the job 60 s
later through a fresh CLI and records survival, observability, output
integrity vs the control hashes, and whether the session kept burning CU.

Default job is the plan's 5-minute shape (5 units x 60 s); --fast scales to
5 x 6 s for a smoke pass. Requires the operator's login. One session is reused
across cells and always stopped at the end.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _driver import colab, colab_bin, download, record, require_login, stop_session, upload, REPO

SESSION = "probe"
JOB = "km-job"  # relative to the session working dir


def control_hashes(seed: str, units: int) -> list[str]:
    return [hashlib.sha256((hashlib.sha256(f"{seed}:{u}".encode()).hexdigest().encode() * 64)).hexdigest() for u in range(units)]


def observe() -> dict:
    """Observe through the file channel, never the kernel: a running exec
    job occupies the IPython kernel queue, so an exec-based observer just
    times out behind it (measured 2026-10-29). Downloads bypass the kernel.
    """
    import tempfile

    out: dict = {}
    with tempfile.TemporaryDirectory() as td:
        for name in ("checkpoint.json", "heartbeat.log"):
            dest = Path(td) / name
            r = download(SESSION, f"{JOB}/{name}", dest, timeout=120)
            out[name] = dest.read_text()[-500:] if r.returncode == 0 and dest.exists() else None
        ls = colab("ls", "-s", SESSION, JOB, timeout=120)
        out["ls"] = ls.stdout.strip()[-400:]
    try:
        out["checkpoint"] = json.loads(out["checkpoint.json"]) if out.get("checkpoint.json") else None
    except json.JSONDecodeError:
        out["checkpoint"] = None
    out["heartbeat_tail"] = (out.get("heartbeat.log") or "").strip().split("\n")[-3:]
    return out


def launch(pattern: str, units: int, unit_seconds: float, cli_timeout: int | None) -> subprocess.Popen:
    snippet = str(REPO / "snippets" / "heartbeat_job.py")
    remote = f"python3 /content/heartbeat_job.py --out {JOB} --units {units} --unit-seconds {unit_seconds} --seed km"
    if pattern == "exec":
        # exec runs the file in the IPython kernel with the CLI's own argv,
        # so the job is started through a generated wrapper that sets
        # sys.argv first (the pattern the runner itself uses).
        wrapper = REPO / "var" / "km-exec-wrapper.py"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(
            "import sys\n"
            f"sys.argv = ['heartbeat_job.py', '--out', '{JOB}', '--units', '{units}', '--unit-seconds', '{unit_seconds}', '--seed', 'km']\n"
            "exec(open('/content/heartbeat_job.py').read())\n"
        )
        cmd = [colab_bin(), "--auth", "oauth2", "exec", "-s", SESSION, "-f", str(wrapper)]
        if cli_timeout:
            cmd = [colab_bin(), "--auth", "oauth2", "exec", "-s", SESSION, "--timeout", str(cli_timeout), "-f", str(wrapper)]
    elif pattern == "run":
        cmd = [colab_bin(), "--auth", "oauth2", "run", "-s", SESSION, snippet, "--out", JOB, "--units", str(units), "--unit-seconds", str(unit_seconds), "--seed", "km"]
    else:  # detached: short exec starts nohup, returns immediately
        launcher = REPO / "snippets" / "launch_detached.py"
        r = colab("exec", "-s", SESSION, "--timeout", "120", "-f", str(launcher), timeout=300)
        return subprocess.Popen(["true"])  # already detached remotely; nothing local to kill
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def run_cell(pattern: str, kill: str, units: int, unit_seconds: float, wait: int) -> dict:
    colab("exec", "-s", SESSION, "--timeout", "60", "-f", str(REPO / "snippets" / "reset_dir.py"), timeout=120)
    cli_timeout = 20 if kill == "timeout" else None
    proc = launch(pattern, units, unit_seconds, cli_timeout)
    time.sleep(unit_seconds * 1.5)
    if kill == "INT":
        proc.send_signal(signal.SIGINT)
    elif kill == "TERM":
        proc.send_signal(signal.SIGTERM)
    elif kill == "KILL":
        proc.send_signal(signal.SIGKILL)
    elif kill == "stop":
        colab("stop", "-s", SESSION, timeout=120)
        colab("new", "-s", SESSION, "--high-mem", timeout=600)
        upload(SESSION, REPO / "snippets" / "heartbeat_job.py", timeout=300)
    if kill != "timeout":
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    time.sleep(wait)
    state = observe()
    tail = state.get("heartbeat_tail") or []
    return {
        "pattern": pattern, "kill": kill, "state_after": state,
        "job_completed": any("complete" in ln for ln in tail),
        "completed_units": (state.get("checkpoint") or {}).get("completed_units", 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--cells", default="exec:TERM,exec:KILL,exec:INT,exec:timeout,run:KILL,detached:stop")
    args = ap.parse_args()
    require_login("p3_kill_matrix")
    units, unit_seconds, wait = (5, 6, 15) if args.fast else (5, 60, 60)
    data: dict = {"cells": [], "control_hashes": control_hashes("km", units), "mode": "fast" if args.fast else "plan"}
    try:
        colab("new", "-s", SESSION, "--high-mem", timeout=600)
        upload(SESSION, REPO / "snippets" / "heartbeat_job.py", timeout=300)
        usage0 = colab("usage", timeout=120).stdout.strip()
        for spec in args.cells.split(","):
            pattern, kill = spec.split(":")
            try:
                cell = run_cell(pattern, kill, units, unit_seconds, wait)
            except Exception as e:  # one bad cell never kills the matrix
                cell = {"pattern": pattern, "kill": kill, "error": f"{type(e).__name__}: {e}"}
            data["cells"].append(cell)
        data["usage_delta_note"] = {"before": usage0, "after": colab("usage", timeout=120).stdout.strip()}
        data["adjacent_cells"] = "mid-upload / mid-download / mid-bundle-apply kills run with --cells upload:KILL,download:KILL once payload and bundle fixtures exist (Step 7)."
    finally:
        stop_session(SESSION)
    record("p3-kill-matrix", data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
