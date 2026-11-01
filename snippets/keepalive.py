#!/usr/bin/env python3
"""Lease keepalive: a detached loop that keeps a leased Colab runtime
visibly busy so the idle timeout does not reap the session mid-lease
(the operator's ruling, 2026-10-30 — the first away lease died at ~3h, idle).

Each cycle does a short CPU touch and appends a heartbeat JSON line to
keepalive.log in the working directory; the lease reaper reads that file
through the file channel and relaunches the loop if it goes stale.
Single-instance via a pidfile. Runs only under the lease launcher (which
sets COLAB_RELEASE_TAG) or with KEEPALIVE_ALLOW_NONCOLAB=1 (tests).

Env: KEEPALIVE_MAX_CYCLES (0 = forever), KEEPALIVE_INTERVAL_SECONDS (60).
"""

import json
import os
import sys
import time
from pathlib import Path

LOG = Path("keepalive.log")
PID = Path("keepalive.pid")


def claim_pidfile() -> None:
    try:
        fd = os.open(PID, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            other = int(PID.read_text().strip())
            os.kill(other, 0)
            print(f"keepalive already running (pid {other})")
            sys.exit(0)
        except Exception:  # noqa: BLE001 - stale pidfile: take over
            PID.unlink(missing_ok=True)
            claim_pidfile()


def main() -> int:
    if not (os.environ.get("COLAB_RELEASE_TAG") or os.environ.get("KEEPALIVE_ALLOW_NONCOLAB")):
        print("keepalive: refusing to run outside the lease launcher (no Colab marker)", file=sys.stderr)
        return 2
    claim_pidfile()
    max_cycles = int(os.environ.get("KEEPALIVE_MAX_CYCLES", "0"))
    interval = float(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "60"))
    n = 0
    while True:
        n += 1
        touch = 0
        for i in range(200_000):
            touch += i * i
        with LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "cycle": n, "touch": touch % 97}) + "\n")
        if LOG.stat().st_size > 1_000_000:  # keep the log tail-sized for cheap downloads
            lines = LOG.read_text().strip().split("\n")[-50:]
            LOG.write_text("\n".join(lines) + "\n")
        if max_cycles and n >= max_cycles:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
