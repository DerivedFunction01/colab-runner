"""Shared harness for the lease unit leaves (the split of the
former tests/test_lease_unit.py, consolidation Lane A).

Owns the one lease-package load for the folder (under the name
"lease", registered as the stanzas registered it), the fixed
clock, the folder's pytest fixtures, and the stub/record
builders several leaves share. Subject helpers used by a
single leaf live in that leaf.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import repo_fixture  # noqa: E402
from module_loader import load_lease  # noqa: E402

lease = load_lease("lease", register=True)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)

# --- The dynamic spec catalog (2026-11-01) ------------------------------------
# The picker options are parsed from `colab new --help`, never
# hand-listed; measured rates live in var/shape-rates.json.

HELP_TEXT = """
 Usage: colab new [OPTIONS]

 Create a new session

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session   -s      <str>  Session name                                      │
│ --tpu               <str>  TPU accelerator variant. Supported: v5e1, v6e1.   │
│ --gpu               <str>  GPU accelerator variant. Supported: T4, L4, G4,   │
│                            H100, A100.                                       │
│                            If omitted (along with --tpu), a CPU runtime is   │
│                            created.                                          │
│ --high-mem                 Request a high-RAM machine shape (CPU, T4, A100,  │
│                            etc.). Requires Colab Pro or Pro+ entitlement.    │
│                            Ignored for accelerators that only offer a single │
│                            shape (L4, v5e1, v6e1).                              │
│ --help      -h             Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
"""


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A scratch repo carrying the authored fixture config (see
    tests/fixtures/repo_fixture.py): the suite must pass in a
    bare clone, where the checkout has no operator config."""
    return repo_fixture.lease_repo(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def gate_permissive(monkeypatch):
    """Session shape gate: these units pin lease mechanics against
    the fixture repo's authored config, so they declare the
    permissive fixture state explicitly — the same explicit
    disable a fixture-owned config would carry. The gate's own
    tests live in test_lease_fake."""
    monkeypatch.setattr(lease.gate, "session_gate_policy", lambda repo: {"enabled": False, "agent_classes": ["high_ram"], "bypass_actors": ["panel"], "infra_actor": "standing"})


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def seed_record(repo):
    rec = {
        "session": "standard",
        "shape": "cpu",
        "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "rate_cu_per_hour": 0.08,
        "acquired_at": "2026-10-29T17:00:00+00:00",
        "deadline": "2026-10-30T01:00:00+00:00",
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": [],
    }
    lease.save_record(repo, rec)
    return rec


def seed_window_record(repo, **over):
    rec = {
        "session": "standard",
        "shape": "cpu",
        "shape_spec": {"gpu": None, "tpu": None, "high_mem": False},
        "rate_cu_per_hour": 0.08,
        # 12 h before NOW: at the renewal threshold (cap 12 h - 30 min).
        "acquired_at": "2026-10-29T06:00:00+00:00",
        "deadline": "2026-10-30T06:00:00+00:00",  # window still open
        "last_used_at": "2026-10-29T17:30:00+00:00",
        "jobs": ["run-1"],
        "renew": True,
    }
    rec.update(over)
    # run-1's registration holds through its backing record
    # (the pruned projection): plant it live BEFORE the save,
    # or the save's own pruning drops the registration.
    runs = repo / "results" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run-1.json").write_text(json.dumps({"run_id": "run-1", "session": "standard", "state": "running", "started_at": "2026-10-29T06:00:00+00:00"}))
    lease.save_record(repo, rec)
    return rec


def make_extend_stub(alive_sessions):
    def stub(*a, **k):
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(alive_sessions))
        return cp("")

    return stub


def make_renewal_stub(alive, calls, fail_new=None, handover=None):
    def stub(*a, **k):
        calls.append(a)
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(sorted(alive)))
        if cmd == "new":
            if fail_new is not None and fail_new():
                return cp("boom", rc=1)
            alive.add(a[a.index("-s") + 1])
            return cp("created")
        if cmd == "stop":
            alive.discard(a[a.index("-s") + 1])
            return cp("stopped")
        if cmd == "download":
            if handover is not None and str(a[3]).endswith("lease-handover.json"):
                dest = Path(a[-1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(handover))
                return cp("downloaded")
            return cp("", rc=1)  # no heartbeat file in the stub world
        return cp("")

    return stub


def _acquire_stub(*a, **k):
    if a and a[0] == "usage":
        return cp("balance: 500 CU (stub)")
    if a and a[0] == "sessions":
        return cp("[]")
    if a and a[0] == "new":
        return cp("created")
    if a and a[0] == "download":
        return cp("", rc=1)
    return cp("")


def _auto_stub(alive):
    def stub(*a, **k):
        cmd = a[0] if a else ""
        if cmd == "usage":
            return cp("balance: 500 CU (stub)")
        if cmd == "sessions":
            return cp(json.dumps(sorted(alive)))
        if cmd == "new":
            alive.add(a[a.index("-s") + 1])
            return cp("created")
        if cmd == "download":
            return cp("", rc=1)
        return cp("")

    return stub


def orphan_stub(names, balance=500):
    def stub(*args, **kwargs):
        if args and args[0] == "usage":
            return cp(f"balance: {balance} CU (stub)")
        if args and args[0] == "sessions":
            return cp(json.dumps(names))
        return cp("")

    return stub


def plant_kit_config(repo, session, *, deadline, acquired_at, gpu=None, tpu=None, high_mem=True):
    cfg = {
        "shape": lease.shape_id_for(gpu, tpu, high_mem),
        "session": session,
        "deadline": deadline.isoformat(),
        "acquired_at": acquired_at.isoformat(),
        "cap_seconds": 3600,
        "margin_seconds": 600,
        "high_mem": high_mem,
        "gpu": gpu,
        "tpu": tpu,
    }
    (repo / "var" / f"renewal-config-{session}.json").write_text(json.dumps(cfg, indent=2))
