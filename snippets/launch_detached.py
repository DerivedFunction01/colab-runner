#!/usr/bin/env python3
"""Start the heartbeat job detached (nohup) on the VM and return at once."""

import subprocess

p = subprocess.Popen(
    ["nohup", "python3", "/content/heartbeat_job.py", "--out", "/content/km-job", "--units", "5", "--unit-seconds", "60", "--seed", "km"],
    stdout=open("/content/km-job.nohup.log", "w"),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(f"detached pid {p.pid}")
