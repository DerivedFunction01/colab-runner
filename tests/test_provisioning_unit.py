"""Provisioning unit tests with a stub colab callable.

Pins the session-provisioning contract (2026-11-11): the
Chromium ensure launches once per session lifetime, a landed
verdict is folded into the session's stamp by the read path,
a stamped-ok session is never re-provisioned, and a failed
attempt retries only past its window — the ensure never
raises into an acquire.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "lease", REPO / "sync" / "lease" / "__init__.py",
    submodule_search_locations=[str(REPO / "sync" / "lease")])
lease = importlib.util.module_from_spec(spec)
sys.modules.setdefault("lease", lease)
if "provisioning" not in dir(lease):
    spec.loader.exec_module(lease)
provisioning = importlib.import_module("lease.provisioning")

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = repo_fixture.scratch_repo(tmp_path)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    return root


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


class StubColab:
    def __init__(self, verdict=None, fail_upload=False):
        self.calls = []
        self.verdict = verdict
        self.fail_upload = fail_upload

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        op = args[0]
        if op == "upload":
            if self.fail_upload:
                raise RuntimeError("channel stalled")
            return cp("uploaded")
        if op == "exec":
            return cp("provisioning launched")
        if op == "download":
            if self.verdict is None:
                return cp("", rc=1)
            Path(args[-1]).write_text(json.dumps(self.verdict))
            return cp("downloaded")
        return cp("")


def test_ensure_launches_once_then_reads_pending(repo):
    stub = StubColab()
    out = provisioning.ensure_provisioning(stub, repo, "sess-1")
    assert out == {"provisioning": "launched"}
    assert [c[0] for c in stub.calls] == ["upload", "exec"]
    out = provisioning.ensure_provisioning(stub, repo, "sess-1")
    assert out == {"provisioning": "pending"}
    assert len(stub.calls) == 2, "a second ensure inside the window launched again"


def test_stamped_ok_session_is_never_reprovisioned(repo):
    provisioning._write_stamp(repo, "sess-2", {"chromium": "ok", "checked_at": "2026-11-11T00:00:00Z"})
    stub = StubColab()
    assert provisioning.ensure_provisioning(stub, repo, "sess-2") == {"provisioning": "ok"}
    assert stub.calls == []


def test_failed_attempt_retries_only_past_its_window(repo):
    old = (datetime.now(timezone.utc) - timedelta(seconds=provisioning.RETRY_AFTER_S + 60)).isoformat()
    provisioning._write_stamp(repo, "sess-3", {"attempted_at": old, "state": "launched"})
    stub = StubColab()
    assert provisioning.ensure_provisioning(stub, repo, "sess-3") == {"provisioning": "launched"}
    assert [c[0] for c in stub.calls] == ["upload", "exec"]


def test_upload_failure_is_a_result_not_an_exception(repo):
    stub = StubColab(fail_upload=True)
    out = provisioning.ensure_provisioning(stub, repo, "sess-4")
    assert out == {"provisioning": "launch-failed", "stage": "upload"}
    assert provisioning.read_stamp(repo, "sess-4") == {}, "a failed launch stamped an attempt (it would suppress the retry)"


def test_reconcile_folds_a_landed_verdict_into_the_stamp(repo):
    provisioning._write_stamp(repo, "sess-5", {"attempted_at": datetime.now(timezone.utc).isoformat(), "state": "launched"})
    stub = StubColab(verdict={"chromium": "installed", "missing_before": ["libatk1.0-0"], "missing_after": []})
    stamp = provisioning.reconcile_provisioning(stub, repo, "sess-5")
    assert stamp["chromium"] == "installed" and stamp["state"] == "verdict"
    calls = len(stub.calls)
    stamp = provisioning.reconcile_provisioning(stub, repo, "sess-5")
    assert stamp["chromium"] == "installed" and len(stub.calls) == calls, "a terminal verdict was re-read"


def test_reconcile_without_a_verdict_keeps_the_attempt_stamp(repo):
    provisioning._write_stamp(repo, "sess-6", {"attempted_at": datetime.now(timezone.utc).isoformat(), "state": "launched"})
    stub = StubColab(verdict=None)
    stamp = provisioning.reconcile_provisioning(stub, repo, "sess-6")
    assert stamp.get("state") == "launched" and "chromium" not in stamp


def test_disabled_config_short_circuits_the_ensure(repo):
    cfg_path = repo / "config" / "lease.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["provisioning"] = {"enabled": False}
    cfg_path.write_text(json.dumps(cfg))
    stub = StubColab()
    assert provisioning.ensure_provisioning(stub, repo, "sess-7") == {"provisioning": "disabled"}
    assert stub.calls == [], "a disabled ensure touched the channel"
    assert provisioning.read_stamp(repo, "sess-7") == {}, "a disabled ensure stamped an attempt"
