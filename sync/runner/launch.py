#!/usr/bin/env python3
"""The launch path's front end: verified pre-launch uploads,
and the orphan notes an acquire refusal prints (E1, runner
half) so a refusal names the state that may have caused it.
"""

import json
import os
import subprocess
from pathlib import Path

from .foundation import REPO, colab, lease, transfer

# Pre-launch uploads above this size move through the chunked
# transfer (which verifies the whole-file hash) instead of the
# plain upload: the single-file ceiling the fs tools enforce.
UPLOAD_CHUNK_THRESHOLD_BYTES = 50 * 1024 * 1024


def upload_one(session: str, src: Path, dest_name: str, tag: str) -> str | None:
    """One pre-launch upload, verified before the job starts.
    A failed or short transfer is an error HERE — never a
    missing file discovered inside the job later (2026-11-08:
    a ~100 MB snapshot bundle's plain upload failed with its
    return code unchecked and the run proceeded without it).
    Large files move chunked (hash-verified by the transfer);
    smaller files move plain and are verified by remote size
    through a probe exec in the session's working directory,
    the reader's own model. Returns an error string, or None
    on a verified landing."""
    # Every outcome is journaled as launch-channel health
    # (sync/lease/health.py): adoption ranks sessions by it.
    # Every call is timeout-guarded: colab() re-raises
    # TimeoutExpired by design, and an unguarded timeout here
    # killed the launcher by exception and stranded the run's
    # record (2026-11-11: five consecutive consol launches died
    # at this probe) — a timeout is an upload failure with an
    # error string, never a dead launcher.
    if src.stat().st_size > UPLOAD_CHUNK_THRESHOLD_BYTES:
        up = transfer.chunked_upload(session, src, dest_name)
        lease.append_launch_outcome(REPO, session, "upload", bool(up["ok"]), dest_name)
        if not up["ok"]:
            return f"chunked upload failed: {json.dumps(up)[:300]}"
        return None
    try:
        r = colab("upload", "-s", session, str(src), f"/content/{dest_name}", timeout=600)
    except subprocess.TimeoutExpired:
        lease.append_launch_outcome(REPO, session, "upload", False, f"{dest_name}: upload timed out")
        return f"upload timed out for {dest_name}"
    if r.returncode != 0:
        lease.append_launch_outcome(REPO, session, "upload", False, dest_name)
        return f"upload failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}"
    probe = REPO / "var" / f"upload-probe-{tag}.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(f"import os, json\np = {dest_name!r}\nprint(json.dumps({{'size': os.path.getsize(p) if os.path.exists(p) else None}}))\n")
    # The timeout is env-tunable for the reason the poll loop's
    # is: tests shrink it (COLAB_RUN_POLL_TIMEOUT_S precedent).
    probe_timeout = int(os.environ.get("COLAB_RUN_PROBE_TIMEOUT_S", "300"))
    try:
        pr = colab("exec", "-s", session, "--timeout", "120", "-f", str(probe), timeout=probe_timeout)
    except subprocess.TimeoutExpired:
        lease.append_launch_outcome(REPO, session, "probe", False, f"{dest_name}: verify probe timed out")
        return f"upload verify timed out for {dest_name}"
    try:
        size = json.loads(pr.stdout[pr.stdout.index("{"):])["size"]
    except (ValueError, KeyError):
        lease.append_launch_outcome(REPO, session, "probe", False, dest_name)
        return f"upload verify unreadable for {dest_name}: {pr.stdout.strip()[-200:]}"
    if size != src.stat().st_size:
        lease.append_launch_outcome(REPO, session, "probe", False, dest_name)
        return f"upload verify: {dest_name} landed at {size} bytes, expected {src.stat().st_size}"
    lease.append_launch_outcome(REPO, session, "probe", True, dest_name)
    return None


def orphan_notes(shape_id: str, shape: dict) -> list[str]:
    """E1, runner half — failure path only: after an acquire
    refusal, name any live-but-unrecorded sessions at the
    requested shape and print the verb that restores them, the
    refusal-prints-the-alternative pattern. Costs one live-list
    read, so it never runs on a hot path; never fatal — a nicety
    must not mask the refusal it annotates."""
    try:
        live = lease.live_sessions(colab, REPO)
        if not live:
            return []
        recorded = {r.get("session") for r in lease.all_records(REPO)}
        want_class = "high_ram" if (shape or {}).get("high_mem") else "standard"
        notes = []
        for entry in live:
            name = entry["session"]
            if name in recorded or entry.get("shape_class") not in (None, want_class):
                continue
            notes.append(f"colab-run: note: session {name} is live but has no lease record (orphaned) — restore it with: bin/colab-lease adopt {name}")
        return notes
    except Exception:  # noqa: BLE001
        return []
