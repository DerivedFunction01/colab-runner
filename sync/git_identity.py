#!/usr/bin/env python3
"""The git identity plant: a tree the machinery forms carries
the source repo's configured identity, never whatever the
forming shell happens to hold.

The defect this cures (commissioned 2026-11-12): session
trees carried no configured git identity, so a session-side
commit inherited a lane-local identity that reached GitHub
and broke a downstream deploy gate. The identity is the
source repo's OWN repo-local git config (user.name /
user.email as `git config --local` reports them) — every
local checkout here carries that pair, the email sometimes
deliberately empty (the `DL <>` convention the per-command
-c flags on this machine's landings replicate). A complete
pair is: user.name set and non-empty, and the user.email
key present (empty allowed — set-but-empty is a configured
value, distinct from unset). Anything less plants nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def repo_identity(local_path) -> "dict | None":
    """The repo-local configured pair at local_path, or None
    when either half is not configured there. Reads the
    checkout's own config only — never the global chain —
    because the plant's claim is precisely "this repo's
    identity", and a global fallback would reintroduce the
    environment inheritance being cured."""
    root = Path(local_path).expanduser()
    if not (root / ".git").exists():
        return None

    def _get(key: str) -> "str | None":
        r = subprocess.run(
            ["git", "-C", str(root), "config", "--local", "--get", key],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return None
        return r.stdout.rstrip("\n")

    name, email = _get("user.name"), _get("user.email")
    if not name or email is None:
        return None
    return {"name": name, "email": email}


def plant_payload(tree: str, name: str, email: str) -> str:
    """The session-side plant script: set both values
    repo-locally in the tree at `tree` (resolved against the
    exec's cwd, the transfer BASE idiom), read them back,
    and print one JSON object {"planted": bool, ...}. The
    readback is the verdict — a set that does not read back
    equal is not a plant."""
    return (
        "import json, subprocess\n"
        "from pathlib import Path\n"
        f"tree = Path({tree!r})\n"
        "if not (tree / '.git').exists():\n"
        "    print(json.dumps({'planted': False, 'error': f'no .git at {tree}'}))\n"
        "    raise SystemExit(0)\n"
        "def git(*a):\n"
        "    return subprocess.run(['git', '-C', str(tree), *a], capture_output=True, text=True)\n"
        f"r1 = git('config', 'user.name', {name!r})\n"
        f"r2 = git('config', 'user.email', {email!r})\n"
        "if r1.returncode != 0 or r2.returncode != 0:\n"
        "    print(json.dumps({'planted': False, 'error': (r1.stderr + r2.stderr).strip()[-300:]}))\n"
        "    raise SystemExit(0)\n"
        "name = git('config', '--local', '--get', 'user.name').stdout.rstrip('\\n')\n"
        "email = git('config', '--local', '--get', 'user.email').stdout.rstrip('\\n')\n"
        f"ok = name == {name!r} and email == {email!r}\n"
        "print(json.dumps({'planted': ok, 'name': name, 'email': email}))\n"
    )


def parse_plant_output(stdout: str) -> "dict | None":
    """The plant's verdict from an exec's stdout: the last
    line that parses as a JSON object (the real CLI's
    preamble precedes payload output — the same production
    shape the misplaced probe's parser now rides)."""
    for line in reversed(stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and "planted" in parsed:
            return parsed
    return None
