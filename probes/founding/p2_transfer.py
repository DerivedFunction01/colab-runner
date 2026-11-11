#!/usr/bin/env python3
"""P2: transfer channels — upload/download timing, in-VM clone, bundle apply,
and the two regeneration calibrations (network at 8 rps; hybrid driver route).

Requires the operator's login. Payload files are deterministic (seeded) so hashes
are comparable across runs.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from _driver import colab, download, record, require_login, stop_session, upload, REPO

SESSION = "probe"
WORK = REPO / "var" / "p2"
SIZES = [1, 10, 100, 1024]  # MB


def payload(mb: int) -> Path:
    p = WORK / f"payload-{mb}mb.bin"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        block = hashlib.sha256(f"p2-{mb}".encode()).digest()
        with open(p, "wb") as fh:
            written = 0
            while written < mb * 1024 * 1024:
                fh.write(block * 4096)
                written += len(block) * 4096
            fh.truncate(mb * 1024 * 1024)
    return p


def timed(fn) -> dict:
    t0 = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t0
    return {"seconds": round(dt, 2), "rc": r.returncode, "stdout_tail": r.stdout.strip()[-200:], "stderr_tail": r.stderr.strip()[-200:]}


def main() -> int:
    require_login("p2_transfer")
    data: dict = {"transfers": []}
    try:
        colab("new", "-s", SESSION, "--high-mem", timeout=600)
        sys.path.insert(0, str(REPO / "sync"))
        import transfer as xfer

        for mb in SIZES:
            p = payload(mb)
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            if mb <= 50:  # single-file channel is proven to 50 MB; ceiling is 50-75 MB
                up = timed(lambda: upload(SESSION, p, timeout=3600))
                down_path = WORK / f"down-{mb}mb.bin"
                dn = timed(lambda: download(SESSION, p.name, down_path, timeout=3600))
                ok = down_path.exists() and hashlib.sha256(down_path.read_bytes()).hexdigest() == sha
            else:  # above the ceiling: 32 MB chunks, reassembled, hash-verified
                t0 = time.perf_counter()
                up_r = xfer.chunked_upload(SESSION, p, p.name)
                up = {"seconds": round(time.perf_counter() - t0, 2), "rc": 0 if up_r["ok"] else 1, "detail": up_r}
                down_path = WORK / f"down-{mb}mb.bin"
                t0 = time.perf_counter()
                dn_r = xfer.chunked_download(SESSION, p.name, down_path)
                dn = {"seconds": round(time.perf_counter() - t0, 2), "rc": 0 if dn_r["ok"] else 1, "detail": dn_r}
                ok = dn_r.get("sha256") == sha
            data["transfers"].append({"mb": mb, "sha256": sha, "upload": up, "download": dn, "roundtrip_hash_ok": ok})
        # exec runs Python in the kernel, so the shell script goes through a
        # generated launcher. The workload repo is private: an anonymous
        # in-VM clone is expected to fail auth, which is itself the finding
        # (base clone needs a credential story; the bundle route is primary).
        launcher = REPO / "var" / "p2-clone.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            "import subprocess\n"
            f"r = subprocess.run(['bash', 'base_clone.sh', {os.environ.get('COLAB_PROBE_CLONE_URL', 'https://example.invalid/workload.git')!r}, 'workload'], capture_output=True, text=True)\n"
            "print('rc', r.returncode)\nprint((r.stdout + r.stderr)[-600:])\n"
        )
        upload(SESSION, REPO / "sync" / "base_clone.sh", timeout=300)
        clone = timed(lambda: colab("exec", "-s", SESSION, "--timeout", "1200", "-f", str(launcher), timeout=1300))
        data["in_vm_clone"] = clone
        bundle = subprocess.run(["bash", str(REPO / "sync" / "make_snapshot_bundle.sh")], capture_output=True, text=True)
        data["bundle_build"] = {"rc": bundle.returncode, "stdout": bundle.stdout.strip(), "stderr_tail": bundle.stderr.strip()[-300:]}
        data["usage_end"] = colab("usage", timeout=120).stdout.strip()
        data["notes"] = (
            "Bundle upload/apply timing and the hybrid filing_catalog route run through "
            "sync/apply_bundle_remote.py once a bundle exists; network-regen calibration "
            "runs the bounded SEC fetch snippet on the VM with SEC_USER_AGENT passed via --env."
        )
    finally:
        stop_session(SESSION)
    record("p2", data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
