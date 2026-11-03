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


def test_profiles_config_is_local_and_seeded_from_tracked_example():
    """The workspace's named profiles are local data, following the
    repos-registry split: the actual config/profiles.json is
    gitignored and untracked, while a fictional, schema-complete
    config/profiles.example.json is tracked as the seed."""
    names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    assert "config/profiles.example.json" in names
    assert "config/profiles.json" not in names
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "config/profiles.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0

    example = json.loads((REPO / "config" / "profiles.example.json").read_text())
    profiles = example.get("profiles", {})
    assert profiles
    assert example.get("default_profile") in profiles
    assert all(profile.get("rate_cu_per_hour") is None for profile in profiles.values())
    example_text = json.dumps(example)
    assert WORKLOAD_LITERAL not in example_text
    assert "pay" + "gate" not in example_text


def test_no_workload_literal_in_executable_code():
    """The default workload's profile id must not appear as a
    literal in executable code (2026-11-03 hardcode strip):
    code resolves the default profile only from
    config/profiles.json's `default_profile` and fails loudly
    when the key is absent, and any session creation derives its
    names from config.

    Scope: only files that are BOTH tracked and hand-authored
    executable code — `git ls-files` supplies the tracked set, so
    untracked files and machine-generated files are the exclusion
    (they are never listed, never scanned, never edited for
    this). Within sync/, bin/, ui/ and probes/, only code files
    are scanned (.py/.sh/.js and the extensionless bin
    executables): docs, markup, stylesheets, job definitions and
    fixtures are data and out of scope, and
    the local config/profiles.json — outside these directories
    anyway — is the sanctioned seam that names the workload. The literal
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
