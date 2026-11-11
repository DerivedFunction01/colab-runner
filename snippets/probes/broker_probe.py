#!/usr/bin/env python3
"""Broker probe: the in-VM half of the two-tier envelope plan's
Step 6 (beachhead broker probe). Runs inside a beachhead session
(the standing lease) and measures what a brokered burst start
costs: place the account token at its canonical path, build the
official CLI's venv (the renewal kit's measured steps, its own
directory), then mint throwaway standard sessions with the in-VM
CLI, timing each mint. The first child is left running and its
binding entry is written into the result file, so the local side
can transplant the binding and test whether a brokered session is
addressable by name from outside; later children are stopped
in-VM right after their mint timing.

This is a probe, not a mechanism: it installs no daemon, renews
nothing, and adopts nothing. The token and the sessions file are
never printed or logged — the result file carries the child's
binding entry as data for the transplant test, and the summary
names sessions and seconds only.

Standard library only. Refuses to run off a Colab VM via the
_remote_guard pattern.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/content/broker-probe")
TOKEN_CANONICAL = Path.home() / ".config" / "colab-cli" / "token.json"
SESSIONS_FILE = Path.home() / ".config" / "colab-cli" / "sessions.json"
RESULT = ROOT / "result.json"


def require_colab_vm() -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from _remote_guard import require_colab

        require_colab("broker_probe")
        return
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - sibling missing: same check, inlined
        pass
    if not (any(k.startswith("COLAB_") for k in os.environ) or Path("/content").exists()):
        print("broker_probe: REFUSED — no Colab runtime marker", file=sys.stderr)
        raise SystemExit(2)


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cli_bin() -> Path:
    return ROOT / "venv" / "bin" / "colab"


def cli(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return run([str(cli_bin()), "--auth", "oauth2", *args], timeout=timeout)


def place_token() -> bool:
    """The renewal kit's atomic placement: stage to a .tmp sibling
    and rename over the live file at mode 600."""
    staged = ROOT / "token.json"
    if not staged.exists():
        return False
    TOKEN_CANONICAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_CANONICAL.with_name(TOKEN_CANONICAL.name + ".tmp")
    shutil.copyfile(staged, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_CANONICAL)
    staged.unlink()
    return True


def build_venv() -> bool:
    """The renewal kit's measured build steps, in the probe's own
    directory: venv --without-pip, seed pip, install the CLI with
    --ignore-installed."""
    venv = ROOT / "venv"
    vpy = venv / "bin" / "python"
    if cli_bin().exists():
        return True
    try:
        if not vpy.exists():
            run(["python3", "-m", "venv", "--without-pip", str(venv)], timeout=600)
            if not vpy.exists():
                return False
        if run([str(vpy), "-m", "pip", "--version"], timeout=120).returncode != 0:
            run([str(vpy), "-m", "ensurepip", "--upgrade"], timeout=600)
        if run([str(vpy), "-m", "pip", "--version"], timeout=120).returncode != 0:
            get_pip = ROOT / "get-pip.py"
            urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", get_pip)
            run([str(vpy), str(get_pip)], timeout=600)
        r = run([str(vpy), "-m", "pip", "install", "--quiet", "--ignore-installed", "google-colab-cli"], timeout=900)
        return r.returncode == 0 and cli_bin().exists()
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    require_colab_vm()
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, default=2)
    parser.add_argument("--prefix", default="brokerprobe")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    out: dict = {"ts": datetime.now(timezone.utc).isoformat(), "children": [], "bindings": {}}
    if not place_token():
        out["error"] = "token-not-staged"
        RESULT.write_text(json.dumps(out, indent=2))
        print(json.dumps({"error": "token-not-staged"}))
        return 1
    t0 = time.monotonic()
    if not build_venv():
        out["error"] = "venv-build-failed"
        RESULT.write_text(json.dumps(out, indent=2))
        print(json.dumps({"error": "venv-build-failed"}))
        return 1
    out["venv_build_seconds"] = round(time.monotonic() - t0, 1)
    sessions = {}
    try:
        sessions = json.loads(SESSIONS_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    for i in range(args.children):
        name = f"{args.prefix}-{chr(ord('a') + i)}"
        t0 = time.monotonic()
        try:
            r = cli("new", "-s", name, timeout=600)
            rc = r.returncode
        except Exception as exc:  # noqa: BLE001
            out["children"].append({"name": name, "error": type(exc).__name__})
            continue
        seconds = round(time.monotonic() - t0, 1)
        entry = None
        try:
            entry = json.loads(SESSIONS_FILE.read_text()).get(name)
        except Exception:  # noqa: BLE001
            pass
        out["children"].append({"name": name, "mint_seconds": seconds, "rc": rc, "bound": entry is not None})
        if entry is not None:
            out["bindings"][name] = entry
        if i > 0:
            # Later children exist only for their mint timing.
            try:
                cli("stop", "-s", name, "--yes", timeout=300)
            except Exception:  # noqa: BLE001
                pass
    RESULT.write_text(json.dumps(out, indent=2))
    print(json.dumps({"venv_build_seconds": out.get("venv_build_seconds"), "children": out["children"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
