"""Lease unit tests: stop_session_safe and the unassign
fallback, with stub colab callables (no subprocess)."""

import json

from unit_harness import REPO, cp, gate_permissive, lease, repo  # noqa: F401


def _stop_harness(monkeypatch, tmp_path, *, stop_rc, alive, unassign_rc):
    """Wire stop_session_safe's seams to stubs: the colab callable,
    the local binding, the fallback subprocess, and the server-side
    alive read. Returns (colab, fallback_argv_list)."""
    from types import SimpleNamespace

    binding = tmp_path / "sessions.json"
    binding.write_text(json.dumps({"sess": {"endpoint": "m-s-fake-endpoint"}}))
    monkeypatch.setattr(lease.sessions, "local_sessions_path", lambda: binding)
    monkeypatch.setattr(lease.sessions, "session_alive", lambda c, s: alive)
    monkeypatch.setattr(lease.sessions, "time", SimpleNamespace(sleep=lambda s: None))
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return cp(rc=unassign_rc)

    monkeypatch.setattr(lease.sessions, "subprocess", SimpleNamespace(run=fake_run))

    def colab(*args, **kwargs):
        return cp(rc=stop_rc)

    return colab, calls


def test_stop_session_safe_stopped_via_cli(monkeypatch, tmp_path):
    colab, calls = _stop_harness(monkeypatch, tmp_path, stop_rc=0, alive=False, unassign_rc=0)
    assert lease.stop_session_safe(colab, "sess") == "stopped"
    assert calls == []


def test_stop_session_safe_fallback_verdict_verified(monkeypatch, tmp_path):
    colab, calls = _stop_harness(monkeypatch, tmp_path, stop_rc=1, alive=False, unassign_rc=0)
    assert lease.stop_session_safe(colab, "sess") == "unassigned-fallback"
    script = calls[0][2]
    assert 'data=b"{}"' in script and "GetUnassignRequest" in script


def test_stop_session_safe_fallback_still_alive_is_failed(monkeypatch, tmp_path):
    colab, _ = _stop_harness(monkeypatch, tmp_path, stop_rc=1, alive=True, unassign_rc=0)
    assert lease.stop_session_safe(colab, "sess") == "stop-failed"


def test_stop_session_safe_fallback_call_failed_is_failed(monkeypatch, tmp_path):
    colab, _ = _stop_harness(monkeypatch, tmp_path, stop_rc=1, alive=False, unassign_rc=1)
    assert lease.stop_session_safe(colab, "sess") == "stop-failed"


def test_no_raw_unassign_call_remains():
    """The bodiless-POST defect (411, 2026-11-04) lived in three raw
    call sites; none may return. The sanctioned form replays the
    client's two-step flow with a body attached."""
    lease_src = "".join(
        p.read_text() for p in sorted((REPO / "sync" / "lease").glob("*.py")))
    assert "client.unassign(" not in lease_src
    for rel in ("probes/_driver.py", "snippets/unassign_orphans.py"):
        assert "client.unassign(" not in (REPO / rel).read_text(), rel
    assert 'data=b"{}"' in lease.UNASSIGN_FALLBACK_SCRIPT
