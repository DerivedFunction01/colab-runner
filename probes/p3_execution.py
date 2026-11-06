#!/usr/bin/env python3
"""P3: remote execution behaviour — dependency install cost, DuckDB smoke,
SEC reachability from Colab IPs, and the comparative check.py gate.

Requires the operator's login. The local gate leg runs only with --run-local-gate
(it is a full check.py invocation on this 2-vCPU machine).
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _driver import colab, record, require_login, stop_session, REPO

SESSION = "probe"
# The local-gate leg compares against the workload repo's own gate;
# its checkout is the operator's to name (this repo carries no
# workload path).
TARGET_REPO = Path(os.environ["COLAB_PROBE_REPO"]) if os.environ.get("COLAB_PROBE_REPO") else None
GATE_PATHS = ["tests/foundation"]  # fixed scoped path set for comparability


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-local-gate", action="store_true")
    args = ap.parse_args()
    require_login("p3_execution")
    data: dict = {}
    try:
        colab("new", "-s", SESSION, "--high-mem", timeout=600)
        t0 = time.perf_counter()
        inst = colab("install", "-s", SESSION, "duckdb", "pyarrow", "pandas", "pytest", "selectolax", timeout=1800)
        data["install"] = {"seconds": round(time.perf_counter() - t0, 2), "rc": inst.returncode, "tail": (inst.stdout + inst.stderr).strip()[-300:]}
        smoke = colab("exec", "-s", SESSION, "--timeout", "600", "-f", str(REPO / "snippets" / "smoke_duckdb.py"), timeout=700)
        data["smoke_remote"] = smoke.stdout.strip()
        local = subprocess.run([sys.executable, str(REPO / "snippets" / "smoke_duckdb.py")], capture_output=True, text=True)
        data["smoke_local"] = local.stdout.strip() or local.stderr.strip()[-200:]
        data["smoke_match"] = data["smoke_remote"].split('"total"')[-1] == data["smoke_local"].split('"total"')[-1] if local.returncode == 0 else "local duckdb unavailable"
        # Reachability only needs A declared User-Agent (SEC fair access);
        # a probe identity is used, never the operator's real UA. exec --env sets it
        # in the remote kernel directly (measured CLI capability).
        sec = colab("exec", "-s", SESSION, "--timeout", "120", "--env", "SEC_USER_AGENT=colab-runner-probe research@example.com", "-f", str(REPO / "snippets" / "sec_reachability.py"), timeout=300)
        data["sec_from_colab"] = sec.stdout.strip() or sec.stderr.strip()[-300:]
        if args.run_local_gate:
            if TARGET_REPO is None:
                data["gate_local"] = {"error": "COLAB_PROBE_REPO is not set; the local gate leg has no target repo"}
            else:
                t0 = time.perf_counter()
                gate = subprocess.run([str(TARGET_REPO / ".venv" / "bin" / "python"), "check.py", *GATE_PATHS, "--workers", "2"], cwd=TARGET_REPO, capture_output=True, text=True)
                data["gate_local"] = {"seconds": round(time.perf_counter() - t0, 2), "rc": gate.returncode, "tail": gate.stdout.strip()[-300:]}
        data["gate_remote_note"] = "Remote gate leg runs via bin/colab-check once the Step 7 bundle exists; verdicts must agree, only wall time may differ."
    finally:
        stop_session(SESSION)
    record("p3", data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
