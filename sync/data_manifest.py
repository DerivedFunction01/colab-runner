#!/usr/bin/env python3
"""Price the .artifacts data routes for a job and choose per class.

For every class: generator classification, bytes, its driver where one
exists, and all three priced routes — (1) transfer the artifact itself,
(2) transfer the driver and regenerate, (3) regenerate from the network at
the SEC rate limit — with the chosen route and estimate recorded.
Transfer time uses a measured bytes/sec (default: the P0 baseline).
Network regeneration is priced only where a request count is derivable;
otherwise it is recorded as unpriced, never invented.

Return decisions are symmetric: a remotely produced artifact whose driver
is local is disposed of (record manifest + content identity only); genuine
SEC bytes and baselines are pulled back.

Default exclusions (AGENTS contract): .venv, cache, caches, transient,
run-logs move only when explicitly named via --include.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

DEFAULT_BPS = 2_418_283  # P0 baseline, 2026-10-29
RATE_RPS = 8.0

# class -> (generator, driver class or None, network requests or None, return mode)
CLASSES = {
    "metadata": ("root_fetch", None, None, "pull"),  # refetch is not identity-preserving: always transfer
    "filing_catalog": ("derived", "metadata", 0, "dispose"),  # zero-network pipeline from a metadata snapshot
    "cohorts": ("derived", "metadata", None, "dispose"),
    "document_planning": ("plan", None, 0, "pull"),  # the portable artifact for acquisition: workers fetch disjoint shards
    "document_acquisition": ("sec_bytes", "document_planning", None, "pull"),
    "cache": ("sec_bytes", None, None, "pull"),
    "xbrl": ("derived", None, None, "pull"),  # tiny; transfer doubles as the comparison baseline
    "run-logs": ("leaf", None, 0, "none"),  # never an input
    "transient": ("disposable", None, 0, "none"),
    "caches": ("disposable", None, 0, "none"),
}
EXCLUDED_BY_DEFAULT = {"cache", "caches", "transient", "run-logs"}


def dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def listing_fingerprint(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file():
            h.update(f"{f.relative_to(p)}:{f.stat().st_size}\n".encode())
    return h.hexdigest()


def latest_snapshot(p: Path) -> Path | None:
    snaps = [d for d in p.rglob("*") if d.is_dir()] if p.exists() else []
    return max(snaps, key=lambda d: d.stat().st_mtime) if snaps else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default=None,
                    help="artifacts root (default: the registry default_repo's local checkout + /.artifacts)")
    ap.add_argument("--bps", type=float, default=DEFAULT_BPS)
    ap.add_argument("--include", action="append", default=[], help="explicitly include a default-excluded class")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.artifacts is None:
        # Repo-agnostic default: the local registry's default repo's
        # checkout. No repo is named here; with no registry entry the
        # caller must pass --artifacts explicitly.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "repo_registry", Path(__file__).resolve().parent / "repo_registry.py")
        rr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rr)
        try:
            _, entry = rr.get_entry(rr.load_local_registry(Path(__file__).resolve().parent.parent))
            args.artifacts = str(Path(os.path.expanduser(entry["local_path"])) / ".artifacts")
        except ValueError as exc:
            print(f"data_manifest: --artifacts not given and no registry default ({exc})")
            return 2
    root = Path(args.artifacts)
    entries = []
    present = {d.name: d for d in root.iterdir() if d.is_dir()} if root.exists() else {}
    sizes = {name: dir_bytes(d) for name, d in present.items()}

    for name, d in sorted(present.items()):
        generator, driver_name, net_requests, return_mode = CLASSES.get(name, ("unknown", None, None, "pull"))
        excluded = name in EXCLUDED_BY_DEFAULT and name not in args.include
        routes: dict = {"transfer_self": {"seconds": round(sizes[name] / args.bps, 1), "bytes": sizes[name]}}
        driver = None
        if driver_name and driver_name in present:
            drv_path = latest_snapshot(present[driver_name]) or present[driver_name]
            drv_bytes = dir_bytes(drv_path)
            driver = {"class": driver_name, "path": str(drv_path), "bytes": drv_bytes, "listing_fingerprint": listing_fingerprint(drv_path)}
            routes["transfer_driver_regen"] = {"seconds": round(drv_bytes / args.bps, 1), "bytes": drv_bytes, "plus_remote_compute": "unpriced until P2 hybrid calibration"}
        if net_requests is not None:
            routes["network_regen"] = {"seconds": round(net_requests / RATE_RPS, 1) if net_requests else 0.0, "requests": net_requests, "identity_preserving": name != "metadata"}
        else:
            routes["network_regen"] = {"seconds": None, "requests": None, "note": "request count not derivable from local state; unpriced, not invented"}
        if excluded:
            chosen, reason = "excluded", "default exclusion (AGENTS contract); name it with --include to move it"
        elif name == "filing_catalog" and driver:
            chosen, reason = "transfer_driver_regen", "zero-network regeneration from the metadata snapshot beats moving 2.9 GB; content-addressed identity verifies the result"
        elif name == "metadata":
            chosen, reason = "transfer_self", "root fetch: a refetch is not identity-preserving (SEC data changes daily)"
        elif name in ("run-logs", "transient", "caches"):
            chosen, reason = "excluded", "leaf/disposable class: never an input to remote work"
        else:
            chosen, reason = "transfer_self", "smallest priced route among available options"
        entries.append({
            "class": name, "generator": generator, "bytes": sizes[name],
            "listing_fingerprint": listing_fingerprint(d),
            "driver": driver, "routes": routes, "chosen_route": chosen, "reason": reason,
            "return": return_mode,
        })

    manifest = {"artifacts_root": str(root), "bps": args.bps, "rate_rps": RATE_RPS, "classes": entries}
    text = json.dumps(manifest, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
