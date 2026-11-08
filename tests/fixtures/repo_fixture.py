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
