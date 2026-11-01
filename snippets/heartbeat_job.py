#!/usr/bin/env python3
"""Deterministic checkpointed job for the kill matrix and resume tests.

Writes one hashed output per unit plus a checkpoint file naming the last
completed unit. Relauching with the same --out dir skips completed units.

Usage: heartbeat_job.py --out DIR [--units 5] [--unit-seconds 60]
"""

import argparse
import hashlib
import json
import time
from pathlib import Path


def unit_payload(run_seed: str, unit: int) -> bytes:
    return hashlib.sha256(f"{run_seed}:{unit}".encode()).hexdigest().encode() * 64


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--units", type=int, default=5)
    ap.add_argument("--unit-seconds", type=float, default=60)
    ap.add_argument("--seed", default="colab-runner")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "heartbeat.log"
    done = 0
    ckpt = out / "checkpoint.json"
    if ckpt.exists():
        done = json.loads(ckpt.read_text())["completed_units"]
    with open(log, "a") as fh:
        fh.write(f"start units={args.units} resume_from={done} t={time.time():.1f}\n")
        fh.flush()
        for unit in range(done, args.units):
            time.sleep(args.unit_seconds)
            payload = unit_payload(args.seed, unit)
            (out / f"unit-{unit}.bin").write_bytes(payload)
            ckpt.write_text(json.dumps({"completed_units": unit + 1, "last_sha256": hashlib.sha256(payload).hexdigest()}))
            fh.write(f"unit {unit} done sha256={hashlib.sha256(payload).hexdigest()} t={time.time():.1f}\n")
            fh.flush()
        fh.write(f"complete t={time.time():.1f}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
