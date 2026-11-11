#!/usr/bin/env python3
"""P3 low-tier limits: OOM behaviour on the cheap standard CPU tier.

Standard tier (~2 vCPU / ~13 GB, ~0.07 CU/h) reaches ceilings quickly and
cheaply; high-RAM is never filled just to observe an OOM. Requires the operator's
login.

Design (hardened after two harness-eaten attempts): both workloads run
DETACHED on the VM as real processes writing log files, never as kernel
cells — a kernel cell under memory pressure wedges the kernel, the exec
never returns (not even at its own --timeout), and `colab stop` hangs on
the wedged kernel. The driver polls the log files through the file
channel with short timeouts, records each leg's outcome independently,
and always writes its record.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _driver import colab, download, record, require_login, stop_session, upload, REPO

SESSION = "probe-low"


def run_detached(session: str, snippet: str, log_name: str, poll_s: int, done_marker: str) -> dict:
    """Launch `snippet` on the VM with nohup, poll its log via downloads."""
    launcher = REPO / "var" / f"oom-launch-{snippet}"
    launcher.write_text(
        "import subprocess, sys\n"
        f"p = subprocess.Popen(['python3', '/content/{snippet}'], cwd='/content', "
        f"stdout=open('/content/{log_name}', 'w'), stderr=subprocess.STDOUT, "
        "start_new_session=True, env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONPATH': '/content', "
        "'HOME': '/root', 'COLAB_RELEASE_TAG': 'detached'})\n"
        f"print('launched pid', p.pid)\n"
    )
    out: dict = {}
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=180)
        out["launch_stdout"] = r.stdout.strip()[-200:]
    except Exception as e:  # noqa: BLE001 - a failed launch is a recorded leg outcome
        out["launch_error"] = f"{type(e).__name__}: {e}"
        return out
    local_log = REPO / "var" / log_name
    lines: list[str] = []
    deadline = time.time() + poll_s
    while time.time() < deadline:
        time.sleep(20)
        try:
            download(session, f"/content/{log_name}", str(local_log), timeout=60)
            lines = local_log.read_text(errors="replace").strip().split("\n")
        except Exception:  # noqa: BLE001 - a wedged VM shows up as failed polls; keep polling
            continue
        if any(done_marker in ln for ln in lines):
            break
    out["lines"] = lines[-14:]
    out["finished"] = any(done_marker in ln for ln in lines)
    return out


def main() -> int:
    require_login("p3_oom_lowtier")
    data: dict = {}
    try:
        colab("new", "-s", SESSION, timeout=600)  # standard tier on purpose: no --high-mem
        try:
            r = colab("exec", "-s", SESSION, "-f", str(REPO / "snippets" / "probe_resources.py"), timeout=300)
            try:
                data["resources"] = json.loads(r.stdout)
            except json.JSONDecodeError:
                data["resources_stdout"] = r.stdout.strip()
        except Exception as e:  # noqa: BLE001
            data["resources_error"] = f"{type(e).__name__}: {e}"
        for snip in ("_remote_guard.py", "memory_ramp.py", "bert_batch_ramp.py"):
            upload(SESSION, REPO / "snippets" / snip, timeout=300)
        data["ramp"] = run_detached(SESSION, "memory_ramp.py", "ramp.log", poll_s=600, done_marker="survived_ramp")
        # Post-ramp survival: a fresh, short-timeout kernel call and a status read.
        try:
            alive = colab("exec", "-s", SESSION, "--env", "KM_KIT_MODE=alive", "-f", str(REPO / "snippets" / "kill_matrix_kit.py"), timeout=120)
            data["kernel_alive_after_ramp"] = "session-reusable" in alive.stdout
        except Exception as e:  # noqa: BLE001
            data["kernel_alive_after_ramp"] = False
            data["kernel_check_error"] = f"{type(e).__name__}: {e}"
        try:
            data["status_after_ramp"] = colab("status", timeout=120).stdout.strip()
        except Exception as e:  # noqa: BLE001
            data["status_after_ramp_error"] = f"{type(e).__name__}: {e}"
        data["bert"] = run_detached(SESSION, "bert_batch_ramp.py", "bert.log", poll_s=1500, done_marker="ceiling_batch")
        try:
            alive = colab("exec", "-s", SESSION, "--env", "KM_KIT_MODE=alive", "-f", str(REPO / "snippets" / "kill_matrix_kit.py"), timeout=120)
            data["session_reusable"] = "session-reusable" in alive.stdout
        except Exception as e:  # noqa: BLE001
            data["session_reusable"] = False
            data["reusable_check_error"] = f"{type(e).__name__}: {e}"
    finally:
        stop_session(SESSION)
    ramp_lines = (data.get("ramp") or {}).get("lines", [])
    bert_lines = (data.get("bert") or {}).get("lines", [])
    bert_json = {}
    for ln in reversed(bert_lines):
        if ln.strip().startswith("{"):
            try:
                bert_json = json.loads(ln)
                break
            except json.JSONDecodeError:
                continue
    data["edge_cases"] = {
        "ramp_failure_mode": (
            "python_MemoryError_caught" if any("MemoryError" in ln for ln in ramp_lines)
            else "detached_process_died (OOM-killer class)" if data.get("ramp", {}).get("finished") is False and ramp_lines and not any("survived_ramp" in ln for ln in ramp_lines)
            else "ramp_survived_to_cap" if any("survived_ramp" in ln for ln in ramp_lines)
            else "inconclusive (see ramp lines)"
        ),
        "ramp_last_allocated_gb": next((json.loads(ln)["allocated_gb"] for ln in reversed(ramp_lines) if '"allocated_gb"' in ln), None),
        "kernel_alive_after_ramp": data.get("kernel_alive_after_ramp"),
        "session_reusable_after_ramp_and_bert": data.get("session_reusable"),
        "status_reported_after_ramp": bool(data.get("status_after_ramp")),
        "bert_ceiling_batch": bert_json.get("ceiling_batch"),
    }
    record("p3-oom-lowtier", data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
