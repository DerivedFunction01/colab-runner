"""The store prune's protections (owner ruling 2026-11-12):
records are never candidates, work in flight keeps its
staging, a complete transfer's parts and an old snapshot
bundle are the first reclaim classes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "ops" / "store_prune.py"


def _sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "runner"
    (root / "var" / "chunks").mkdir(parents=True)
    (root / "results" / "runs").mkdir(parents=True)
    (root / "results" / "xfers").mkdir(parents=True)
    return root


def _write(path: Path, data: bytes = b"x" * 64, age_s: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    return path


def _record(root: Path, kind: str, name: str, rec: dict) -> None:
    (root / "results" / kind / name).write_text(json.dumps(rec))


def _plan_paths(root: Path) -> set:
    import importlib.util

    os.environ["COLAB_RUNNER_HOME"] = str(root)
    try:
        spec = importlib.util.spec_from_file_location("store_prune_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        chosen, _report = module.plan(1e15)
        return {str(p.relative_to(root)) for _o, p, _s, _c in chosen}
    finally:
        os.environ.pop("COLAB_RUNNER_HOME", None)


def test_records_and_in_flight_work_are_never_candidates(tmp_path):
    root = _sandbox(tmp_path)
    _record(root, "runs", "run-live.json", {"state": "polling", "session": "s1"})
    _record(root, "runs", "run-done.json", {"state": "complete", "session": "s1", "last_evidence_at": "2026-11-09T00:00:00+00:00"})
    done_rec = root / "results" / "runs" / "run-done.json"
    old = time.time() - 3 * 3600
    os.utime(done_rec, (old, old))  # terminal AND cold: past the output grace
    _record(root, "runs", "run-fresh.json", {"state": "complete", "session": "s1"})  # terminal but fresh: a collector may be mid-verdict
    _record(root, "xfers", "up-1.json", {
        "state": "complete", "remote_name": "done.bundle",
        "started_at": "2026-11-12T00:00:00+00:00", "finished_at": "2026-11-12T00:01:00+00:00",
    })
    _write(root / "var" / "done.bundle.part-000")            # complete transfer: prunable
    _write(root / "var" / "old.bundle", age_s=3 * 3600)       # old snapshot: prunable
    _write(root / "var" / "fresh.bundle", age_s=60)           # under the floor: kept
    _write(root / "var" / "stray.bundle.part-000", age_s=60)  # fresh staging: kept
    _write(root / "results" / "run-live" / "out.bin")         # in flight: kept
    _write(root / "results" / "run-done" / "out.bin")         # terminal + cold payload: prunable
    _write(root / "results" / "run-fresh" / "out.bin")        # terminal but inside the grace: kept
    chosen = _plan_paths(root)
    assert "var/done.bundle.part-000" in chosen
    assert "var/old.bundle" in chosen
    assert "results/run-done" in chosen
    assert "results/run-fresh" not in chosen, "the prune swept a fresh terminal run's outputs (the M1 mid-collection specimen)"
    assert "var/fresh.bundle" not in chosen
    assert "var/stray.bundle.part-000" not in chosen
    assert "results/run-live" not in chosen
    assert not any(c.startswith("results/runs") or c.startswith("results/xfers") for c in chosen)
