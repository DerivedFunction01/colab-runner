#!/usr/bin/env python3
"""P1: authenticated session and remote shape. Requires the operator's login."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _driver import colab, record, require_login, stop_session, REPO

SESSION = "probe"


def main() -> int:
    require_login("p1_session")
    data: dict = {}
    try:
        # First live run also pins the CLI's real flag spellings for exec/run.
        data["help"] = colab("--help", timeout=60).stdout.strip()
        data["exec_help"] = colab("exec", "--help", timeout=60).stdout.strip()
        data["usage_before"] = colab("usage", timeout=120).stdout.strip()
        data["new"] = colab("new", "-s", SESSION, "--high-mem", timeout=600).stdout.strip()
        data["status"] = colab("status", timeout=120).stdout.strip()
        data["sessions"] = colab("sessions", timeout=120).stdout.strip()
        r = colab("exec", "-s", SESSION, "-f", str(REPO / "snippets" / "probe_resources.py"), timeout=300)
        data["resources_stdout"] = r.stdout.strip()
        try:
            data["resources"] = json.loads(r.stdout)
        except json.JSONDecodeError:
            data["resources"] = None
        try:
            dm = colab("drivemount", "-s", SESSION, timeout=180)
            data["drivemount"] = {"rc": dm.returncode, "tail": (dm.stdout + dm.stderr).strip()[-300:]}
        except Exception as e:  # drivemount hangs headless (issue #113): a timeout IS the finding
            data["drivemount"] = {"rc": "timeout", "tail": f"{type(e).__name__}: drivemount did not return within 180 s on a headless session"}
        data["usage_after"] = colab("usage", timeout=120).stdout.strip()
    finally:
        stop_session(SESSION)
    res = data.get("resources") or {}
    dr = res.get("derive_resources") or {}
    data["verdict"] = {
        "python": res.get("python"),
        "cpus": res.get("cpus"),
        "memory_gb": round((res.get("mem_total_bytes") or 0) / 1e9, 1),
        "high_mem_shape_ok": (res.get("cpus") or 0) >= 8 and (res.get("mem_total_bytes") or 0) >= 45e9,
        "derived_workers": dr.get("workers"),
    }
    record("p1", data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
