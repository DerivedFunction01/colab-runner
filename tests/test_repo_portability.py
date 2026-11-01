"""Repo portability guard (the operator's standing rule, applied to
this repo 2026-11-01): no tracked file carries the owner's name,
absolute home paths, or home-relative workspace paths — files must
read correctly cloned anywhere. Paths resolve repo-relative or via
documented env vars (LEASE_UI_RUNTIME_DIR for the UI deploy, the
repos registry for workload checkouts). The Colab runtime's own
/content paths are the product's target environment, not machine
paths, and are out of scope here. This file is exempt from its own
scan: it has to name the patterns to forbid them (the same reason
tests/test_env_registry_unit.py carries its forbidden literals).
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).name
FORBIDDEN = ["Zqx" + "jkv", "/home/" + "zqx", "~/" + "workspace"]


def test_tracked_files_are_portable():
    names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for name in names:
        if Path(name).name == SELF:
            continue
        path = REPO / name
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in FORBIDDEN:
            if pattern in text:
                offenders.append(f"{name}: contains {pattern!r}")
    assert not offenders, "\n".join(offenders)
