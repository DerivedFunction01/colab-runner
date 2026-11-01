"""Repo registry: pointer-only local data describing target repos.

The registry is local, untracked data (`config/repos.json`, seeded from
the tracked `config/repos.example.json`): which repos exist on this
machine, where each clones from, where its checkout lives locally,
where it lands on a Colab VM (`/content/<dest>`), which venv path it
declares inside its clone, how its own `init_venv.py` is invoked, which
installed versions the gate checks, and which tree roots the import
scan covers. It never carries a package list — each repo's spec lives
in that repo's `init_venv.py` and what it reads, so an entry carrying
one is rejected. Removing a repo is deleting its entry; no code names
any repo.

Remote side: each env build merges one record per built clone into
`/content/env-registry.json` on the VM (see `merge_remote_record`);
consumer jobs resolve their repo's clone dir and venv interpreter from
that file by repo id. Stdlib only.
"""

import hashlib
import json
from pathlib import Path

FORBIDDEN_ENTRY_KEYS = {
    "packages", "package_list", "pkgs", "package_spec", "spec",
    "requirements", "dependencies", "install",
}
REQUIRED_ENTRY_KEYS = {"clone_url", "local_path", "dest", "venv", "init"}


def load_local_registry(runner_root: Path) -> dict:
    """Load config/repos.json under the runner root; {} when absent."""
    p = Path(runner_root) / "config" / "repos.json"
    if not p.exists():
        return {"repos": {}}
    data = json.loads(p.read_text())
    validate_registry(data)
    return data


def validate_registry(data: dict) -> dict:
    repos = data.get("repos")
    if not isinstance(repos, dict) or not repos:
        raise ValueError("registry: 'repos' must be a non-empty object")
    for repo_id, entry in repos.items():
        validate_entry(repo_id, entry)
    default = data.get("default_repo")
    if default is not None and default not in repos:
        raise ValueError(f"registry: default_repo {default!r} is not a registered repo")
    return data


def validate_entry(repo_id: str, entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f"registry: entry {repo_id!r} must be an object")
    bad = FORBIDDEN_ENTRY_KEYS & set(entry)
    if bad:
        raise ValueError(
            f"registry: entry {repo_id!r} carries {sorted(bad)} — package specs live "
            "in the repo's own init_venv.py, never in the registry"
        )
    missing = REQUIRED_ENTRY_KEYS - set(entry)
    if missing:
        raise ValueError(f"registry: entry {repo_id!r} is missing {sorted(missing)}")
    init = entry["init"]
    if not isinstance(init, dict) or not init.get("argv"):
        raise ValueError(f"registry: entry {repo_id!r} init.argv must be a non-empty list")
    for variant, vent in (entry.get("variants") or {}).items():
        if not isinstance(vent, dict):
            raise ValueError(f"registry: variant {variant!r} of {repo_id!r} must be an object")
        vbad = FORBIDDEN_ENTRY_KEYS & set(vent)
        if vbad:
            raise ValueError(f"registry: variant {variant!r} of {repo_id!r} carries {sorted(vbad)}")
    return entry


def get_entry(registry: dict, repo_id: str | None = None) -> tuple[str, dict]:
    """Resolve (repo_id, entry): explicit id, else the default_repo.

    Refuses when neither names a registered repo — no command defaults
    to a hardcoded repo."""
    repos = registry.get("repos") or {}
    rid = repo_id or registry.get("default_repo")
    if not rid:
        raise ValueError("registry: no repo given and no default_repo registered")
    if rid not in repos:
        raise ValueError(f"registry: repo {rid!r} is not registered (have: {sorted(repos)})")
    return rid, repos[rid]


def variant_dest(entry: dict, variant: str | None) -> str:
    """Remote dest slug for a build: the entry dest, or <dest>-<variant>."""
    return entry["dest"] if not variant else f"{entry['dest']}-{variant}"


def variant_delta(entry: dict, variant: str | None) -> list:
    if not variant:
        return []
    vent = (entry.get("variants") or {}).get(variant)
    if vent is None:
        raise ValueError(f"registry: variant {variant!r} is not defined for this repo")
    return list(vent.get("delta") or [])


def parse_version(text: str) -> tuple:
    parts = []
    for piece in str(text).strip().split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _padded(a: tuple, b: tuple) -> tuple:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def version_satisfies(version: str, spec: str) -> bool:
    """Comma-AND spec of >=, <=, >, <, ==, != clauses (PEP-440-lite)."""
    v = parse_version(version)
    for clause in str(spec).split(","):
        clause = clause.strip()
        if not clause:
            continue
        op = next((o for o in ("==", "!=", ">=", "<=", ">", "<") if clause.startswith(o)), None)
        if op is None:
            raise ValueError(f"version check: unparseable clause {clause!r}")
        want = parse_version(clause[len(op):])
        a, b = _padded(v, want)
        ok = {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False
    return True


def check_versions(versions: dict, checks: dict) -> dict:
    """{package: (installed_version, spec, ok)} for every checked package."""
    out = {}
    for pkg, spec in (checks or {}).items():
        got = versions.get(pkg)
        out[pkg] = (got, spec, bool(got) and version_satisfies(got, spec))
    return out


def versions_fingerprint(versions: dict) -> str:
    canonical = json.dumps(dict(sorted(versions.items())), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def merge_remote_record(registry: dict, record: dict) -> dict:
    """Merge one built clone's record into the remote registry dict.

    Keyed by dest slug, so a second repo's (or variant's) build never
    clobbers the first; re-building the same dest replaces its record."""
    out = dict(registry or {})
    out[record["dest"]] = record
    return out


def load_remote_registry(content_dir) -> dict:
    p = Path(content_dir) / "env-registry.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def resolve_remote_repo(content_dir, repo_id: str | None = None, variant: str | None = None) -> dict:
    """Find a built clone's remote record by repo id (+ optional variant).

    With no repo id, the registry must hold exactly one record."""
    records = load_remote_registry(content_dir)
    matches = [
        rec for rec in records.values()
        if (repo_id is None or rec.get("repo") == repo_id)
        and (variant is None or rec.get("variant") == variant)
    ]
    if repo_id is None and variant is None and len(records) == 1:
        return next(iter(records.values()))
    if len(matches) != 1:
        raise ValueError(
            f"remote registry: expected exactly one record for repo={repo_id!r} "
            f"variant={variant!r}, found {len(matches)} (dests: {sorted(records)})"
        )
    return matches[0]
