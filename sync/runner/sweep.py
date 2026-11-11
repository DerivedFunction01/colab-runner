#!/usr/bin/env python3
"""The sweep: reconcile non-terminal run records against the
live session list — orphaned sessions, launches whose launcher
died mid-launch (stale "running"), and launches that died
before landing (stale "starting").
"""

import json
from datetime import datetime, timezone

from .foundation import REPO, RUNS, colab, lease, now
from .records import emit_terminal_event, mark_terminal, save_record

# A record still in "running" (the pre-confirmation state) this
# long after its start, with no fresher evidence, had its
# launcher die mid-launch: live launchers pass through the state
# in minutes (launch exec + one confirmation read, both bounded).
STALE_RUNNING_S = 1200
# The stale-"starting" bound for the sweep: "starting" is a
# pre-launch state whose worst legitimate hold is one wedged
# upload call (subprocess bound 3600 s) plus the phases around
# it, so the bound sits at twice that call. Past it, with no
# fresher stamp or save, the launcher died before the launch.
STALE_STARTING_S = 7200


def orphan_sweep() -> tuple[list[dict], bool]:
    """Reconcile non-terminal run records against live sessions.

    Membership is exact-name, through the lease core's live-list
    parser (lease.live_sessions) — never substring on raw stdout:
    a name that textually collides with a live one must not read
    as alive, and a partial list must not read as a census. An
    unreadable list (None) reconciles nothing — the session_state
    rule, that no caller may act on a list it could not read. A
    torn record is skipped, never fatal: one damaged file must
    not block the reconcile of every other run (it blocked every
    runner entry in the 2026-11-08 flap). The sweep runs on the
    reconcile cadences (--poll, --sweep), never on the launch
    path. Returns (findings, live_list_readable)."""
    findings = []
    if not RUNS.exists():
        return findings, True
    live = lease.live_sessions(colab, REPO)
    if live is None:
        return findings, False
    live_names = {entry["session"] for entry in live}
    for f in sorted(RUNS.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("state") in ("running", "polling", "starting") and rec.get("session") and rec.get("session") not in live_names:
            rec["state"] = "orphaned"
            rec["orphan_noted_at"] = now()
            save_record(rec)
            if emit_terminal_event(rec):
                save_record(rec)
            findings.append({"run_id": rec["run_id"], "job": rec["job"], "action": "marked orphaned; relaunch with --resume RUN_ID to continue from its remote run-id"})
        elif rec.get("state") == "running" and rec.get("remote_out") and not rec.get("launch_confirmed_at"):
            # Stale-running: the launcher died between stamping
            # "running" and confirming the launch (a killed
            # launcher — flap, signal — lands here even with the
            # launch path's timeout guards). "running" is a
            # minutes-long state for any live launcher, and an
            # actively reconciled run keeps last_evidence_at
            # fresh, so staleness on both clocks is a dead
            # launcher, never live work. The remote_out gate is
            # the discriminator: a launched record always carries
            # it (stamped in the same save as "running"), while a
            # registration-style record without one is co-tenancy
            # evidence the sweep has never touched.
            try:
                started_age = (datetime.now(timezone.utc) - datetime.fromisoformat(rec["started_at"])).total_seconds()
                evidence_age = (datetime.now(timezone.utc) - datetime.fromisoformat(rec["last_evidence_at"])).total_seconds() if rec.get("last_evidence_at") else None
            except (KeyError, ValueError):
                started_age, evidence_age = 0, None
            if started_age > STALE_RUNNING_S and (evidence_age is None or evidence_age > STALE_RUNNING_S):
                mark_terminal(rec, "failed", launch_lost=True, error=f"record still 'running' {int(started_age // 60)} min after start with no launch confirmation and no fresher evidence: the launcher died mid-launch; relaunch with --resume {rec['run_id']} to continue from its remote run-id")
                findings.append({"run_id": rec["run_id"], "job": rec["job"], "action": "marked failed (launch-lost): stale 'running' record whose launcher died mid-launch; relaunch with --resume RUN_ID"})
        elif rec.get("state") == "starting" and (rec.get("session") in live_names or not rec.get("session")):
            # Stale-"starting": the launcher died before its
            # launch landed (the upload-phase deaths on a wedged
            # session — 2026-11-11: six records sat in "starting"
            # for hours, reading as in-flight work and holding
            # their callers' claim evidence). Freshness is the
            # freshest of the record's stamps and its file mtime
            # (every pre-launch save — bundle seed, usage read —
            # rewrites the file), and the bound clears the worst
            # legitimate pre-launch hold. Co-tenancy is not at
            # risk here: registration-style records are planted
            # in "running", a state this branch never touches.
            stamps = []
            for key in ("last_evidence_at", "launch_at", "resumed_at", "started_at"):
                try:
                    if rec.get(key):
                        stamps.append(datetime.fromisoformat(rec[key]).timestamp())
                except ValueError:
                    pass
            try:
                stamps.append(f.stat().st_mtime)
            except OSError:
                pass
            age = datetime.now(timezone.utc).timestamp() - max(stamps) if stamps else 0
            if age > STALE_STARTING_S:
                mark_terminal(rec, "failed", launch_lost=True, error=f"record still 'starting' {int(age // 60)} min after its freshest stamp: the launcher died before the launch landed; relaunch with --resume {rec['run_id']} to retry")
                findings.append({"run_id": rec["run_id"], "job": rec["job"], "action": "marked failed (launch-lost): stale 'starting' record whose launcher died pre-launch; relaunch with --resume RUN_ID"})
    return findings, True
