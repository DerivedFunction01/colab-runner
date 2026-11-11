"""Unit tests for the session-provisioning payload
(snippets/session_provision.py), with a scripted subprocess.

Pins the lock-tolerance contract (specimen: ember r1794417373,
2026-11-11 — the first post-landing provisioning attempt died
at install rc 100 on a dpkg frontend lock held by a concurrent
apt-get, missing set unchanged): the apt calls wait on the
lock, a lock failure retries a bounded number of times, and a
verdict that still fails on the lock names the contention
(lock_contended) instead of reading as a package failure.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
from module_loader import load_module  # noqa: E402

payload = load_module("session_provision", REPO / "snippets" / "session_provision.py")

LOCK_STDERR = (
    "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 7385 (apt-get)\n"
    "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?"
)
MISSING = ["libatk1.0-0", "libasound2"]


def cp(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class ScriptedRun:
    """A subprocess.run stand-in: dpkg-query reports the
    scripted installed set; apt-get update succeeds; apt-get
    install walks the scripted outcomes, marking the missing
    set installed on a success."""

    def __init__(self, install_outcomes):
        self.calls = []
        self.installed = set(payload.PACKAGES) - set(MISSING)
        self.install_outcomes = list(install_outcomes)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "dpkg-query":
            pkg = argv[-1]
            return cp(0, "install ok installed" if pkg in self.installed else "deinstall ok")
        if argv[:2] == ["apt-get", "update"]:
            return cp(0)
        if argv[:2] == ["apt-get", "install"]:
            outcome = self.install_outcomes.pop(0) if self.install_outcomes else cp(0)
            if outcome.returncode == 0:
                self.installed.update(MISSING)
            return outcome
        raise AssertionError(f"unexpected argv: {argv}")

    def apt_calls(self, verb):
        return [c for c in self.calls if c[:2] == ["apt-get", verb]]


def run_payload(tmp_path, monkeypatch, install_outcomes):
    fake = ScriptedRun(install_outcomes)
    sleeps = []
    monkeypatch.setattr(payload.subprocess, "run", fake)
    monkeypatch.setattr(payload.time, "sleep", sleeps.append)
    out = tmp_path / "verdict.json"
    monkeypatch.setattr(sys, "argv", ["session_provision.py", "--out", str(out)])
    assert payload.main() == 0
    return json.loads(out.read_text()), fake, sleeps


def test_lock_failure_retries_and_installs(tmp_path, monkeypatch):
    verdict, fake, sleeps = run_payload(tmp_path, monkeypatch, [cp(100, "", LOCK_STDERR), cp(0)])
    assert verdict["chromium"] == "installed"
    assert verdict["missing_before"] == MISSING
    assert verdict["missing_after"] == []
    assert verdict["install_attempts"] == 2
    assert sleeps == [payload.LOCK_RETRY_SLEEP_S]
    # Both apt verbs carry the lock wait.
    for call in fake.apt_calls("update") + fake.apt_calls("install"):
        assert f"DPkg::Lock::Timeout={payload.LOCK_TIMEOUT_S}" in call


def test_persistent_lock_failure_names_the_contention(tmp_path, monkeypatch):
    verdict, fake, sleeps = run_payload(
        tmp_path, monkeypatch, [cp(100, "", LOCK_STDERR)] * payload.INSTALL_ATTEMPTS)
    assert verdict["chromium"] == "failed"
    assert verdict["lock_contended"] is True
    assert verdict["install_attempts"] == payload.INSTALL_ATTEMPTS
    assert verdict["missing_after"] == MISSING
    assert len(sleeps) == payload.INSTALL_ATTEMPTS - 1


def test_non_lock_failure_does_not_retry(tmp_path, monkeypatch):
    verdict, fake, sleeps = run_payload(
        tmp_path, monkeypatch, [cp(100, "", "E: Unable to locate package libnope")])
    assert verdict["chromium"] == "failed"
    assert "lock_contended" not in verdict
    assert verdict["install_attempts"] == 1
    assert sleeps == []


def test_nothing_missing_never_calls_apt(tmp_path, monkeypatch):
    fake = ScriptedRun([])
    fake.installed = set(payload.PACKAGES)
    monkeypatch.setattr(payload.subprocess, "run", fake)
    out = tmp_path / "verdict.json"
    monkeypatch.setattr(sys, "argv", ["session_provision.py", "--out", str(out)])
    assert payload.main() == 0
    verdict = json.loads(out.read_text())
    assert verdict["chromium"] == "ok"
    assert fake.apt_calls("update") == [] and fake.apt_calls("install") == []
