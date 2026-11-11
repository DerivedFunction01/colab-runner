#!/usr/bin/env python3
"""Kill-matrix support kit: the probe drivers' tiny in-VM helpers, in one file.

The kill matrix (probes/founding/p3_kill_matrix.py) and the low-tier OOM
probe (probes/founding/p3_oom_lowtier.py) drive a heartbeat job on the VM
through small one-purpose snippets; the four helpers live here as modes
instead of separate files:

  alive   print the session-reusable marker (the OOM probe greps stdout for it)
  reset   remove the kill-matrix job dir so each cell starts clean
  launch  start the heartbeat job detached (nohup) and print its pid
  state   print a heartbeat job's observable state as JSON

The drivers exec this file in the session kernel (colab exec -f), where
sys.argv carries the CLI's own flags, so they select the mode with
--env KM_KIT_MODE=<mode> (the state mode's job dir rides $KM_KIT_JOB_DIR).
Run as a plain process, the mode is the first positional argument:
kill_matrix_kit.py state /content/km-job.

The fold is a kit, not modes on heartbeat_job.py: that payload is
uploaded alone by name and its argv is the job contract itself, while
these helpers share the drivers' exec -f shape, not the job shape.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def alive() -> int:
    print("session-reusable")
    return 0


def reset() -> int:
    shutil.rmtree("/content/km-job", ignore_errors=True)
    print("reset")
    return 0


def launch() -> int:
    p = subprocess.Popen(
        ["nohup", "python3", "/content/heartbeat_job.py", "--out", "/content/km-job", "--units", "5", "--unit-seconds", "60", "--seed", "km"],
        stdout=open("/content/km-job.nohup.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"detached pid {p.pid}")
    return 0


def state(job_dir: str) -> int:
    d = Path(job_dir)
    out = {"dir": str(d), "exists": d.exists()}
    ckpt = d / "checkpoint.json"
    out["checkpoint"] = json.loads(ckpt.read_text()) if ckpt.exists() else None
    log = d / "heartbeat.log"
    out["heartbeat_tail"] = log.read_text().strip().split("\n")[-3:] if log.exists() else []
    out["units"] = sorted(p.name for p in d.glob("unit-*.bin")) if d.exists() else []
    r = subprocess.run(["pgrep", "-f", "heartbeat_job"], capture_output=True, text=True)
    out["heartbeat_pids"] = r.stdout.split()
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    mode = os.environ.get("KM_KIT_MODE") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if mode == "alive":
        return alive()
    if mode == "reset":
        return reset()
    if mode == "launch":
        return launch()
    if mode == "state":
        job_dir = os.environ.get("KM_KIT_JOB_DIR") or (sys.argv[2] if len(sys.argv) > 2 else "")
        if not job_dir:
            print("kill_matrix_kit: state mode needs a job dir (argument or $KM_KIT_JOB_DIR)", file=sys.stderr)
            return 2
        return state(job_dir)
    print("kill_matrix_kit: a mode is required — alive | reset | launch | state "
          "(via $KM_KIT_MODE under colab exec, else argv[1])", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
