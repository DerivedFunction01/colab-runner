"""Caller attribution seams against the fake colab CLI and
in process: the caller stamps, the demand carry, the extend
fill, the CLI claim refusal, the access list, and
unattributed-session detection."""

import json
import subprocess
import sys
from datetime import timedelta

from fake_harness import PNOW, FakeColab, _spool_kinds, gate_repo, lease_bin, lease_env, lease_mod, record, record_files, run_lease, scratch_repo  # noqa: F401

# --- Caller attribution seams (plan: .plans/2026-11-04-caller-attribution.md) --
# The core stamps the caller it is given (the wrapper seams own
# validation); these tests pin the stamping, the demand carry, the
# extend fill, and the CLI's claim-time refusal.


def test_caller_stamps_created_record_and_adopt_fills(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    assert out["action"] == "created"
    session = out["record"]["session"]
    assert lease_mod.load_record(repo, session)["caller"] == "alpha"
    # The creator re-acquiring adopts its own lease, caller intact.
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    assert out["action"] == "adopted"
    assert out["record"]["session"] == session
    # Another caller rides it: same session, creator's stamp kept,
    # the ride journaled as the rider's claim.
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="beta")
    assert out["action"] == "adopted"
    assert out["record"]["session"] == session
    assert out["riding"] is True and out["owner_caller"] == "alpha"
    assert len(lease_mod.all_records(repo)) == 1
    assert lease_mod.load_record(repo, session)["caller"] == "alpha"
    assert lease_mod.load_caller_claims(repo)[session]["caller"] == "beta"
    # A pre-attribution record gains the adopting call's caller:
    # an unstamped record is adoptable by the first caller to ask.
    out = lease_mod.acquire(fake, repo, "cpu-highram", now_dt=PNOW)
    assert out["action"] == "created"
    plain = out["record"]["session"]
    assert "caller" not in lease_mod.load_record(repo, plain)
    out = lease_mod.acquire(fake, repo, "cpu-highram", now_dt=PNOW, caller="beta")
    assert out["action"] == "adopted"
    assert out["record"]["session"] == plain
    assert lease_mod.load_record(repo, plain)["caller"] == "beta"


def test_caller_extend_fills_only_when_absent(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab()
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    session = out["record"]["session"]
    out = lease_mod.extend(fake, repo, session, 1, now_dt=PNOW, caller="alpha")
    assert out["action"] == "extended" and out["caller"] == "alpha"
    assert lease_mod.load_record(repo, session)["caller"] == "alpha"
    out = lease_mod.extend(fake, repo, session, 1, now_dt=PNOW, caller="beta")
    assert lease_mod.load_record(repo, session)["caller"] == "alpha"


def test_caller_demand_consumed_stamps_the_record(tmp_path, monkeypatch):
    repo = gate_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COLAB_RUNNER_AUTO_ACQUIRE", "1")
    fake = FakeColab()
    # A High-RAM demand from a registered caller is askable (the
    # write-time check is shape-only) and the duty creates it,
    # stamping the demand's caller on the record and the result.
    out = lease_mod.write_request(repo, reason="burst", shape="cpu-highram", caller="alpha", now_dt=PNOW)
    assert out["action"] == "requested"
    assert lease_mod.load_request(repo)["caller"] == "alpha"
    out = lease_mod.auto_acquire(fake, repo, now_dt=PNOW)
    assert out["action"] == "auto-acquired" and out["caller"] == "alpha"
    assert lease_mod.load_record(repo)["caller"] == "alpha"


def test_caller_cli_claim_refusal_and_stamp(tmp_path):
    """Against the live config: an unregistered --caller is refused
    at claim time (exit 2, nothing created); a machinery caller
    claims cleanly and is stamped on the record."""
    env = lease_env(tmp_path)

    def run(*args):
        return subprocess.run([sys.executable, str(lease_bin(env)), *args], capture_output=True, text=True, env=env, timeout=300)

    r = run("acquire", "--shape", "cpu", "--caller", "no-such-thread")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no-such-thread" in r.stderr
    assert record_files(env) == []
    out = run_lease(env, "acquire", "--shape", "cpu", "--caller", "test")
    assert out["action"] == "created"
    assert record(env)["caller"] == "test"


def test_access_list_attributes_each_class(tmp_path, monkeypatch):
    """Step 5: the access list serves every live session with its
    attribution — record-backed from the record, claim-only from
    the claims journal, the rest unattributed with the detection
    stamp's first_seen."""
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"ghost", "stray"})
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW, caller="alpha")
    session = out["record"]["session"]
    lease_mod.append_caller_claim(repo, "ghost", "beta", "colab-sh")
    lease_mod.save_unattributed(repo, {"stray": {"first_seen": "2026-11-05T01:02:03+00:00", "endpoint": None, "shape_class": "standard"}})
    access = lease_mod.access_list(fake, repo)
    by_session = {e["session"]: e for e in access}
    assert set(by_session) == {session, "ghost", "stray"}
    assert by_session[session]["caller"] == "alpha"
    assert by_session[session]["first_seen"] == lease_mod.load_record(repo, session)["acquired_at"]
    assert "unattributed" not in by_session[session]
    assert by_session["ghost"]["caller"] == "beta" and by_session["ghost"]["first_seen"]
    assert by_session["stray"]["caller"] is None and by_session["stray"]["unattributed"] is True
    assert by_session["stray"]["first_seen"] == "2026-11-05T01:02:03+00:00"
    assert by_session["stray"]["shape_class"] == "standard"


