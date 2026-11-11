"""Runtime-identity unit tests with a stub colab callable.

Pins the runtime-identity contract (2026-11-11): a live
lease's runtime identity (host endpoint + boot identity) is
captured at first registration into a side-file stamp, the
reap cycle's check baselines a stamp-less lease silently,
an unreadable probe is unknown (never a verdict), and a
confirmed replacement re-stamps — spooling a
runtime-replaced event only when registered jobs or
non-terminal run records name the session, so a recycle
under live work surfaces instead of healing silently.
"""

import importlib
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
if "identity" not in dir(lease):
    spec.loader.exec_module(lease)
identity = importlib.import_module("lease.identity")
foundation = importlib.import_module("lease.foundation")

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = repo_fixture.scratch_repo(tmp_path)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


class ProbeColab:
    """A stub whose identity-probe exec answers scripted boot
    facts; every other verb answers empty success."""

    def __init__(self, boot_id="boot-1", uptime_s=3600.0, fail=False):
        self.boot_id = boot_id
        self.uptime_s = uptime_s
        self.fail = fail
        self.execs = 0

    def __call__(self, *args, **kwargs):
        if args and args[0] == "exec":
            self.execs += 1
            if self.fail:
                raise RuntimeError("channel down")
            return cp(json.dumps({"boot_id": self.boot_id, "uptime_s": self.uptime_s}))
        return cp("")


def _rec(session="sess-1", jobs=(), caller="panel"):
    return {"session": session, "caller": caller, "shape": "cpu-highram", "jobs": list(jobs)}


def _plant_run(repo, run_id, session, state="running"):
    runs = repo / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "job": "smoke", "session": session, "state": state, "started_at": "2026-11-11T00:00:00+00:00"}))


def _reaper_events(repo):
    path = foundation.reaper_events_path(repo)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- Capture (ensure) ----------------------------------------------------------


def test_ensure_captures_once_then_reads_present(repo):
    stub = ProbeColab()
    assert identity.ensure_identity(stub, repo, "sess-1", "acquire") == {"identity": "captured"}
    stamp = identity.read_stamp(repo, "sess-1")
    assert stamp["boot_id"] == "boot-1" and stamp["source"] == "acquire"
    assert stamp["boot_epoch"] is not None
    stub.boot_id = "boot-2"
    assert identity.ensure_identity(stub, repo, "sess-1", "acquire") == {"identity": "present"}
    assert identity.read_stamp(repo, "sess-1")["boot_id"] == "boot-1", "a capture overwrote the standing baseline"
    assert stub.execs == 1


def test_ensure_unreadable_probe_stamps_nothing(repo):
    stub = ProbeColab(fail=True)
    assert identity.ensure_identity(stub, repo, "sess-1") == {"identity": "unreadable"}
    assert identity.read_stamp(repo, "sess-1") == {}


# --- Check (the reap cycle's comparison) ---------------------------------------


def test_check_baselines_a_stamp_less_lease_silently(repo):
    stub = ProbeColab()
    out = identity.check_identity(stub, repo, _rec())
    assert out == {"identity": "baselined"}
    assert identity.read_stamp(repo, "sess-1")["boot_id"] == "boot-1"
    assert _reaper_events(repo) == [], "a baseline spooled an event"


def test_check_ok_when_the_boot_id_matches(repo):
    stub = ProbeColab()
    identity.ensure_identity(stub, repo, "sess-1")
    assert identity.check_identity(stub, repo, _rec()) == {"identity": "ok"}
    assert _reaper_events(repo) == []


def test_check_unreadable_probe_is_unknown_and_stamps_nothing(repo):
    stub = ProbeColab()
    identity.ensure_identity(stub, repo, "sess-1")
    before = identity.read_stamp(repo, "sess-1")
    stub.fail = True
    assert identity.check_identity(stub, repo, _rec()) == {"identity": "unknown"}
    assert identity.read_stamp(repo, "sess-1") == before


