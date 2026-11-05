#!/usr/bin/env python3
"""Apply a snapshot bundle inside a base clone (runs on the Colab VM, or
locally against a scratch clone for verification).

Verifies the bundle's SHA-256 against the manifest BEFORE touching git,
fetches the snapshot commit, fast-forwards, and prints the resulting tree
hash, which must equal the manifest's tree.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("repo")
    ap.add_argument("--manifest", help="default: <bundle>.manifest.json")
    ap.add_argument("--origin-url", help="recorded as the seeded repo's origin (seed mode only)")
    args = ap.parse_args()

    bundle, repo = Path(args.bundle).resolve(), Path(args.repo).resolve()
    manifest = json.loads(Path(args.manifest or str(bundle) + ".manifest.json").read_text())
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        print(json.dumps({"applied": False, "error": "bundle sha256 mismatch", "expected": manifest["sha256"], "got": digest}))
        return 1
    if not (repo / ".git").exists():
        # Seed mode: no base clone exists. A private repo's plain
        # HTTPS clone cannot authenticate on the VM (no credential
        # lives there by design), so the bundle is the code route:
        # init the repo, fetch the snapshot, and put main on it.
        # The tree is verified against the manifest exactly as the
        # apply path verifies it.
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
        if args.origin_url:
            git(repo, "remote", "add", "origin", args.origin_url)
        git(repo, "fetch", str(bundle), manifest["snapshot_commit"])
        git(repo, "checkout", "-B", "main", "FETCH_HEAD")
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        print(json.dumps({"applied": True, "seeded": True, "head": git(repo, "rev-parse", "HEAD"), "tree": tree, "tree_matches_manifest": tree == manifest["tree"]}))
        return 0 if tree == manifest["tree"] else 1
    git(repo, "fetch", str(bundle), manifest["snapshot_commit"])
    # The base clone's main may have moved since the bundle was built
    # (parallel work pushing to origin) or sit on a sibling commit, which
    # makes the ff-only merge refuse as "diverging". The clone is a
    # scratch copy whose only job is to become the snapshot tree, so pin
    # it to the snapshot's parent first — the parent object is present
    # after the fetch — then fast-forward.
    git(repo, "reset", "--hard", "FETCH_HEAD^")
    git(repo, "merge", "--ff-only", "FETCH_HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    print(json.dumps({"applied": True, "head": git(repo, "rev-parse", "HEAD"), "tree": tree, "tree_matches_manifest": tree == manifest["tree"]}))
    return 0 if tree == manifest["tree"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
