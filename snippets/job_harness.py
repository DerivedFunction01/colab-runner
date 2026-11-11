"""The runner harness contract, implemented once for the workload jobs.

Every job the runner launches speaks the same contract (see bin/colab-run):
it takes --out DIR, streams progress to <out>/heartbeat.log, records
completion in <out>/checkpoint.json with completed_units=1, and ends its
heartbeat with a line containing "complete". shell_job.py and
heartbeat_job.py are the reference implementations; the workload payloads
(facade_battery_job, facade_memory_profiler, fixture_gathering_job) take
the contract from here instead of each re-implementing it.

This module rides beside the payload that imports it: a manifest that
launches an importing payload lists snippets/job_harness.py among its
uploads, so the two land side by side in /content on the VM (and sit
side by side in snippets/ locally).

Standard library only, like every snippet.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def job_parser(**kwargs) -> argparse.ArgumentParser:
    """An argument parser carrying the contract's --out argument."""
    parser = argparse.ArgumentParser(**kwargs)
    parser.add_argument("--out", required=True)
    return parser


def beat(out_dir, message: str) -> None:
    """Append one timestamped line to <out>/heartbeat.log."""
    with (Path(out_dir) / "heartbeat.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")


def write_checkpoint(out_dir, rc: int) -> None:
    """Record completion: <out>/checkpoint.json with completed_units=1."""
    (Path(out_dir) / "checkpoint.json").write_text(
        json.dumps({"completed_units": 1, "rc": rc}) + "\n",
        encoding="utf-8",
    )


def resolve_remote_repo(repo_id=None, variant=None, content_dir="/content"):
    """Resolve a built clone's record from /content/env-registry.json.

    The env build writes one record per built clone (dest, venv_python,
    repo, variant); consumer jobs take their repo from an argument or env
    repo id, never from a hardcoded path."""
    p = Path(content_dir) / "env-registry.json"
    if not p.exists():
        raise SystemExit(f"remote registry missing: {p} (run the env build first)")
    records = json.loads(p.read_text())
    matches = [
        r for r in records.values()
        if (repo_id is None or r.get("repo") == repo_id)
        and (variant is None or r.get("variant") == variant)
    ]
    if repo_id is None and variant is None and len(records) == 1:
        return next(iter(records.values()))
    if len(matches) != 1:
        raise SystemExit(
            f"remote registry: expected one record for repo={repo_id!r} "
            f"variant={variant!r}, found {len(matches)} (dests: {sorted(records)})"
        )
    return matches[0]
