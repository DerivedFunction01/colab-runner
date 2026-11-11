"""Lease unit tests: the provisioning ensure on the adoption
paths, with stub colab callables (no subprocess).

Pins the retry-reachability contract (specimen: ember
r1794417373, 2026-11-11 — a lock-contended provisioning
failure at first registration could never retry, because the
acquire adoption branch only fired the ensure when NO stamp
was on file, so the first failed stamp was terminal for the
session's life despite the 30-minute retry window): adoption
fires the ensure unconditionally, and the ensure's own stamp
logic decides — a failed attempt past its window relaunches,
an attempt inside its window reads pending, a verdict-ok
stamp no-ops.
"""

import json
from datetime import datetime, timedelta, timezone

from unit_harness import NOW, _auto_stub, lease, repo  # noqa: F401


def _seed_named(repo, session, deadline_h=6):
    rec = {
        "session": session,
        "shape": "cpu",
        "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "rate_cu_per_hour": 0.08,
        "acquired_at": NOW.isoformat(),
        "deadline": (NOW + timedelta(hours=deadline_h)).isoformat(),
        "last_used_at": NOW.isoformat(),
        "jobs": [],
    }
    lease.save_record(repo, rec)
    return rec


def _plant_stamp(repo, session, *, attempted_ago, chromium="failed"):
    attempted = datetime.now(timezone.utc) - attempted_ago
    stamp = {"attempted_at": attempted.isoformat(), "state": "verdict", "chromium": chromium, "install_rc": 100}
    path = lease.var_dir(repo) / f"provision-{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp))
    return path


def test_adopt_retries_a_failed_stamp_past_its_window(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = _auto_stub({"prov-retry-01"})
    _seed_named(repo, "prov-retry-01")
    _plant_stamp(repo, "prov-retry-01", attempted_ago=timedelta(hours=2))
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="prov-retry-01")
    assert out["action"] == "adopted"
    assert out["provisioning"] == "launched"


def test_adopt_inside_the_window_reads_pending_and_launches_nothing(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    base = _auto_stub({"prov-pend-01"})

    def stub(*a, **k):
        calls.append(a)
        return base(*a, **k)

    _seed_named(repo, "prov-pend-01")
    _plant_stamp(repo, "prov-pend-01", attempted_ago=timedelta(minutes=5))
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="prov-pend-01")
    assert out["action"] == "adopted"
    assert out["provisioning"] == "pending"
    assert not any(c and c[0] == "upload" and any("session_provision" in str(x) for x in c) for c in calls)


def test_adopt_with_a_verdict_ok_stamp_never_relaunches(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = _auto_stub({"prov-ok-01"})
    _seed_named(repo, "prov-ok-01")
    _plant_stamp(repo, "prov-ok-01", attempted_ago=timedelta(hours=5), chromium="installed")
    out = lease.acquire(stub, repo, "cpu", now_dt=NOW, prefer_session="prov-ok-01")
    assert out["action"] == "adopted"
    assert out["provisioning"] == "installed"
