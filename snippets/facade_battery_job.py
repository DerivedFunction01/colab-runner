"""Run the facade corpus battery of a built clone on the leased VM.

Follows the colab-run harness contract: progress goes to
<out>/heartbeat.log, completion is <out>/checkpoint.json with
completed_units=1 plus a final heartbeat line containing "complete".
Outputs: battery-report.json (the tool's report), battery.log (tool output).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


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



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--cohorts", nargs="+", default=["seed", "vintage", "sp100"])
    parser.add_argument("--repo", default=os.environ.get("COLAB_REPO_ID"),
                        help="registry repo id (default: $COLAB_REPO_ID, else the sole built clone)")
    args = parser.parse_args()
    record = resolve_remote_repo(args.repo)
    REPO = Path("/content") / record["dest"]
    venv_py = Path(record["venv_python"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    heartbeat = out / "heartbeat.log"

    def beat(message: str) -> None:
        with heartbeat.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    beat(f"starting facade battery for cohorts {args.cohorts}")
    env = dict(os.environ)
    env["RUNTIME_TEMP_DIRECTORY"] = str(REPO / ".artifacts" / "transient")
    env["PYTHONPATH"] = str(REPO)
    log_path = out / "battery.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            [
                str(venv_py),
                "-m",
                "workload_sec.tools.facade_battery",
                "--cohorts",
                *args.cohorts,
                "--out",
                str(out / "battery-report.json"),
            ],
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    beat(f"battery exited rc={proc.returncode}")
    (out / "checkpoint.json").write_text(
        json.dumps({"completed_units": 1, "rc": proc.returncode}) + "\n",
        encoding="utf-8",
    )
    beat("battery complete")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
