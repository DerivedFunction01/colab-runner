"""Run the fixture gathering of a built clone on the leased VM, from a config.

Follows the colab-run harness contract: progress goes to
<out>/heartbeat.log, completion is <out>/checkpoint.json with
completed_units=1 plus a final heartbeat line containing "complete".
Inputs copied to /content beforehand: the config JSON alone is edited
locally (default /content/fixture-gathering-config.json); the filing
catalog parts live at /content/fixture-gathering/catalog (transferred
once per lease). The execution plan is generated on the VM from the
config and that catalog — no plan file is uploaded.
Outputs: gathering-plan.json, fixture-candidates-8ka.json, gathering.log.
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

DEFAULT_CONFIG = Path("/content/fixture-gathering-config.json")
DEFAULT_CATALOG = Path("/content/fixture-gathering/catalog")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--catalog-dir", default=str(DEFAULT_CATALOG))
    parser.add_argument("--repo", default=os.environ.get("COLAB_REPO_ID"),
                        help="registry repo id (default: $COLAB_REPO_ID, else the sole built clone)")
    args = parser.parse_args()
    record = resolve_remote_repo(args.repo)
    REPO = Path("/content") / record["dest"]
    venv_py = Path(record["venv_python"])
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    heartbeat = out / "heartbeat.log"

    def beat(message: str) -> None:
        with heartbeat.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    def finish(rc: int) -> int:
        (out / "checkpoint.json").write_text(
            json.dumps({"completed_units": 1, "rc": rc}) + "\n",
            encoding="utf-8",
        )
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
                "workload_sec.tools.fixture_candidates",
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
