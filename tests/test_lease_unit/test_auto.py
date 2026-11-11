"""Lease unit tests: the auto-acquire duty's timed starts,
with stub colab callables (no subprocess)."""

from datetime import timedelta

from unit_harness import NOW, _auto_stub, gate_permissive, lease, repo  # noqa: F401


def test_auto_timed_start_waits_then_fires(repo, tmp_path, monkeypatch):
    """A demand with a future not_before survives an auto pass
    untouched (not consumed, not failed, no lease), then acquires on
    the first pass after its time — a timed start as data, with the
    request's tag (written here in its legacy via spelling) read as
    the caller claim and stamped on the created record."""
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    alive = set()
    stub = _auto_stub(alive)
    lease.write_request(repo, reason="timed", shape="cpu", now_dt=NOW, via="timer", not_before=NOW + timedelta(hours=1))
    out = lease.auto_acquire(stub, repo, now_dt=NOW)
    assert out["action"] == "not-due"
    assert lease.load_request(repo) is not None
    assert lease.load_record(repo) is None
    assert alive == set()
    out = lease.auto_acquire(stub, repo, now_dt=NOW + timedelta(hours=2))
    assert out["action"] == "auto-acquired"
    assert out["caller"] == "timer"
    assert lease.load_request(repo) is None
    rec = lease.load_record(repo)
    assert rec is not None and rec["caller"] == "timer"