def test_replacement_with_work_in_flight_spools_the_event(repo):
    stub = ProbeColab()
    identity.ensure_identity(stub, repo, "sess-1")
    _plant_run(repo, "run-reg", "sess-1")
    _plant_run(repo, "run-loose", "sess-1")
    _plant_run(repo, "run-done", "sess-1", state="complete")
    stub.boot_id = "boot-2"
    out = identity.check_identity(stub, repo, _rec(jobs=["run-reg"]))
    assert out["identity"] == "replaced" and out["event"] is True
    assert out["jobs"] == ["run-reg"], "the registered job was not named"
    assert out["runs"] == ["run-loose"], "the unregistered non-terminal run was not named (the terminal one must not be)"
    events = [e for e in _reaper_events(repo) if e["kind"] == "runtime-replaced"]
    assert len(events) == 1
    assert events[0]["session"] == "sess-1" and events[0]["previous"]["boot_id"] == "boot-1"
    assert events[0]["current"]["boot_id"] == "boot-2"
    stamp = identity.read_stamp(repo, "sess-1")
    assert stamp["boot_id"] == "boot-2" and stamp["previous"]["boot_id"] == "boot-1"
    assert stamp["replaced_at"]
    # The re-stamp is the new baseline: the next check is quiet.
    assert identity.check_identity(stub, repo, _rec(jobs=["run-reg"])) == {"identity": "ok"}


def test_replacement_with_nothing_in_flight_restamps_quietly(repo):
    stub = ProbeColab()
    identity.ensure_identity(stub, repo, "sess-1")
    stub.boot_id = "boot-2"
    out = identity.check_identity(stub, repo, _rec())
    assert out["identity"] == "replaced" and out["event"] is False
    assert _reaper_events(repo) == [], "a workless replacement spooled an event"
    assert identity.read_stamp(repo, "sess-1")["boot_id"] == "boot-2"


def test_epoch_fallback_detects_a_replacement_without_boot_ids(repo):
    stub = ProbeColab(boot_id=None, uptime_s=7200.0)
    assert identity.ensure_identity(stub, repo, "sess-1") == {"identity": "captured"}
    # Same runtime, read again: its boot epoch does not move.
    assert identity.check_identity(stub, repo, _rec()) == {"identity": "ok"}
    # A replacement runtime booted minutes ago: its epoch
    # postdates the stamped one far beyond the tolerance.
    stub.uptime_s = 120.0
    out = identity.check_identity(stub, repo, _rec())
    assert out["identity"] == "replaced"


# --- Reap integration ------------------------------------------------------------


class ReapColab:
    """The full reap surface: sessions list, keepalive
    relaunch, kernel pulse — and a scripted identity probe."""

    def __init__(self, boot_id="boot-1"):
        self.boot_id = boot_id

    def __call__(self, *args, **kwargs):
        if not args:
            return cp("")
        op = args[0]
        if op == "sessions":
            return cp('["sess-1"]')
        if op == "download":
            return cp("", rc=1)
        if op == "upload":
            return cp("uploaded")
        if op == "exec":
            text = " ".join(str(a) for a in args)
            if "identity-probe" in text:
                return cp(json.dumps({"boot_id": self.boot_id, "uptime_s": 3600.0}))
            if "lease-pulse" in text:
                return cp("lease pulse")
            return cp("keepalive launched")
        return cp("")


def test_reap_baselines_then_surfaces_a_replacement(repo):
    now = datetime.now(timezone.utc)
    rec = {
        "session": "sess-1", "shape": "cpu", "caller": "panel",
        "acquired_at": (now - timedelta(hours=1)).isoformat(),
        "deadline": (now + timedelta(hours=2)).isoformat(),
        "last_used_at": now.isoformat(), "jobs": ["run-1"],
    }
    runs = repo / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run-1.json").write_text(json.dumps({"run_id": "run-1", "job": "smoke", "session": "sess-1", "state": "running", "started_at": now.isoformat()}))
    lease.records.save_record(repo, rec)
    stub = ReapColab()
    out = lease.reap(stub, repo, now_dt=now)
    assert out["action"] == "kept"
    assert "runtime_identity" not in out, "the baseline pass announced a replacement"
    assert identity.read_stamp(repo, "sess-1")["boot_id"] == "boot-1"
    stub.boot_id = "boot-2"
    out = lease.reap(stub, repo, now_dt=now)
    assert out["runtime_identity"] == "replaced"
    assert out["runtime_runs"] == [] and out["runtime_jobs"] == ["run-1"]
    events = [e for e in _reaper_events(repo) if e["kind"] == "runtime-replaced"]
    assert len(events) == 1 and events[0]["runs"] == [] and events[0]["jobs"] == ["run-1"]
