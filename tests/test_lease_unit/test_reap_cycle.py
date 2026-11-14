"""Lease unit tests: the reap cycle budget (2026-11-14).

Pins the cure for the 2026-11-13 exit-124s: a pass classifies
every record from ONE shared listing, lifecycle legs complete
before any furniture starts, furniture runs under a monotonic
budget that skips-and-reports instead of being killed
mid-flight, and keepalive launches execute as a deferred
phase of their own — a deferral costs one cycle, never a
lease, and never another record's renewal.
"""

import subprocess
import sys
from datetime import timedelta

from unit_harness import NOW, cp, lease, repo, seed_record  # noqa: F401

reap_mod = sys.modules["lease.reap"]


def _seed_second(repo):
    seed_record(repo)
    rec2 = dict(lease.load_record(repo))
    rec2["session"] = "second"
    lease.save_record(repo, rec2)


def test_one_listing_serves_every_verdict(repo):
    """Two records, one pass: exactly one sessions call — the
    snapshot — serves both records' state verdicts and the
    unattributed detection riding on it."""
    _seed_second(repo)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "sessions":
            return cp('["standard", "second"]')
        if a and a[0] == "download":
            return cp("", rc=1)  # no heartbeat: launch path, harmless here
        return cp("")

    out = lease.reap(stub, repo, now_dt=NOW)
    assert {r["session"]: r["action"] for r in out["records"]} == {"standard": "kept", "second": "kept"}
    assert len([c for c in calls if c and c[0] == "sessions"]) == 1
    assert out["budget"]["skipped"] == []


def test_failed_snapshot_costs_one_retry_sequence(repo):
    """An unreadable listing classifies every record
    kept-unknown (never dead) and costs the reap retry
    sequence once — not once per record."""
    seed_record(repo)
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise subprocess.TimeoutExpired("colab", 45)

    out = lease.reap(boom, repo, now_dt=NOW)
    assert out["action"] == "kept-unknown"
    assert out["read_error"]
    assert len([c for c in calls if c and c[0] == "sessions"]) == lease.sessions._REAP_READ_ATTEMPTS
    assert lease.load_record(repo) is not None


def test_budget_never_skips_lifecycle(repo, monkeypatch):
    """Budget zero: a past-deadline record still releases —
    the budget gates furniture, never lifecycle."""
    monkeypatch.setenv("COLAB_LEASE_REAP_BUDGET_S", "0")
    seed_record(repo)
    calls = []
    stopped = {"done": False}

    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "stop":
            stopped["done"] = True
            return cp("")
        if a and a[0] == "sessions":
            return cp("[]" if stopped["done"] else '["standard"]')
        return cp("")

    out = lease.reap(stub, repo, now_dt=NOW + timedelta(days=2))
    assert out["action"] == "released-deadline"
    assert lease.load_record(repo) is None
    assert not [c for c in calls if c and c[0] == "exec"]
    assert out["budget"]["skipped"] == ["listeners:standard"]


def test_budget_skips_furniture_whole(repo, monkeypatch):
    """Budget zero on a kept record: no identity exec, no
    freshness download, no pulse — the skip is on the record's
    entry and in the pass's budget report."""
    monkeypatch.setenv("COLAB_LEASE_REAP_BUDGET_S", "0")
    seed_record(repo)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "sessions":
            return cp('["standard"]')
        return cp("")

    out = lease.reap(stub, repo, now_dt=NOW)
    assert out["action"] == "kept"
    entry = out["records"][0]
    assert entry["furniture"] == "skipped-budget"
    assert "pulse" not in entry and "keepalive" not in entry
    assert not [c for c in calls if c and c[0] in ("exec", "download", "upload")]
    assert "furniture:standard" in out["budget"]["skipped"]


class _FakeTime:
    """reap.py's time namespace with a hand-driven monotonic
    clock (its sleep/time delegate to the real module)."""

    def __init__(self, real):
        self._real = real
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        return self._real.sleep(s)

    def time(self):
        return self._real.time()


def _stale_keepalive_stub(calls, on_download=None):
    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            if on_download is not None:
                on_download()
            return cp("", rc=1)  # no heartbeat file: the loop reads stale
        return cp("")

    return stub


def test_stale_keepalive_launch_deferred_when_budget_expires(repo, monkeypatch):
    """The freshness read happens inside the budget; by the
    launch phase the budget is gone, so no upload is attempted
    and the record carries launch-deferred — while the pulse,
    furniture in its own right, still ran."""
    import time as real_time

    fake = _FakeTime(real_time)
    monkeypatch.setattr(reap_mod, "time", fake)
    monkeypatch.setenv("COLAB_LEASE_REAP_BUDGET_S", "360")
    seed_record(repo)
    calls = []
    stub = _stale_keepalive_stub(calls, on_download=lambda: setattr(fake, "now", 1000.0))
    out = lease.reap(stub, repo, now_dt=NOW)
    entry = out["records"][0]
    assert entry["keepalive"] == "launch-deferred"
    assert entry["pulse"] == "ok"
    assert not [c for c in calls if c and c[0] == "upload"]
    assert "keepalive-launch:standard" in out["budget"]["skipped"]


def test_stale_keepalive_launches_in_the_deferred_phase(repo):
    """Ample budget: the launch the furniture phase owed
    executes in the launch phase and its verdict lands on the
    record, exactly as the inline launch used to report it."""
    seed_record(repo)
    calls = []
    stub = _stale_keepalive_stub(calls)
    out = lease.reap(stub, repo, now_dt=NOW)
    entry = out["records"][0]
    assert entry["keepalive"] == "launched"
    assert entry["pulse"] == "ok"
    assert [c for c in calls if c and c[0] == "upload"]
