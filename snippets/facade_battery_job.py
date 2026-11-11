"""Run the facade corpus battery of a built clone on the leased VM.

Follows the colab-run harness contract (implemented once in
job_harness.py): progress goes to <out>/heartbeat.log, completion is
<out>/checkpoint.json with completed_units=1 plus a final heartbeat
line containing "complete". The built clone is resolved from
/content/env-registry.json via the harness's resolve_remote_repo.
Outputs: battery-report.json (the tool's report), battery.log (tool output).
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_harness


def main() -> int:
    parser = job_harness.job_parser()
    parser.add_argument("--cohorts", nargs="+", default=["seed", "vintage", "sp100"])
    parser.add_argument("--repo", default=os.environ.get("COLAB_REPO_ID"),
                        help="registry repo id (default: $COLAB_REPO_ID, else the sole built clone)")
    args = parser.parse_args()
    record = job_harness.resolve_remote_repo(args.repo)
    REPO = Path("/content") / record["dest"]
    venv_py = Path(record["venv_python"])
    package = record.get("package")
    if not package:
        raise SystemExit("facade_battery_job: the built clone's registry record carries no package name (re-run the env build with a registry entry that declares one)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    beat = partial(job_harness.beat, out)

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
                f"{package}.tools.facade_battery",
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
    job_harness.write_checkpoint(out, proc.returncode)
    beat("battery complete")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
