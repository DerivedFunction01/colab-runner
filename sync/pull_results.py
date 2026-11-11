#!/usr/bin/env python3
"""Verify downloaded results against their manifest before trusting them.

A result manifest lists {path, sha256, bytes} per file, as produced on the
VM. Every downloaded file must match on size and SHA-256; a mismatch is a
hard rejection (exit 1) — a corrupted download is never accepted silently.
With --session, files are first fetched with `colab download`.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("dest", help="directory holding (or to hold) the downloaded files")
    ap.add_argument("--session", help="if set, download each file from this colab session first")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    cli = None
    if args.session:
        # The CLI funnel is sync/cli.py's, the one implementation
        # every consumer loads (consolidation Lane B step 5; this
        # script used to carry its own inline resolution). Loaded
        # only on the --session path, so manifest verification
        # against already-downloaded files never touches it.
        spec = importlib.util.spec_from_file_location("cli", Path(__file__).resolve().parent / "cli.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

    results, bad = [], False
    for entry in manifest["files"]:
        local = dest / Path(entry["path"]).name
        if cli:
            r = cli.colab("download", "-s", args.session, entry["path"], str(local))
            if r.returncode != 0:
                results.append({"path": entry["path"], "ok": False, "error": "download failed"})
                bad = True
                continue
        if not local.exists():
            results.append({"path": entry["path"], "ok": False, "error": "missing"})
            bad = True
            continue
        ok = local.stat().st_size == entry["bytes"] and sha256(local) == entry["sha256"]
        results.append({"path": entry["path"], "ok": ok, "bytes": local.stat().st_size})
        bad |= not ok
    print(json.dumps({"verified": not bad, "files": results}, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
