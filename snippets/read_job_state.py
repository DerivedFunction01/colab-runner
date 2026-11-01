#!/usr/bin/env python3
"""Print a heartbeat job's observable state: checkpoint, log tail, liveness.

Usage: read_job_state.py JOB_DIR
"""

import json
import subprocess
import sys
from pathlib import Path

d = Path(sys.argv[1])
out = {"dir": str(d), "exists": d.exists()}
ckpt = d / "checkpoint.json"
out["checkpoint"] = json.loads(ckpt.read_text()) if ckpt.exists() else None
log = d / "heartbeat.log"
out["heartbeat_tail"] = log.read_text().strip().split("\n")[-3:] if log.exists() else []
out["units"] = sorted(p.name for p in d.glob("unit-*.bin")) if d.exists() else []
r = subprocess.run(["pgrep", "-f", "heartbeat_job"], capture_output=True, text=True)
out["heartbeat_pids"] = r.stdout.split()
print(json.dumps(out, indent=2))
