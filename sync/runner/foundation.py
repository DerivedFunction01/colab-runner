#!/usr/bin/env python3
"""Runner foundation: repo roots, the module handles, the funnel.

Every runner leaf builds on these: REPO/RUNS (derived from this
file's location, one level deeper than bin/ was for the
monolith — the root it lands on is the same), the sync/cli.py funnel
binding, the lease / repo-registry / transfer handles loaded
through bin/_load.py (the one bin loader), and the two helpers
with no seam of their own (now, mirror).
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# The repo root is the path as loaded, never resolved: the
# test fixtures symlink the checkout's sync/ tree into a
# scratch repo whose copied bin/ keeps the root in the
# fixture (repo_fixture.cli_repo's contract), and resolving
# here would follow that symlink back to the checkout —
# the subprocess would read the checkout's (absent) operator
# config and the session gate would fail closed against the
# fixture's authored policy.
REPO = Path(__file__).parent.parent.parent
RUNS = REPO / "results" / "runs"

sys.path.insert(0, str(REPO / "bin"))
import _load

cli = _load.sync("cli")
lease = _load.sync("lease")
repo_registry = _load.sync("repo_registry")
transfer = _load.sync("transfer")

# The funnel, re-exported under the names this module's consumers
# (bin/colab-lease & friends historically, the test suites) use.
colab = cli.colab
colab_bin = cli.colab_bin
colab_env = cli.colab_env
TOKEN = cli.TOKEN


def mirror(op: str) -> None:
    subprocess.run([sys.executable, str(REPO / "ops" / "mirror_transient.py"), op], capture_output=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
