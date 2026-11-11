"""Run the fixture gathering of a built clone on the leased VM, from a config.

Follows the colab-run harness contract (implemented once in
job_harness.py): progress goes to <out>/heartbeat.log, completion is
<out>/checkpoint.json with completed_units=1 plus a final heartbeat
line containing "complete". The built clone is resolved from
/content/env-registry.json via the harness's resolve_remote_repo.
Inputs copied to /content beforehand: the config JSON alone is edited
locally (default /content/fixture-gathering-config.json); the filing
catalog parts live at /content/fixture-gathering/catalog (transferred
once per lease). The execution plan is generated on the VM from the
config and that catalog — no plan file is uploaded.
Outputs: gathering-plan.json, fixture-candidates-8ka.json, gathering.log.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_harness

DEFAULT_CONFIG = Path("/content/fixture-gathering-config.json")
DEFAULT_CATALOG = Path("/content/fixture-gathering/catalog")


def main() -> int:
    parser = job_harness.job_parser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--catalog-dir", default=str(DEFAULT_CATALOG))
    parser.add_argument("--repo", default=os.environ.get("COLAB_REPO_ID"),
                        help="registry repo id (default: $COLAB_REPO_ID, else the sole built clone)")
    args = parser.parse_args()
    record = job_harness.resolve_remote_repo(args.repo)
    REPO = Path("/content") / record["dest"]
    venv_py = Path(record["venv_python"])
    package = record.get("package")
    if not package:
        raise SystemExit("fixture_gathering_job: the built clone's registry record carries no package name (re-run the env build with a registry entry that declares one)")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    beat = partial(job_harness.beat, out)

    def finish(rc: int) -> int:
        job_harness.write_checkpoint(out, rc)
        beat("gathering complete")
        return 0 if rc == 0 else 1

    config = Path(args.config)
    catalog = Path(args.catalog_dir)
    beat(f"starting fixture gathering from config {config}")
    if not config.is_file():
        beat(f"config file missing: {config}")
        return finish(2)
    if not catalog.is_dir() or not any(catalog.rglob("*.parquet")):
        beat(f"catalog parts missing under {catalog}")
        return finish(2)
    if not os.environ.get("SEC_USER_AGENT"):
        beat("SEC_USER_AGENT is not set in the job environment")
        return finish(2)
    env = dict(os.environ)
    env["RUNTIME_TEMP_DIRECTORY"] = str(REPO / ".artifacts" / "transient")
    env["PYTHONPATH"] = str(REPO)
    log_path = out / "gathering.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            [
                str(venv_py),
                "-m",
                f"{package}.tools.fixture_candidates",
                "gather",
                "--config",
                str(config),
                "--work-dir",
                str(out / "work"),
                "--out-dir",
                str(out),
                "--catalog-dir",
                str(catalog),
            ],
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    beat(f"gathering exited rc={proc.returncode}")
    return finish(proc.returncode)


if __name__ == "__main__":
    sys.exit(main())
