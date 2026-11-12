#!/usr/bin/env python3
"""The runner store prune (owner ruling, 2026-11-12: "Auto
prune, don't wait for me to state it — else it becomes too
late and I get locked out").

The store's growth driver is transfer staging: every seeded
launch uploads a ~109 MB snapshot bundle in 32 MB parts, and
the parts (plus the bundles themselves) accumulate in var/
long after their transfers complete. This prune reclaims,
in order:

  1. parts of COMPLETE transfers (var/*.part-*, var/chunks/*)
     — residue the moment the transfer lands;
  2. snapshot bundles at var/*.bundle (+ their manifests)
     older than the bundle age floor — every launch rebuilds
     its own from the source repo, which holds the truth;
  3. parts of failed / stale-running transfers and
     record-less parts past the resume window (24 h) — a
     failed transfer's parts are resume state, kept a day;
  4. results/<run-id>/ payload dirs of terminal runs;
  5. gate scratch bundles/patches in ~/workspace/tmp-gate.

Never touched: run and transfer RECORDS (results/runs,
results/xfers — metadata and verdicts are the evidence the
programmes cite), lease records, anything a non-terminal
run or a fresh non-terminal transfer references, anything
younger than the class floors, and anything outside the
classes above. The prune stops as soon as the free-space
target is met. --dry-run reports the plan without deleting.
Every execution appends its report to var/prune-log.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

_env_home = os.environ.get("COLAB_RUNNER_HOME")
REPO = Path(_env_home) if _env_home else Path(__file__).resolve().parent.parent
VAR = REPO / "var"
RESULTS = REPO / "results"
TMP_GATE = REPO.parent / "tmp-gate"
GB = 1024**3

BUNDLE_AGE_FLOOR_S = 2 * 3600
RESUME_WINDOW_S = 24 * 3600
FRESH_XFER_S = 3600
SCRATCH_AGE_FLOOR_S = 3600


def _epoch(iso) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _load_records() -> tuple[set, list]:
    """Protected file names from in-flight work, and the
    transfer records indexed by the names their parts carry."""
    protected: set = set()
    now = time.time()
    for path in (RESULTS / "runs").glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("state") in ("starting", "running", "polling"):
            seed = rec.get("bundle_seed") or {}
            if seed.get("bundle"):
                protected.add(Path(seed["bundle"]).name)
    xfers = []
    for path in (RESULTS / "xfers").glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rec["_stamp"] = max(_epoch(rec.get("finished_at")), _epoch(rec.get("started_at")), path.stat().st_mtime)
        xfers.append(rec)
        if rec.get("state") in ("starting", "running") and now - rec["_stamp"] < FRESH_XFER_S:
            for key in ("remote_name", "local_path"):
                if rec.get(key):
                    protected.add(Path(rec[key]).name)
    return protected, xfers


def _part_base(name: str) -> str:
    """sp-x.bundle.part-000 -> sp-x.bundle (also .part-0)."""
    stem, dot, tail = name.rpartition(".part-")
    return stem if dot and tail.isdigit() else name


def plan(target_free_gb: float) -> tuple[list, dict]:
    now = time.time()
    protected, xfers = _load_records()
    by_base: dict = {}
    for rec in xfers:
        for key in ("remote_name", "local_path"):
            if rec.get(key):
                by_base.setdefault(Path(rec[key]).name, []).append(rec)

    def part_class(path: Path) -> "tuple[int, str] | None":
        base = _part_base(path.name)
        if base in protected or path.name in protected:
            return None
        recs = by_base.get(base, [])
        age = now - path.stat().st_mtime
        if recs:
            newest = max(recs, key=lambda r: r["_stamp"])
            if newest.get("state") == "complete":
                return (1, "parts of complete transfers")
            if now - newest["_stamp"] > RESUME_WINDOW_S and age > RESUME_WINDOW_S:
                return (3, "parts past the resume window")
            return None
        # No record: the runner's own internal transfers
        # (seed uploads) write no xfer record — their parts
        # are re-staged from the source file by any later
        # launch, so past the age floor they are pure residue.
        if age > BUNDLE_AGE_FLOOR_S:
            return (2, "unrecorded transfer staging past the age floor")
        return None

    candidates: list = []  # (order, path, bytes, class)

    def add(order: int, path: Path, cls: str):
        try:
            size = path.stat().st_size if path.is_file() else sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            return
        candidates.append((order, path, size, cls))

    for part in list(VAR.glob("*.part-*")) + list((VAR / "chunks").glob("*")):
        if not part.is_file():
            continue
        verdict = part_class(part)
        if verdict:
            add(verdict[0], part, verdict[1])
    for bundle in VAR.glob("*.bundle"):
        if bundle.name in protected:
            continue
        if now - bundle.stat().st_mtime > BUNDLE_AGE_FLOOR_S:
            add(2, bundle, "snapshot bundles past the age floor")
            manifest = Path(str(bundle) + ".manifest.json")
            if manifest.exists():
                add(2, manifest, "snapshot bundles past the age floor")
    terminal_dirs = set()
    for path in (RESULTS / "runs").glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("state") in ("complete", "failed", "orphaned"):
            terminal_dirs.add(path.stem)
    for entry in RESULTS.iterdir():
        if entry.is_dir() and entry.name not in ("runs", "xfers"):
            if entry.name in terminal_dirs or now - entry.stat().st_mtime > RESUME_WINDOW_S:
                add(4, entry, "payload dirs of terminal runs")
    if TMP_GATE.exists():
        for scratch in list(TMP_GATE.glob("*.bundle")) + list(TMP_GATE.glob("*.patch")):
            if now - scratch.stat().st_mtime > SCRATCH_AGE_FLOOR_S:
                add(5, scratch, "gate scratch")

    candidates.sort(key=lambda c: (c[0], -c[2]))
    free_now = shutil.disk_usage(REPO).free
    chosen, projected = [], free_now
    for order, path, size, cls in candidates:
        if projected >= target_free_gb * GB:
            break
        chosen.append((order, path, size, cls))
        projected += size
    summary: dict = {}
    for _o, _p, size, cls in chosen:
        summary[cls] = summary.get(cls, {"files": 0, "bytes": 0})
        summary[cls]["files"] += 1
        summary[cls]["bytes"] += size
    return chosen, {"free_before": free_now, "projected_free": projected, "classes": summary}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-free-gb", type=float, default=60.0)
    ap.add_argument("--if-below-gb", type=float, default=None, help="standing mode: exit 0 doing nothing unless free space is below this")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    free_now = shutil.disk_usage(REPO).free
    if args.if_below_gb is not None and free_now >= args.if_below_gb * GB:
        # Standing mode stays silent above the threshold:
        # the scheduler entry reports on output, so a pass
        # that prunes nothing must print nothing.
        return 0
    chosen, report = plan(args.target_free_gb)
    reclaimed = 0
    if not args.dry_run:
        for _o, path, size, _cls in chosen:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                reclaimed += size
            except OSError:
                continue
    report.update(
        {
            "pruned": not args.dry_run and bool(chosen),
            "dry_run": args.dry_run,
            "files": len(chosen),
            "reclaimed_bytes": reclaimed if not args.dry_run else 0,
            "free_after": shutil.disk_usage(REPO).free,
            "target_free_gb": args.target_free_gb,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    print(json.dumps(report, indent=2))
    if not args.dry_run and chosen:
        with (VAR / "prune-log.jsonl").open("a") as fh:
            fh.write(json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
