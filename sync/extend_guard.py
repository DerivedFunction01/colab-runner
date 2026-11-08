"""Auto-extension guard (owner-ordered 2026-11-06): every live
lease is extended before it lapses, so a venue is standing when
work fires. Extending an existing lease is lifecycle, never a
fresh start — the guard creates nothing.

The policy (`.plans/2026-11-06-extend-guard-policy.md`) lives in
the lease config's `extend_guard` block — tracked example,
untracked live file. Each shape, and optionally each caller,
carries a mode: `active-only` (extend only a lease with work
signals), `active-or-first` (active leases always; idle leases
up to a per-shape cap, warmest first), `always`, or `never`.
Activity is read from the lease record and the run records
only — registered jobs, an in-flight run, or a `last_used_at`
inside the idle window; the keepalive heartbeat is not
activity. An idle lease is extended at most once per idle
episode (an episode is a constant `last_used_at`): extended
while idle and still idle at the same stamp, it is skipped and
lapses at its deadline. Unknown mode values fail closed to
`never` with a finding line.

The scheduler runs `bin/colab-extend-guard` on a cadence. A
clean pass exits 0 (extensions and policy skips print one JSON
line each; skips are records, not speech). A refusal, an error,
or a bad policy value exits 3 with finding lines, refusals
episode-tracked in `var/extend-guard-state.json` so the
escalation speaks once per episode. The state file also carries
the resolved policy and one verdict per live lease for the
core's `state` serving.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

MODES = ("active-only", "active-or-first", "always", "never")
POLICY_DEFAULTS = {
    "enabled": True,
    "threshold_minutes": 120,
    "window_hours": 8,
    "idle_after_minutes": 60,
    "max_extended_per_shape": 1,
    "default_mode": "active-or-first",
    "shapes": {},
    "callers": {},
}


def load_policy(block) -> tuple[dict, list[dict]]:
    """Resolve the guard policy from the lease config's
    `extend_guard` block (or None). Returns (policy, findings):
    an unknown mode anywhere resolves that entry to `never` and
    produces a policy finding line."""
    if not isinstance(block, dict):
        block = {}
    policy = dict(POLICY_DEFAULTS)
    policy["shapes"] = {}
    policy["callers"] = {}
    for key in ("enabled", "threshold_minutes", "window_hours", "idle_after_minutes", "max_extended_per_shape", "default_mode"):
        if key in block:
            policy[key] = block[key]
    for src in ("shapes", "callers"):
        if isinstance(block.get(src), dict):
            policy[src] = dict(block[src])
    findings: list[dict] = []

    def checked(mode, where):
        if mode not in MODES:
            findings.append({"action": "policy-unknown-mode", "finding": True, "where": where, "mode": mode})
            return "never"
        return mode

    policy["default_mode"] = checked(policy["default_mode"], "default_mode")
    policy["shapes"] = {k: checked(v, f"shapes.{k}") for k, v in policy["shapes"].items()}
    policy["callers"] = {k: checked(v, f"callers.{k}") for k, v in policy["callers"].items()}
    return policy, findings


def resolve_mode(policy: dict, shape, caller) -> tuple[str, str]:
    """The mode governing one lease, and its source: the caller
    map first, then the shape map, then the default."""
    if caller and caller in policy["callers"]:
        return policy["callers"][caller], "caller"
    if shape in policy["shapes"]:
        return policy["shapes"][shape], "shape"
    return policy["default_mode"], "default"


def _inflight_sessions(repo: Path) -> set:
    out = set()
    runs = repo / "results" / "runs"
    if runs.is_dir():
        for path in runs.glob("*.json"):
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if rec.get("state") in ("starting", "running", "polling") and rec.get("session"):
                out.add(rec["session"])
    # Detached shell work (E8) is in-flight work beside the runs:
    # a session busy with a detached launch is not idle, whatever
    # its run records say.
    detached = repo / "results" / "detached"
    if detached.is_dir():
        for path in detached.glob("*.json"):
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if rec.get("state") == "running" and rec.get("session"):
                out.add(rec["session"])
    return out


def _is_active(lease_mod, rec: dict, inflight: set, now_dt: datetime, idle_after_s: int) -> bool:
    if rec.get("jobs"):
        return True
    if rec.get("session") in inflight:
        return True
    last_used = rec.get("last_used_at")
    if last_used:
        try:
            return (now_dt - lease_mod.parse_dt(last_used)).total_seconds() <= idle_after_s
        except (TypeError, ValueError):
            return False
    return False


def _state_path(repo: Path) -> Path:
    return repo / "var" / "extend-guard-state.json"


def _load_state(repo: Path) -> dict:
    try:
        state = json.loads(_state_path(repo).read_text())
        if isinstance(state, dict) and isinstance(state.get("episodes"), dict):
            state.setdefault("verdicts", {})
            return state
    except (OSError, ValueError):
        pass
    return {"episodes": {}, "verdicts": {}}


def _save_state(repo: Path, state: dict) -> None:
    path = _state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def guard_state(repo: Path) -> dict:
    """The guard's state file as written by its last pass
    (episodes, verdicts, resolved policy); empty before the
    first pass. Read-only accessor for the core's state serving."""
    return _load_state(repo)


