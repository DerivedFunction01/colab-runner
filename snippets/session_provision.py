#!/usr/bin/env python3
"""Session provisioning: the Chromium OS-library ensure.

A fresh Colab image carries no Chromium runtime libraries, so
every Playwright e2e leg on a new session fails at
browserType.launch with a missing shared library
(libatk-1.0.so.0 is the first) until someone provisions it by
hand — and the provisioning dies with the session, so the
failure returns at 100% on the next one (2026-11-11: 74/74
legs dead at ~6 ms each, read first as contention, then as
product; the cure was `playwright install-deps chromium`).

This payload is the runner-owned ensure: it checks the
runtime library set with the distro's package state, installs
the standard Chromium dependency set when anything is
missing, and writes its verdict as JSON to --out — the lease
machinery reads the verdict back and stamps it on the session
record, so a later launch failure can cite provisioning
instead of masquerading as a product or contention failure.

Usage: session_provision.py --out VERDICT.json
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

# The Debian package set Playwright's install-deps installs
# for Chromium (the lane-proven cure), checked by dpkg state.
PACKAGES = [
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libcups2",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libasound2",
    "libpango-1.0-0",
    "libcairo2",
    "libnss3",
    "libnspr4",
    "libxss1",
]


def missing_packages() -> list:
    missing = []
    for pkg in PACKAGES:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", pkg],
            capture_output=True, text=True,
        )
        if "install ok installed" not in r.stdout:
            missing.append(pkg)
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    verdict = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    missing = missing_packages()
    verdict["missing_before"] = missing
    if not missing:
        verdict["chromium"] = "ok"
    else:
        update = subprocess.run(
            ["apt-get", "update", "-qq"], capture_output=True, text=True, timeout=600
        )
        install = subprocess.run(
            ["apt-get", "install", "-y", "-qq", *missing],
            capture_output=True, text=True, timeout=1200,
        )
        still = missing_packages()
        verdict["missing_after"] = still
        verdict["install_rc"] = install.returncode
        if update.returncode != 0:
            verdict["update_rc"] = update.returncode
        verdict["chromium"] = "installed" if not still else "failed"
        if still:
            verdict["error"] = (install.stderr or install.stdout).strip()[-300:]
    Path(args.out).write_text(json.dumps(verdict))
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
