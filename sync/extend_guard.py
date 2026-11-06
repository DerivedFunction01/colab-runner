"""Auto-extension guard (owner-ordered 2026-11-06): every live
lease is extended before it lapses, so a venue is standing when
work fires. Extending an existing lease is lifecycle, never a
fresh start — the guard creates nothing.

The scheduler runs `bin/colab-extend-guard` on a cadence; each
pass reads every lease record, and any non-standing lease whose
deadline is inside the threshold is extended by a full window
through the core's `extend()` (which prices the delta against
the budget, flags the record renew, and installs the renewal
kit on the live session). Standing leases are skipped: their
lifetime belongs to the grant.

Outcomes print as one JSON line per action or finding. A clean
pass exits 0 (silence); a refusal or error exits 3 with the
finding lines, episode-tracked in `var/extend-guard-state.json`
so the escalation speaks once per episode rather than every
pass.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

EXTEND_HOURS = 8.0
DEFAULT_THRESHOLD_S = 7200


def _state_path(repo: Path) -> Path:
    return repo / "var" / "extend-guard-state.json"


def _load_state(repo: Path) -> dict:
    try:
        state = json.loads(_state_path(repo).read_text())
        if isinstance(state, dict) and isinstance(state.get("episodes"), dict):
            return state
    except (OSError, ValueError):
        pass
    return {"episodes": {}}


def _save_state(repo: Path, state: dict) -> None:
    path = _state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def run_guard(lease_mod, colab, repo: Path, now_dt: datetime | None = None, threshold_s: int | None = None, hours: float = EXTEND_HOURS) -> tuple[int, list[dict]]:
    now_dt = now_dt or datetime.now(timezone.utc)
    if threshold_s is None:
        threshold_s = int(os.environ.get("COLAB_EXTEND_GUARD_THRESHOLD_S", str(DEFAULT_THRESHOLD_S)))
    state = _load_state(repo)
    episodes = state["episodes"]
    lines: list[dict] = []
    failed = False
    for rec in lease_mod.all_records(repo):
        session = rec.get("session")
        if not session or rec.get("standing"):
            continue
        deadline = rec.get("deadline")
        if not deadline:
            continue
        remaining = (lease_mod.parse_dt(deadline) - now_dt).total_seconds()
        if remaining > threshold_s:
            episodes.pop(session, None)
            continue
        out = lease_mod.extend(colab, repo, session, hours, now_dt=now_dt, caller="lease")
        action = out.get("action")
        if action == "extended":
            episodes.pop(session, None)
            lines.append({"session": session, "action": "extended", "old_deadline": out.get("old_deadline"), "new_deadline": out.get("new_deadline")})
            continue
        failed = True
        prior = episodes.get(session)
        first = prior is None or prior.get("action") != action
        episodes[session] = {"action": action, "since": (prior or {}).get("since") or now_dt.isoformat()}
        lines.append({"session": session, "action": action, "finding": True, "episode_first": first, "caller": rec.get("caller")})
    _save_state(repo, state)
    return (3 if failed else 0), lines
