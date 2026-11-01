#!/usr/bin/env python3
"""Remote bootstrap probe: report the VM's Python toolchain.

Records the system python version and the uv build this repo's env
provisioning guarantees (Unit 0): uv is resolved by candidate absolute
paths ($COLAB_UV_BIN, PATH, ~/.local/bin, ~/.cargo/bin) and verified —
never invoked as a bare name, and never installed from here.

Optionally installs a requirements file, into an isolated scratch venv
created by the resolved uv (default /content/.bootstrap-venv, override
$BOOTSTRAP_VENV): the retired bare-uv `--system` route is gone, and
nothing here installs into the system python.

Usage on the VM: bootstrap_remote.py [requirements.txt]
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def resolve_uv() -> str | None:
    """First candidate that verifies via `<candidate> --version`."""
    candidates = [
        os.environ.get("COLAB_UV_BIN"),
        None,  # PATH slot, filled below without trusting it blindly
        str(Path.home() / ".local" / "bin" / "uv"),
        str(Path.home() / ".cargo" / "bin" / "uv"),
    ]
    import shutil

    candidates[1] = shutil.which("uv")
    for cand in candidates:
        if not cand:
            continue
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return cand
    return None


def main() -> int:
    out = {"python": sys.version.split()[0]}
    pip = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
    out["pip"] = (pip.stdout or pip.stderr).strip().split("\n")[0] if pip.returncode == 0 else None
    uv = resolve_uv()
    out["uv_path"] = uv
    out["uv"] = None
    if uv:
        r = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=30)
        out["uv"] = (r.stdout or r.stderr).strip().split("\n")[0] if r.returncode == 0 else None
    if len(sys.argv) > 1:
        if not uv:
            out["install"] = {"skipped": "uv unresolved; no install attempted"}
        else:
            venv = Path(os.environ.get("BOOTSTRAP_VENV", "/content/.bootstrap-venv"))
            t0 = time.perf_counter()
            subprocess.run([uv, "venv", str(venv), "--python", sys.executable],
                           capture_output=True, text=True, timeout=600)
            cmd = [uv, "pip", "install", "--python", str(venv / "bin" / "python"),
                   "-r", sys.argv[1]]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            out["install"] = {"cmd": " ".join(cmd), "venv": str(venv),
                              "seconds": round(time.perf_counter() - t0, 2),
                              "rc": r.returncode,
                              "tail": (r.stdout + r.stderr).strip().split("\n")[-3:]}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
