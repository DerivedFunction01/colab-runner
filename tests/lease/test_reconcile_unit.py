"""Reconciliation-evidence and last-use unit tests.

Pins the record-truth contracts (2026-11-11): a
records-vs-channel disagreement (live session, no lease
record / live record, channel-absent session) lands as a
journal line carrying both readings — throttled per
(session, kind, readings) inside the window, re-journaled
when a reading changes — from its producers (the lease
state reads, colab-sh's orphan advisory); and the served
last-use line folds detached-record activity in, so
detached shell work counts as session use.
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

REPO = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "lease", REPO / "sync" / "lease" / "__init__.py",
    submodule_search_locations=[str(REPO / "sync" / "lease")])
lease = importlib.util.module_from_spec(spec)
sys.modules.setdefault("lease", lease)
if "reconcile" not in dir(lease):
    spec.loader.exec_module(lease)
reconcile = importlib.import_module("lease.reconcile")
records = importlib.import_module("lease.records")

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)

_sh_spec = importlib.util.spec_from_loader("colab_sh", SourceFileLoader("colab_sh", str(REPO / "bin" / "colab-sh")))
sh = importlib.util.module_from_spec(_sh_spec)
_sh_spec.loader.exec_module(sh)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = repo_fixture.scratch_repo(tmp_path)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


T0 = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)


def _journal(repo):
    path = reconcile.reconcile_journal_path(repo)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- The journal -----------------------------------------------------------------


def test_journal_line_carries_both_readings(repo):
    assert reconcile.journal_disagreement(repo, "sess-1", "live-no-record", "no-record", "live", "state", now_dt=T0) is True
    (entry,) = _journal(repo)
    assert entry == {"ts": T0.isoformat(), "session": "sess-1", "kind": "live-no-record", "local": "no-record", "channel": "live", "source": "state"}


def test_standing_disagreement_journals_once_per_window(repo):
    assert reconcile.journal_disagreement(repo, "sess-1", "live-no-record", "no-record", "live", "state", now_dt=T0) is True
    assert reconcile.journal_disagreement(repo, "sess-1", "live-no-record", "no-record", "live", "state", now_dt=T0 + timedelta(minutes=30)) is False
    assert len(_journal(repo)) == 1
    # Past the window the standing disagreement re-journals.
    assert reconcile.journal_disagreement(repo, "sess-1", "live-no-record", "no-record", "live", "state", now_dt=T0 + timedelta(seconds=reconcile.JOURNAL_WINDOW_S + 1)) is True
    assert len(_journal(repo)) == 2


def test_a_changed_reading_rejournals_at_once(repo):
    reconcile.journal_disagreement(repo, "sess-1", "record-no-session", "record-live", "absent", "state", now_dt=T0)
    assert reconcile.journal_disagreement(repo, "sess-1", "record-no-session", "record-live", "unknown", "state", now_dt=T0 + timedelta(minutes=1)) is True
    assert len(_journal(repo)) == 2


# --- Producers: the state reads ---------------------------------------------------


def _seed(repo, session="sess-2", last_used_at="2026-11-11T15:55:00+00:00"):
    rec = {
        "session": session, "shape": "cpu", "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "acquired_at": "2026-11-11T15:00:00+00:00", "deadline": "2026-11-11T23:00:00+00:00",
        "last_used_at": last_used_at, "jobs": [],
    }
    records.save_record(repo, rec)
    return rec


def test_status_record_no_session_journals_the_disagreement(repo):
    _seed(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp("[]")
        return cp("", rc=1)

    out = lease.status(stub, repo, session="sess-2")
    assert out["action"] == "recorded-but-dead"
    (entry,) = _journal(repo)
    assert entry["kind"] == "record-no-session" and entry["session"] == "sess-2" and entry["source"] == "state"


def test_status_live_record_journals_nothing(repo):
    _seed(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["sess-2"]')
        return cp("", rc=1)

    out = lease.status(stub, repo, session="sess-2")
    assert out["action"] == "active"
    assert _journal(repo) == []


def test_orphan_census_journals_live_no_record(repo):
    _seed(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["sess-2", "orphan-9"]')
        return cp("", rc=1)

    out = lease.state_orphans(stub, repo, now_dt=T0)
    assert [o["session"] for o in out["orphans"]] == ["orphan-9"]
    (entry,) = _journal(repo)
    assert entry["kind"] == "live-no-record" and entry["session"] == "orphan-9"


# --- Producer: colab-sh's orphan advisory -----------------------------------------


def test_colab_sh_orphan_advisory_journals(repo, capsys):
    sh.orphan_advisory("orphan-7")
    note = capsys.readouterr().err
    assert "no lease record (orphaned)" in note
    (entry,) = _journal(repo)
    assert entry["kind"] == "live-no-record" and entry["session"] == "orphan-7" and entry["source"] == "colab-sh"


def test_colab_sh_advisory_is_silent_with_a_record(repo, capsys):
    _seed(repo, session="sess-3")
    sh.orphan_advisory("sess-3")
    assert capsys.readouterr().err == ""
    assert _journal(repo) == []


# --- Last use folds detached activity ----------------------------------------------


def _plant_detached(repo, did, session, started_at, finished_at=None):
    detached = repo / "results" / "detached"
    detached.mkdir(parents=True, exist_ok=True)
    (detached / f"{did}.json").write_text(json.dumps({
        "id": did, "session": session, "caller": "panel",
        "state": "complete" if finished_at else "running",
        "exit_code": 0 if finished_at else None,
        "started_at": started_at, "finished_at": finished_at,
    }))


def test_detached_activity_reads_the_newest_stamp(repo):
    _plant_detached(repo, "d1", "sess-2", "2026-11-11T15:58:00+00:00", "2026-11-11T16:05:00+00:00")
    _plant_detached(repo, "d2", "sess-2", "2026-11-11T16:20:00+00:00")
    _plant_detached(repo, "d3", "other", "2026-11-11T17:00:00+00:00")
    assert records.detached_activity(repo, "sess-2") == "2026-11-11T16:20:00+00:00"
    assert records.detached_activity(repo, "nobody") is None


def test_status_last_use_folds_detached_activity(repo):
    rec = _seed(repo)
    _plant_detached(repo, "d1", "sess-2", "2026-11-11T15:58:00+00:00")

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["sess-2"]')
        return cp("", rc=1)

    out = lease.status(stub, repo, session="sess-2")
    assert out["record"]["last_used_at"] == "2026-11-11T15:58:00+00:00", "the served line ignored the detached run"
    assert records.load_record(repo, "sess-2")["last_used_at"] == rec["last_used_at"], "the read rewrote the stored record"
