"""Listener census unit tests: the in-session probe against a
fixture /proc, the collection duty's stamps and throttle, and
the served state's suspect derivation.

The census exists for the 2026-11-12 leaks: two next-servers
parented to init with deleted cwds held ports 3271/3273 on a
live session, invisible to every run record, one voiding a
consumer's draw by answering its health check."""

import importlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from unit_harness import NOW, cp, lease, repo, seed_record  # noqa: F401

listeners = importlib.import_module("lease.listeners")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_probe_spec = importlib.util.spec_from_loader("listener_census", SourceFileLoader("listener_census", str(REPO_ROOT / "snippets" / "listener_census.py")))
probe = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe)

# seed_record's acquired_at (2026-10-29T17:00Z) as an epoch.
ACQUIRED_EPOCH = datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc).timestamp()


def _fixture_proc(tmp_path):
    """One listening socket (port 3273, inode 4242) owned by
    pid 947 whose cwd is a deleted directory."""
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:0CC9 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4242\n"
        "   1: 0100007F:1F90 00000000:0000 01 00000000:00000000 00:00000000 00000000     0        0 9999\n"
    )
    (proc / "net" / "tcp6").write_text("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
    (proc / "stat").write_text("cpu  0 0 0 0\nbtime 1700000000\n")
    pid_dir = proc / "947"
    (pid_dir / "fd").mkdir(parents=True)
    os.symlink("socket:[4242]", pid_dir / "fd" / "3")
    os.symlink("/content/gone (deleted)", pid_dir / "cwd")
    (pid_dir / "comm").write_text("next-server\n")
    (pid_dir / "stat").write_text("947 (next-server) " + " ".join(["S"] + ["0"] * 18 + ["5000"]) + "\n")
    return proc


def test_probe_reads_the_listening_owner(tmp_path):
    out = probe.census(_fixture_proc(tmp_path))
    assert len(out["listeners"]) == 1, out
    entry = out["listeners"][0]
    assert entry["port"] == 3273 and entry["pid"] == 947
    assert entry["cwd"] == "/content/gone (deleted)"
    assert entry["cmd"] == "next-server"
    assert entry["started_epoch"] == pytest.approx(1700000000 + 5000 / os.sysconf("SC_CLK_TCK"))


def test_probe_empty_when_nothing_listens(tmp_path):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text("  sl  local_address rem_address   st inode\n")
    (proc / "stat").write_text("btime 1700000000\n")
    assert probe.census(proc) == {"listeners": []}


def _stub_colab(stdout, rc=0):
    def colab(*args, **kwargs):
        return cp(stdout=stdout, rc=rc)

    return colab


PROBE_OUT = '[colab] A new version of the CLI is available\n{"listeners": [{"port": 3273, "pid": 947, "cwd": "/content/sp-gone (deleted)", "cmd": "next-server", "started_epoch": 1793232000.0}]}\n'


def test_collect_stamps_the_parsed_census(repo):
    stamp = listeners.collect_census(_stub_colab(PROBE_OUT), repo, "standard", NOW)
    assert "error" not in stamp
    assert stamp["listeners"][0]["port"] == 3273
    assert listeners.read_census(repo, "standard") == stamp


def test_collect_failure_stamps_the_failure(repo):
    stamp = listeners.collect_census(_stub_colab("", rc=1), repo, "standard", NOW)
    assert stamp["error"] == "probe-failed" and "listeners" not in stamp


def test_maybe_collect_throttles_inside_the_interval(repo):
    listeners.collect_census(_stub_colab(PROBE_OUT), repo, "standard", NOW)
    calls = []

    def counting(*args, **kwargs):
        calls.append(args)
        return cp(stdout=PROBE_OUT)

    assert listeners.maybe_collect(counting, repo, seed_record(repo), NOW + timedelta(seconds=60)) is None
    assert calls == []
    fresh = listeners.maybe_collect(counting, repo, seed_record(repo), NOW + timedelta(seconds=601))
    assert fresh is not None and calls, "a stale stamp must re-collect"


def test_maybe_collect_skips_a_past_deadline_record(repo):
    rec = seed_record(repo)
    rec["deadline"] = (NOW - timedelta(hours=1)).isoformat()
    assert listeners.maybe_collect(_stub_colab(PROBE_OUT), repo, rec, NOW) is None
    assert listeners.read_census(repo, "standard") is None


def _stamp(repo, entries):
    listeners._save_stamp(repo, "standard", {"session": "standard", "at": NOW.isoformat(), "listeners": entries})


def test_served_state_derives_suspects(repo):
    rec = seed_record(repo)
    _stamp(
        repo,
        [
            {"port": 3273, "pid": 947, "cwd": "/content/sp-gone (deleted)", "cmd": "next-server", "started_epoch": ACQUIRED_EPOCH - 3600},
            {"port": 3100, "pid": 555, "cwd": "/content/sp-serve/apps/web", "cmd": "next-server", "started_epoch": ACQUIRED_EPOCH + 3600},
        ],
    )
    state = listeners.listeners_state(repo, rec, NOW)
    by_port = {e["port"]: e for e in state["listeners"]}
    assert by_port[3273]["suspect"] == ["deleted-cwd", "predates-lease"]
    assert by_port[3100]["suspect"] == []
    assert state["error"] is None and state["age_s"] == 0


def test_served_state_without_a_stamp_is_an_honest_empty(repo):
    state = listeners.listeners_state(repo, seed_record(repo), NOW)
    assert state == {"at": None, "age_s": None, "error": None, "listeners": []}


def test_state_listeners_portion_keys_by_session(repo):
    rec = seed_record(repo)
    _stamp(repo, [{"port": 3273, "pid": 947, "cwd": "/x (deleted)", "cmd": "next-server", "started_epoch": ACQUIRED_EPOCH - 60}])
    portion = lease.state_listeners(repo, NOW)
    assert set(portion["listeners"]) == {"standard"}
    assert portion["listeners"]["standard"]["listeners"][0]["suspect"] == ["deleted-cwd"]
