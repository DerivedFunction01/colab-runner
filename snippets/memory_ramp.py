#!/usr/bin/env python3
"""Synthetic memory ramp: allocate in 256 MB steps until the ceiling.

Prints one JSON line per step (allocated GB, peak RSS GB), then either a
MemoryError record or a survived marker. Used by the low-tier OOM probe.
"""

import json
import os
import resource
import sys
from pathlib import Path

try:
    _here = str(Path(__file__).parent)
except NameError:  # under `colab exec`, __file__ is undefined (IPython cell)
    _here = os.getcwd()
sys.path.insert(0, _here)
from _remote_guard import require_colab

_forced_local = require_colab("memory_ramp")

chunks: list[bytearray] = []
STEP = 256 * 1024 * 1024
CAP = 512 * 1024**2 if _forced_local else 64 * 1024**3
try:
    while sum(len(c) for c in chunks) < CAP:
        chunks.append(bytearray(STEP))
        print(json.dumps({
            "allocated_gb": round(sum(len(c) for c in chunks) / 1e9, 2),
            "rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9, 2),
        }), flush=True)
except MemoryError:
    print(json.dumps({"died": "MemoryError", "allocated_gb": round(sum(len(c) for c in chunks) / 1e9, 2)}), flush=True)
print(json.dumps({"survived_ramp": True}), flush=True)
