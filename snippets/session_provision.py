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

A fresh runtime is a busy place: something else (a lane's
interim install-deps, an env build) often holds the dpkg
frontend lock in the same minutes. The apt calls therefore
wait on the lock (DPkg::Lock::Timeout) and a lock failure
retries a bounded number of times before the verdict may
call itself failed; a verdict that still fails on the lock
says so (lock_contended) instead of reading as a package
failure (specimen: ember r1794417373, 2026-11-11 — install
rc 100, frontend lock held by a concurrent apt-get, missing
set unchanged).

Usage: session_provision.py --out VERDICT.json
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

# The Debian package set Playwright's install-deps installs
# for Chromium (the lane-proven cure), as logical names —
# _alternatives resolves each to the name this distro
# actually carries (noble's t64 renames included).
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


# apt waits this long for the dpkg frontend lock before
# failing; a lock failure past the wait still retries below.
LOCK_TIMEOUT_S = 180
INSTALL_ATTEMPTS = 3
LOCK_RETRY_SLEEP_S = 20

_LOCK_MARKERS = (
    "Could not get lock",
    "Unable to acquire the dpkg frontend lock",
    "dpkg frontend lock",
)


def _is_lock_error(text: str) -> bool:
    return any(marker in (text or "") for marker in _LOCK_MARKERS)


def _alternatives(pkg: str) -> list:
    """The names a logical package may carry on this distro.
    Ubuntu noble's t64 transition renamed part of the set
    (libasound2 -> libasound2t64, libatk1.0-0 ->
    libatk1.0-0t64, ...): the plain name has no installation
    candidate there at all, and an install that names it
    aborts atomically for the whole set (specimen: ember
    r1794417373, 2026-11-11 — "Package 'libasound2' has no
    installation candidate", nothing installed). A logical
    package is satisfied by any alternative installed, and
    installs under the first alternative that has a
    candidate."""
    return [pkg, pkg + "t64"]


def _dpkg_installed(pkg: str) -> bool:
    r = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", pkg],
        capture_output=True, text=True,
    )
    return "install ok installed" in r.stdout


def missing_packages() -> list:
    return [pkg for pkg in PACKAGES if not any(_dpkg_installed(alt) for alt in _alternatives(pkg))]


def _has_candidate(pkg: str) -> bool:
    r = subprocess.run(["apt-cache", "policy", pkg], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Candidate:"):
            return line.split(":", 1)[1].strip() not in ("", "(none)")
    return False


def _install_name(pkg: str) -> str:
    for alt in _alternatives(pkg):
        if _has_candidate(alt):
            return alt
    return pkg


def _run_apt(argv: list, timeout: int):
    """One apt verb with the lock stance: apt waits on the
    dpkg lock (the -o option in argv), and a lock failure
    still retries a bounded number of times. Returns
    (result, attempts)."""
    result = None
    attempts = 0
    while attempts < INSTALL_ATTEMPTS:
        attempts += 1
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            break
        if attempts < INSTALL_ATTEMPTS and _is_lock_error(result.stderr or result.stdout):
            time.sleep(LOCK_RETRY_SLEEP_S)
            continue
        break
    return result, attempts


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
        lock_opt = f"DPkg::Lock::Timeout={LOCK_TIMEOUT_S}"
        update, update_attempts = _run_apt(
            ["apt-get", "update", "-qq", "-o", lock_opt], timeout=600)
        if update.returncode != 0:
            verdict["update_rc"] = update.returncode
            verdict["update_attempts"] = update_attempts
            verdict["update_error"] = (update.stderr or update.stdout).strip()[-300:]
        install_names = [_install_name(pkg) for pkg in missing]
        verdict["install_names"] = install_names
        install, attempts = _run_apt(
            ["apt-get", "install", "-y", "-qq", "-o", lock_opt, *install_names],
            timeout=1200,
        )
        still = missing_packages()
        verdict["missing_after"] = still
        verdict["install_rc"] = install.returncode
        verdict["install_attempts"] = attempts
        verdict["chromium"] = "installed" if not still else "failed"
        if still:
            verdict["error"] = (install.stderr or install.stdout).strip()[-300:]
            if _is_lock_error(verdict["error"]):
                verdict["lock_contended"] = True
    Path(args.out).write_text(json.dumps(verdict))
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
