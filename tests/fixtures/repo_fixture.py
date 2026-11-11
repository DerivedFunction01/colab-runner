"""Authored fixture repos for the lease test suites.

The suites must pass in a bare fresh clone: no test may depend on
this workspace's untracked operator files (config/lease.json,
config/callers.json, config/repos.json) being present in the
checkout it runs from. Every fixture repo below is built from
values authored HERE, in the test tree — never read from, copied
from, or templated on the live operator config.

Two shapes:
- scratch_repo(root): an in-process repo — fixture config +
  caller registry at root/config, measured-rate table in
  root/var, the checkout's snippets symlinked in (code only).
- cli_repo(root, var): a subprocess repo — the checkout's bin/
  scripts COPIED in (bin scripts derive their repo root from
  their own file location, resolved, so a symlinked bin would
  resolve back to the checkout), sync/snippets/ops/jobs
  symlinked in (code and tracked manifests, read-only), fixture
  config at root/config, and the measured-rate table in the
  var dir the caller points COLAB_RUNNER_VAR at.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent.parent

# Authored fixture values. Rates mirror the measured-rate table
# the in-process harness in test_lease_fake seeds (cpu at the
# Standard rate, cpu-highram at the High-RAM rate); caps are the
# suite's own round numbers, chosen so the priced windows the
# tests assert (0.08/h cpu, 0.26/h cpu-highram) fit or refuse
# exactly where the tests say they do.
FIXTURE_DAILY_CAP = 15
FIXTURE_MONTHLY_CAP = 150
FIXTURE_STANDING_CAP = 4
FIXTURE_RATES = {"cpu": 0.08, "cpu-highram": 0.26}
FIXTURE_CALLERS = ["alpha", "beta"]


def fixture_config():
    return {
        "enabled": True,
        "daily_cu_budget": FIXTURE_DAILY_CAP,
        "monthly_cu_budget": FIXTURE_MONTHLY_CAP,
        "max_hours": 8,
        "rate_cu_per_hour": {"standard": 0.08, "high_mem": 0.26},
        "keepalive": {"enabled": True, "interval_seconds": 60, "pulse_seconds": 600, "stale_seconds": 600},
        "standing": {"caller": "standing", "shape": "cpu", "daily_cu_cap": FIXTURE_STANDING_CAP, "max_session_hours": 12, "warm_on_renew": True},
        # The gate enforces, with the suite's test actor claim
        # whitelisted as a bypass (the door the CLI harnesses use);
        # gate-specific tests pop the claim to run as a plain agent.
        "session_gate": {"enabled": True, "bypass_actors": ["panel", "test"], "agent_classes": ["high_ram"], "infra_actor": "standing"},
    }


def write_config(root: Path) -> Path:
    cfg_dir = Path(root) / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "lease.json").write_text(json.dumps(fixture_config(), sort_keys=True, separators=(",", ":")))
    (cfg_dir / "callers.json").write_text(json.dumps({"callers": list(FIXTURE_CALLERS)}))
    return Path(root)


def seed_rates(var: Path) -> Path:
    var = Path(var)
    var.mkdir(parents=True, exist_ok=True)
    (var / "shape-rates.json").write_text(json.dumps(FIXTURE_RATES))
    return var


def scratch_repo(root: Path) -> Path:
    """An in-process fixture repo at root (root doubles as its
    var dir's parent: var state lands in root/var)."""
    root = Path(root)
    write_config(root)
    seed_rates(root / "var")
    snippets = root / "snippets"
    if not snippets.exists():
        snippets.symlink_to(CHECKOUT / "snippets")
    return root


def cli_repo(root: Path, var: Path) -> Path:
    """A subprocess fixture repo at root: the bin scripts copied
    in so their resolved repo root is the fixture, code dirs
    symlinked, fixture config written, rates seeded into var."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    bin_dir = root / "bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    shutil.copytree(CHECKOUT / "bin", bin_dir, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("sync", "snippets", "ops", "jobs"):
        link = root / name
        if not link.exists():
            link.symlink_to(CHECKOUT / name)
    write_config(root)
    seed_rates(var)
    return root


# --- Parameterised variants consolidated from the test files ----------
# Each builder below lived in exactly one test file before the
# consolidation; they are variants of the two shapes above,
# differing in the config they author (permissive vs enforcing
# gate) or in what the harness around them needs planted.


def lease_repo(tmp_path, monkeypatch):
    """The unit suites' `repo` fixture body: a scratch repo
    carrying the authored fixture config (the suite must pass in
    a bare clone, where the checkout has no operator config),
    with COLAB_RUNNER_VAR pointed at its var dir."""
    root = scratch_repo(tmp_path)
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    return root


def fake_scratch_repo(tmp_path, monkeypatch, daily_cap=15):
    """The lease fake harness's in-process repo: permissive
    session-gate fixture state (the general suite predates the
    gate; its own tests run under gate_repo), the measured-rate
    table seeding cpu at the Standard rate and cpu-highram at
    the High-RAM rate, and the checkout's snippets symlinked in."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # The measured-rate table seeds the shape rates: cpu at the
    # Standard rate, cpu-highram at the High-RAM rate.
    var_dir = tmp_path / "var"
    var_dir.mkdir(parents=True, exist_ok=True)
    (var_dir / "shape-rates.json").write_text(json.dumps({"cpu": 0.08, "cpu-highram": 0.26}))
    (cfg_dir / "lease.json").write_text(json.dumps({
        "enabled": True, "daily_cu_budget": daily_cap, "monthly_cu_budget": 150, "max_hours": 8,
        "keepalive": {"enabled": True, "interval_seconds": 60, "pulse_seconds": 600, "stale_seconds": 600},
        # Permissive fixture state for the session shape gate: the
        # general suite predates it; the gate's own tests below run
        # under an enforcing fixture (gate_repo).
        "session_gate": {"enabled": False},
    }, sort_keys=True, separators=(",", ":")))
    (tmp_path / "snippets").symlink_to(CHECKOUT / "snippets")
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    # A scratch HOME keeps the local sessions binding (which the
    # post-start shape assertion reads) out of these in-process
    # tests: the real binding belongs to live sessions.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def gate_repo(tmp_path, monkeypatch, gate=None):
    """The session shape gate's enforcing fixture repo (see
    fake_scratch_repo for the permissive counterpart)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "lease.json").write_text(json.dumps({
        "enabled": True, "daily_cu_budget": 15, "monthly_cu_budget": 150, "max_hours": 8,
        "keepalive": {"enabled": True, "interval_seconds": 60, "pulse_seconds": 600, "stale_seconds": 600},
        "standing": {"caller": "standing", "shape": "cpu", "daily_cu_cap": 4, "max_session_hours": 12},
        "session_gate": gate if gate is not None else {"enabled": True, "bypass_actors": ["panel", "test"], "agent_classes": ["high_ram"], "infra_actor": "standing"},
    }, sort_keys=True, separators=(",", ":")))
    # Caller attribution: the fixture's own scratch registry (fictional
    # thread names only — the machine's local registry never leaks in).
    (cfg_dir / "callers.json").write_text(json.dumps({"callers": ["alpha", "beta"]}))
    (tmp_path / "snippets").symlink_to(CHECKOUT / "snippets")
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    # The scratch catalog cache: the CLI space this fixture serves
    # (a T4 GPU shape beyond the always-served CPU shapes).
    var = tmp_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    (var / "cli-catalog.json").write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "gpu": ["T4"], "tpu": ["v6e1"], "no_high_mem": []}))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COLAB_LEASE_ACTOR", raising=False)
    return tmp_path


def bridge_repo(tmp_path):
    """The bridge core suite's repo: a minimal standing config,
    the measured rates, and snippet stand-ins (the duty reads
    their bytes, never executes them)."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "var").mkdir(parents=True)
    (repo / "snippets").mkdir(parents=True)
    (repo / "config" / "lease.json").write_text(json.dumps({"enabled": True, "standing": {"caller": "standing", "shape": "cpu"}}))
    (repo / "var" / "shape-rates.json").write_text(json.dumps({"cpu": 0.08, "cpu-highram": 0.26}))
    for name in ("bridge_agent.py", "_remote_guard.py"):
        (repo / "snippets" / name).write_text("# test stand-in\n")
    return repo
