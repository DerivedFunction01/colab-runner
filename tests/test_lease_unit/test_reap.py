"""Lease unit tests: reap, release, heartbeat, and status reads
with stub colab callables (no subprocess).

Pins the 2026-10-29 failure mode: an unreadable sessions list is
UNKNOWN, never death — reap must not clear the record, and a
failed release must keep the record so the lease is never
forgotten while it may still bill.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from unit_harness import NOW, cp, gate_permissive, lease, repo, seed_record  # noqa: F401


def test_reap_keeps_record_when_sessions_unreadable(repo, tmp_path, monkeypatch):
    seed_record(repo)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.reap(boom, repo, now_dt=NOW)
    assert out["action"] == "kept-unknown"
    assert lease.load_record(repo) is not None


def test_reap_clears_only_on_confirmed_absent(repo, tmp_path, monkeypatch):
    seed_record(repo)
    out = lease.reap(lambda *a, **k: cp("[]"), repo, now_dt=NOW)
    assert out["action"] == "cleared-dead"
    assert lease.load_record(repo) is None


def test_reap_pulses_kernel_when_due_and_skips_when_fresh(repo, tmp_path, monkeypatch):
    seed_record(repo)
    calls = []

    def stub(*a, **k):
        calls.append(a)
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)  # no heartbeat file: keepalive relaunch path, harmless here
        return cp("")

    out = lease.reap(stub, repo, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["pulse"] == "ok"
    execs = [c for c in calls if c and c[0] == "exec" and any("lease-pulse" in str(x) for x in c)]
    assert len(execs) == 1
    assert lease.load_record(repo)["last_pulse_at"] == NOW.isoformat()
    calls.clear()
    out2 = lease.reap(stub, repo, now_dt=NOW)
    assert out2["pulse"] == "not-due"
    assert not [c for c in calls if c and c[0] == "exec" and any("lease-pulse" in str(x) for x in c)]


def test_release_keeps_record_when_stop_fails(repo, tmp_path, monkeypatch):
    seed_record(repo)

    def flaky(*a, **k):
        # stop times out; sessions keeps listing the session as alive.
        if a and a[0] == "stop":
            raise subprocess.TimeoutExpired("colab", 120)
        return cp('["standard"]')

    out = lease.release(flaky, repo)
    assert out["action"] == "release-failed"
    assert lease.load_record(repo) is not None


def test_heartbeat_age_stall_degrades_to_unknown_with_short_bound(repo, tmp_path, monkeypatch):
    """A stalled heartbeat download (the file channel can hang on the
    egress proxy's IPv4 endpoint, diagnosed 2026-11-01) must degrade
    to age-unknown quickly — never wedge a status/state read behind
    the old 120 s bound."""
    seen = {}

    def stall(*a, **k):
        seen["args"] = a
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired("colab", k.get("timeout"))

    assert lease.heartbeat_age(stall, repo, "standard") is None
    assert seen["args"][0] == "download"
    assert seen["timeout"] == lease.HEARTBEAT_READ_TIMEOUT_S
    assert lease.HEARTBEAT_READ_TIMEOUT_S <= 15


def test_heartbeat_age_reads_a_fresh_heartbeat(repo, tmp_path, monkeypatch):

    def fetch(*a, **k):
        dest = Path(a[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).timestamp() - 42
        dest.write_text(json.dumps({"ts": ts}) + "\n")
        return cp("")

    age = lease.heartbeat_age(fetch, repo, "standard")
    assert age is not None and 40 <= age <= 45


def test_status_survives_a_stalled_heartbeat_download(repo, tmp_path, monkeypatch):
    """The UI's read path: session live + heartbeat download stalled
    still returns the status, with the age reported as unknown."""
    seed_record(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        return cp("")

    out = lease.status(stub, repo)
    assert out["action"] == "active"
    assert out["keepalive_heartbeat_age_s"] is None


def test_ensure_keepalive_stalled_upload_is_a_result_not_an_exception(repo, tmp_path, monkeypatch):
    """The keepalive upload rides the stall-prone file channel: a stall
    must come back as launch-failed (lease stands, caller carries on),
    bounded well under the old 300 s — never as a raised timeout."""
    seen = {}

    def stall_upload(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired("colab", k.get("timeout"))

    out = lease.ensure_keepalive(stall_upload, repo, "standard")
    assert out["keepalive"] == "launch-failed"
    assert out["stage"] == "upload"
    assert seen["timeout"] == lease.KEEPALIVE_UPLOAD_TIMEOUT_S
    assert lease.KEEPALIVE_UPLOAD_TIMEOUT_S <= 30


def test_ensure_keepalive_stalled_launch_is_a_result_not_an_exception(repo, tmp_path, monkeypatch):

    def stall_launch(*a, **k):
        if a and a[0] == "exec":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        return cp("")

    out = lease.ensure_keepalive(stall_launch, repo, "standard")
    assert out["keepalive"] == "launch-failed"
    assert out["stage"] == "launch"


def test_reap_pulses_even_when_keepalive_relaunch_stalls(repo, tmp_path, monkeypatch):
    """Regression for 2026-11-01: reap died inside the keepalive
    relaunch's stalled upload before ever reaching the kernel pulse,
    leaving the lease with no activity channel at all. The pulse is
    an exec — it must not be starved by a file-channel failure."""
    seed_record(repo)

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp('["standard"]')
        if a and a[0] == "download":
            return cp("", rc=1)  # no heartbeat: relaunch path
        if a and a[0] == "upload":
            raise subprocess.TimeoutExpired("colab", k.get("timeout"))
        if a and a[0] == "exec" and any("lease-pulse" in str(x) for x in a):
            return cp("lease pulse")
        return cp("")

    out = lease.reap(stub, repo, now_dt=NOW)
    assert out["action"] == "kept"
    assert out["keepalive"] == "launch-failed"
    assert out["pulse"] == "ok"


def test_specs_serves_catalog_labels_with_id_fallback(tmp_path):
    var = tmp_path / "var"
    var.mkdir(parents=True)
    (var / "shape-rates.json").write_text(json.dumps({"cpu": 0.5}))
    specs = {s["id"]: s for s in lease.specs(tmp_path)}
    # No CLI cache in the scratch repo: the CPU shapes always serve,
    # with their mechanical labels; a measured rate joins from the
    # rate table, an unmeasured shape's rate stays unconfirmed.
    assert specs["cpu"]["label"] == "CPU"
    assert specs["cpu"]["rate_cu_per_hour"] == 0.5
    assert specs["cpu-highram"]["label"] == "CPU High-RAM"
    assert specs["cpu-highram"]["rate_cu_per_hour"] is None


def test_specs_real_catalog_labels(repo):
    labels = {s["id"]: s["label"] for s in lease.specs(repo)}
    assert labels["cpu"] == "CPU"
    assert labels["cpu-highram"] == "CPU High-RAM"


def test_live_sessions_empty_listing_has_no_phantom(repo):
    """The CLI answers an empty account with '[colab] No active
    sessions.' — its own log prefix is not a session name, and the
    listing must read as empty, never as a session named 'colab'
    (the phantom the access list stamped unattributed on
    2026-11-05)."""

    def stub(*a, **k):
        if a and a[0] == "sessions":
            return cp("[colab] No active sessions found on server.\n[colab] No active sessions.")
        return cp("")

    assert lease.live_sessions(stub, repo) == []
