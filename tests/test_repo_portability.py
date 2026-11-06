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

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).name
FORBIDDEN = ["Zqx" + "jkv", "/home/" + "zqx", "~/" + "workspace"]
WORKLOAD_LITERAL = "edg" + "ar"  # the default workload's profile id, assembled like FORBIDDEN
CODE_DIRS = ("sync/", "bin/", "ui/", "probes/")
CODE_SUFFIXES = {".py", ".sh", ".js"}


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


def test_profiles_config_is_retired():
    """Profiles were retired (2026-11-05): no tracked example seeds
    them any more. A leftover local config/profiles.json may exist
    as the legacy rate source for pre-retirement records, but it
    stays gitignored and untracked like every local config."""
    names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    # The working tree is the governed surface: a tracked seed
    # would be present in it (the index catches up at landing).
    assert not (REPO / "config" / "profiles.example.json").exists()
    assert "config/profiles.json" not in names
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "config/profiles.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0


def test_no_workload_literal_in_executable_code():
    """The retired default workload's profile id must not appear
    as a literal in executable code (2026-11-03 hardcode strip):
    code resolves the default shape only from config/lease.json's
    `default_shape` key, and session names are generated, never
    derived from a workload.

    Scope: only files that are BOTH tracked and hand-authored
    executable code — `git ls-files` supplies the tracked set, so
    untracked files and machine-generated files are the exclusion
    (they are never listed, never scanned, never edited for
    this). Within sync/, bin/, ui/ and probes/, only code files
    are scanned (.py/.sh/.js and the extensionless bin
    executables): docs, markup, stylesheets, job definitions and
    fixtures are data and out of scope, and
    the config files — outside these directories anyway — are the
    sanctioned seams that name shapes and callers. The literal
    is matched as a standalone token, so the workload repo's own
    name (with a hyphenated suffix) and the SEC system's name
    are not the profile id."""
    token = re.compile(r"(?<![\w-])" + WORKLOAD_LITERAL + r"(?![\w-])")
    names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for name in names:
        if not name.startswith(CODE_DIRS):
            continue
        path = REPO / name
        if path.name == SELF:
            continue
        if path.suffix not in CODE_SUFFIXES and path.suffix != "":
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if token.search(text):
            offenders.append(name)
    assert not offenders, "\n".join(offenders)


# The tracked-files scan has no exemptions: no tracked file's
# function requires naming the workload. Tests assert absence
# with fake stand-in literals (the real words in a test would be
# the very leakage the guard exists to prevent), and consumer
# payload snippets resolve the workload's package from the
# remote env registry's record — the same seam that already
# resolves their clone and interpreter — never from a literal.
WORKLOAD_SCAN_EXEMPT = set()


def test_no_workload_literal_in_tracked_files():
    """The workload literal appears nowhere in tracked files
    (2026-11-06 purge, no exemptions): the token scan above
    governs executable code, and this scan closes the gap it
    left — docs, tests, and prose drifted because nothing
    scanned them. Case-insensitive substring: the repo is
    workload-agnostic, so the workload's name in any form or
    file is a defect, full stop."""
    names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for name in names:
        if name in WORKLOAD_SCAN_EXEMPT or Path(name).name == SELF:
            continue
        try:
            text = (REPO / name).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if WORKLOAD_LITERAL in text.lower():
            offenders.append(name)
    assert not offenders, "\n".join(offenders)
