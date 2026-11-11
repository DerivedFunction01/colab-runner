"""Shared harness for the lease fake leaves (the split of the
former tests/test_lease_fake.py, consolidation Lane A).

Owns the fixtures imports (the CLI environment builder and the
repo builders live in tests/fixtures/), the one in-process
lease-package load (under the name "lease_mod", registered as
the stanza registered it), the fixed clock, the CLI driver
helpers, the stateful FakeColab, and the planting helpers
several leaves share. Subject helpers used by a single leaf
live in that leaf. tests/test_extend_guard.py loads this module
by path as "lease_fake_harness" and reads lease_mod, FakeColab,
scratch_repo, and PNOW from it.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FAKE = REPO / "tests" / "fixtures" / "fake_colab.py"

sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import repo_fixture  # noqa: E402
from env_fixtures import lease_env  # noqa: E402, F401
from module_loader import load_lease  # noqa: E402

lease_mod = load_lease("lease_mod")

PNOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)

# The repo builders live in tests/fixtures/repo_fixture.py (the
# consolidation's single home); these names are the harness's
# surface for them.
scratch_repo = repo_fixture.fake_scratch_repo
gate_repo = repo_fixture.gate_repo


def lease_bin(env):
    return Path(env["TEST_REPO_ROOT"]) / "bin" / "colab-lease"


def run_lease(env, *args):
    r = subprocess.run([sys.executable, str(lease_bin(env)), *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def run_lease_rc(env, *args):
    r = subprocess.run([sys.executable, str(lease_bin(env)), *args], capture_output=True, text=True, env=env, timeout=300)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else {}


def record_files(env):
    var = Path(env["COLAB_RUNNER_VAR"])
    return sorted(p for p in var.glob("lease-*.json") if p.name not in ("lease-spend.json", "lease-request.json"))


def record_path(env):
    # Records live per session since the retirement; the suite's
    # CLI tests hold at most one lease at a time.
    files = record_files(env)
    assert len(files) == 1, files
    return files[0]


def record(env):
    return json.loads(record_path(env).read_text())


def dummy_home_env(env, tmp_path):
    """Point HOME at a scratch home with a dummy token, so the
    renewal kit's token staging never touches the real login."""
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    env["HOME"] = str(home)
    return env


class FakeColab:
    """Stateful fake of the injected colab callable: a session set
    plus a full call log."""

    def __init__(self, alive=()):
        self.alive = set(alive)
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(a)
        cmd = a[0] if a else ""
        if cmd == "usage":
            return subprocess.CompletedProcess([], 0, "balance: 500 CU (fake)", "")
        if cmd == "sessions":
            return subprocess.CompletedProcess([], 0, json.dumps(sorted(self.alive)), "")
        if cmd == "new":
            self.alive.add(a[a.index("-s") + 1])
            return subprocess.CompletedProcess([], 0, "created", "")
        if cmd == "stop":
            self.alive.discard(a[a.index("-s") + 1])
            return subprocess.CompletedProcess([], 0, "stopped", "")
        if cmd == "download":
            return subprocess.CompletedProcess([], 1, "", "")
        return subprocess.CompletedProcess([], 0, "", "")

    def verbs(self):
        return [c[0] for c in self.calls if c]


def gate_journal(repo):
    p = Path(repo) / "var" / "session-gate-journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _seed_cli_catalog(env):
    var = Path(env["COLAB_RUNNER_VAR"])
    var.mkdir(parents=True, exist_ok=True)
    (var / "cli-catalog.json").write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "gpu": ["T4"], "tpu": ["v6e1"], "no_high_mem": []}))


def _spool_kinds(repo, kind):
    p = Path(repo) / "var" / "reaper-events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip() and json.loads(line).get("kind") == kind]


def make_orphan(env, name, *, high_mem=True):
    argv = [sys.executable, str(FAKE), "new", "-s", name]
    if high_mem:
        argv.append("--high-mem")
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def plant_cli_kit_config(env, session, *, deadline, acquired_at):
    cfg = {"shape": "cpu-highram", "session": session, "deadline": deadline.isoformat(), "acquired_at": acquired_at.isoformat(), "cap_seconds": 3600, "margin_seconds": 600, "high_mem": True, "gpu": None, "tpu": None}
    (Path(env["COLAB_RUNNER_VAR"]) / f"renewal-config-{session}.json").write_text(json.dumps(cfg, indent=2))