def test_access_list_unreadable_listing_serves_empty(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("list failed")

    assert lease_mod.live_sessions(boom, repo) is None
    assert lease_mod.access_list(boom, repo) == []


def test_release_session_stops_strays_and_refuses_records(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"stray"})
    out = lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    session = out["record"]["session"]
    lease_mod.save_unattributed(repo, {"stray": {"first_seen": PNOW.isoformat(), "endpoint": None, "shape_class": None}})
    monkeypatch.setattr(lease_mod.sessions, "stop_session_safe", lambda colab, session: "stopped")
    out = lease_mod.release_session(fake, repo, "stray")
    assert out["action"] == "session-released" and out["stop"] == "stopped"
    assert lease_mod.load_unattributed(repo) == {}
    out = lease_mod.release_session(fake, repo, session)
    assert out["action"] == "session-has-record"


def test_reap_detects_unattributed_once_and_clears_on_claim(tmp_path, monkeypatch):
    """Step 6: a live session with no record and no claim is
    stamped and spooled exactly once; a later claim clears it with
    its own event. Record-backed and claimed sessions never flag."""
    repo = scratch_repo(tmp_path, monkeypatch)
    fake = FakeColab(alive={"stray", "ghost"})
    lease_mod.acquire(fake, repo, "cpu", now_dt=PNOW)
    lease_mod.append_caller_claim(repo, "ghost", "beta", "colab-sh")
    out = lease_mod.reap(fake, repo, now_dt=PNOW)
    assert out["unattributed"] == {"detected": ["stray"], "cleared": []}
    stamps = lease_mod.load_unattributed(repo)
    assert stamps["stray"]["first_seen"] == PNOW.isoformat()
    events = _spool_kinds(repo, "unattributed-session")
    assert len(events) == 1 and events[0]["session"] == "stray"
    # A second pass re-spools nothing and keeps the first_seen.
    out = lease_mod.reap(fake, repo, now_dt=PNOW + timedelta(minutes=5))
    assert out["unattributed"] == {"detected": [], "cleared": []}
    assert len(_spool_kinds(repo, "unattributed-session")) == 1
    assert lease_mod.load_unattributed(repo)["stray"]["first_seen"] == PNOW.isoformat()
    # The stray gains a claim: the next pass clears the stamp.
    lease_mod.append_caller_claim(repo, "stray", "alpha", "colab-sh")
    out = lease_mod.reap(fake, repo, now_dt=PNOW + timedelta(minutes=10))
    assert out["unattributed"] == {"detected": [], "cleared": ["stray"]}
    assert lease_mod.load_unattributed(repo) == {}
    cleared = _spool_kinds(repo, "unattributed-cleared")
    assert len(cleared) == 1 and cleared[0]["session"] == "stray"


def test_detect_unattributed_never_acts_on_an_unreadable_list(tmp_path, monkeypatch):
    repo = scratch_repo(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("list failed")

    assert lease_mod.detect_unattributed(boom, repo, PNOW) == {"detected": [], "cleared": []}
    assert lease_mod.load_unattributed(repo) == {}