def run_guard(lease_mod, colab, repo: Path, now_dt: datetime | None = None, policy: dict | None = None) -> tuple[int, list[dict]]:
    now_dt = now_dt or datetime.now(timezone.utc)
    findings: list[dict] = []
    if policy is None:
        policy, findings = load_policy(lease_mod.load_config(repo).get("extend_guard"))
    lines: list[dict] = list(findings)
    failed = bool(findings)
    state = _load_state(repo)
    episodes = state["episodes"]
    verdicts: dict = {}
    state["policy"] = {k: policy[k] for k in ("enabled", "threshold_minutes", "window_hours", "idle_after_minutes", "max_extended_per_shape", "default_mode", "shapes", "callers")}
    state["verdicts"] = verdicts
    if not policy.get("enabled", True):
        _save_state(repo, state)
        return (3 if failed else 0), lines

    threshold_s = int(policy["threshold_minutes"]) * 60
    idle_after_s = int(policy["idle_after_minutes"]) * 60
    hours = float(policy["window_hours"])
    cap = int(policy["max_extended_per_shape"])
    inflight = _inflight_sessions(repo)
    prior_verdicts = _load_state(repo).get("verdicts", {})

    due: dict[str, list[dict]] = {}
    for rec in lease_mod.all_records(repo):
        session = rec.get("session")
        if not session or rec.get("standing"):
            continue
        deadline = rec.get("deadline")
        if not deadline:
            continue
        remaining = (lease_mod.parse_dt(deadline) - now_dt).total_seconds()
        if remaining > threshold_s:
            verdicts[session] = {"action": "not-due", "at": now_dt.isoformat()}
            episodes.pop(session, None)
            continue
        due.setdefault(rec.get("shape"), []).append(rec)

    def extend_one(rec) -> None:
        nonlocal failed
        session = rec["session"]
        out = lease_mod.extend(colab, repo, session, hours, now_dt=now_dt, caller="lease")
        action = out.get("action")
        if action == "extended":
            episodes.pop(session, None)
            verdicts[session] = {"action": "extended", "at": now_dt.isoformat(), "idle": not _is_active(lease_mod, rec, inflight, now_dt, idle_after_s), "last_used_at": rec.get("last_used_at")}
            lines.append({"session": session, "action": "extended", "old_deadline": out.get("old_deadline"), "new_deadline": out.get("new_deadline")})
            return
        failed = True
        prior = episodes.get(session)
        first = prior is None or prior.get("action") != action
        episodes[session] = {"action": action, "since": (prior or {}).get("since") or now_dt.isoformat()}
        verdicts[session] = {"action": action, "at": now_dt.isoformat()}
        line = {"session": session, "action": action, "finding": True, "episode_first": first, "caller": rec.get("caller")}
        if out.get("read_error"):
            line["read_error"] = out["read_error"]
        lines.append(line)

    def skip(rec, action: str) -> None:
        verdicts[rec["session"]] = {"action": action, "at": now_dt.isoformat()}
        lines.append({"session": rec["session"], "action": action, "decision": True})

    for shape, recs in due.items():
        idle_pool: list[dict] = []
        for rec in recs:
            mode, source = resolve_mode(policy, shape, rec.get("caller"))
            if rec.get("shortened_at"):
                # Operator-shrunk lease: the guard never
                # re-extends what the operator deliberately
                # pulled in. The stamp is not a cap — an
                # operator extend clears it, and the guard
                # treats the lease normally again.
                skip(rec, "skipped-shortened")
            elif mode == "never":
                skip(rec, "skipped-caller" if source == "caller" else "skipped-shape")
            elif mode == "always":
                extend_one(rec)
            elif mode == "active-only":
                if _is_active(lease_mod, rec, inflight, now_dt, idle_after_s):
                    extend_one(rec)
                else:
                    skip(rec, "skipped-idle")
            else:  # active-or-first
                if _is_active(lease_mod, rec, inflight, now_dt, idle_after_s):
                    extend_one(rec)
                else:
                    idle_pool.append(rec)
        idle_pool.sort(key=lambda r: (r.get("last_used_at") or "", r.get("acquired_at") or ""), reverse=True)
        extended_idle = 0
        for rec in idle_pool:
            prior = prior_verdicts.get(rec["session"]) or {}
            if prior.get("action") == "extended" and prior.get("idle") and prior.get("last_used_at") == rec.get("last_used_at"):
                skip(rec, "skipped-idle")  # already extended once this idle episode
            elif extended_idle < cap:
                extended_idle += 1
                extend_one(rec)
            else:
                skip(rec, "skipped-count")
    state["verdicts"] = verdicts
    _save_state(repo, state)
    return (3 if failed else 0), lines
