"""probes/_driver.py sync-gate unit tests (sync-channel design,
step D): the probes' long synchronous consumers (exec / install /
upload / download) pass the lease core's sync_rights verdict
before dialing; short reads, lifecycle calls, and session-less
calls do not. The lease core is the checkout's own package; var
state is a scratch dir (COLAB_RUNNER_VAR), and the gate's session
name is unique to this file so no real run record can name it.
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_loader("probe_driver", SourceFileLoader("probe_driver", str(REPO / "probes" / "_driver.py")))
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

SESSION = "probe-gate-session-zz"


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def plant_claim(var: Path, holder="lease"):
    now = datetime.now(timezone.utc)
    claim = {
        "session": SESSION, "caller": holder,
        "claimed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=60)).isoformat(),
        "last_attach_at": now.isoformat(),
    }
    var.mkdir(parents=True, exist_ok=True)
    (var / f"sync-claim-{SESSION}.json").write_text(json.dumps(claim))


def test_exec_refused_under_a_foreign_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    plant_claim(tmp_path / "var")
    fake = FakeRun()
    monkeypatch.setattr(driver.subprocess, "run", fake)
    try:
        driver.colab("exec", "-s", SESSION, "-f", "payload.py")
        raise AssertionError("the gate should have refused")
    except SystemExit as exc:
        assert exc.code == 2
    assert fake.calls == []


def test_exec_allowed_when_the_session_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun()
    monkeypatch.setattr(driver.subprocess, "run", fake)
    out = driver.colab("exec", "-s", SESSION, "-f", "payload.py")
    assert out.returncode == 0 and len(fake.calls) == 1


def test_short_and_sessionless_calls_are_not_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    plant_claim(tmp_path / "var")
    fake = FakeRun()
    monkeypatch.setattr(driver.subprocess, "run", fake)
    driver.colab("stop", "-s", SESSION)  # lifecycle: not gated
    driver.colab("sessions")  # no session target: not gated
    driver.colab("exec", "-f", "payload.py")  # gated verb, no target: not gated
    assert len(fake.calls) == 3
