#!/usr/bin/env python3
"""Remote resource probe: print the shape a job would actually get.

Run on the Colab VM via `colab exec -f` or `colab run`. Stdlib only.
`derive_resources` is the budgeting logic jobs must use instead of constants
copied from the local machine: cgroup-aware memory, CPU-derived workers.
"""

import json
import os
import shutil
import sys


def cgroup_memory_max() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path).read().strip()
            if raw != "max":
                return int(raw)
        except (OSError, ValueError):
            continue
    return None


def mem_total() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return 0


def derive_resources() -> dict:
    cpus = os.cpu_count() or 1
    budget = cgroup_memory_max() or mem_total()
    workers = max(1, cpus)
    return {
        "workers": workers,
        "memory_budget_bytes": budget,
        "per_worker_bytes": budget // workers,
    }


def main() -> int:
    total, used, free = shutil.disk_usage("/")
    out = {
        "python": sys.version.split()[0],
        "python_full": sys.version,
        "cpus": os.cpu_count(),
        "mem_total_bytes": mem_total(),
        "cgroup_memory_max_bytes": cgroup_memory_max(),
        "disk": {"total": total, "free": free},
        "derive_resources": derive_resources(),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
