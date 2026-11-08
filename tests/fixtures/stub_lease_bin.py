#!/usr/bin/env python3
"""Stub bin/colab-lease for the lease UI tests.

Emits the fixed core contract JSON per subcommand and records every
invocation (one line, argv space-joined) to $STUB_LEASE_LOG. If
$STUB_LEASE_FAIL names the subcommand, exits 3 with no JSON (a core
transport failure). $STUB_GRANT=1 makes `state` report an active grant.
$STUB_EXTEND_REFUSE=1 makes `extend` answer refused-budget and
`extend-grant` answer grant-inactive, both with exit 1 (a core refusal).
$STUB_CAPS_INVALID=1 makes `budget-caps` answer caps-invalid with
exit 1 (the core rejecting a cap update).
$STUB_ADOPT_REFUSE=1 makes `adopt` answer adopt-deadline-past
with exit 1 (a core refusal).
$STUB_SHORTEN_REFUSE=1 makes `shorten` answer shorten-not-earlier
with exit 1 (a core refusal).

The `state` shape mirrors sync/lease/state.py's state() exactly: records
are status entries wrapping the lease record (keyed by session),
the budget carries its caps, and every spec carries its catalog
label.
"""

import json
import os
import sys

DEADLINE = "2099-01-01T00:00:00+00:00"


SESSION = "amber-otter-42"
SHAPE_SPEC = {"gpu": None, "tpu": None, "high_mem": True}

ORPHAN = {
    "session": "stray-fox-7",
    "endpoint": "https://m-hm-fake.prod.r.colab.dev",
    "shape_class": "high_ram",
    "shape": "cpu-highram",
    "shape_spec": dict(SHAPE_SPEC),
    "shape_evidenced": True,
    "shape_candidates": [],
    "creator_caller": "alpha",
    "first_claim": "alpha",
    "derived_deadline": "2026-11-09T00:00:00+00:00",
    "deadline_source": "renewal-config",
    "deadline_stale_possible": False,
    "conflicts": [],
    "acquired_at": "2026-11-01T00:00:00+00:00",
    "jobs_in_flight": [],
    "first_seen": "2026-11-01T01:00:00+00:00",
    "kit_installed": True,
    "handover_pending": False,
}


def record(session, shape="cpu-highram"):
    return {
        "session": session,
        "shape": shape,
        "shape_spec": dict(SHAPE_SPEC),
        "acquired_at": "2026-11-01T00:00:00+00:00",
        "deadline": DEADLINE,
        "last_used_at": "2026-11-01T00:00:00+00:00",
        "last_pulse_at": "2026-11-01T00:00:00+00:00",
        "last_pulse_result": "ok",
        "jobs": [],
    }


def state_entry(session):
    claim = None
    if os.environ.get("STUB_SYNC_CLAIM") == "1":
        claim = {
            "caller": "alpha",
            "claimed_at": "2026-11-01T00:00:00+00:00",
            "expires_at": "2099-01-01T01:00:00+00:00",
            "last_attach_at": "2026-11-01T00:00:00+00:00",
        }
    return {
        "action": "active",
        "record": record(session),
        "keepalive_heartbeat_age_s": 12,
        "session": session,
        "warm": None,
        "sync_claim": claim,
    }


def main(argv):
    log = os.environ.get("STUB_LEASE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(" ".join(argv) + "\n")
            if argv and argv[0] == "acquire":
                # The actor claim the server passed in the child env
                # (session shape gate: the panel claims "panel").
                fh.write(f"actor:{os.environ.get('COLAB_LEASE_ACTOR') or 'absent'}\n")
    if not argv:
        return 2
    cmd = argv[0]
    fail = os.environ.get("STUB_LEASE_FAIL")
    part = argv[argv.index("--part") + 1] if "--part" in argv else None
    if fail == cmd or (part is not None and fail == f"{cmd}:{part}"):
        print("stub failure", file=sys.stderr)
        return 3
    if cmd == "state":
        granted = os.environ.get("STUB_GRANT") == "1"
        grant = None
        envelope = None
        entry = state_entry(SESSION)
        if granted:
            caps = {"daily": 10, "monthly": 100, "standing_daily": 2.0}
            scope = {"caps": caps, "standing_shape": "cpu", "burst_shape": "cpu-highram", "shapes": ["cpu", "cpu-highram"]}
            grant = {
                "kind": "two-tier",
                "granted_at": "2026-11-01T00:00:00+00:00",
                "until": DEADLINE,
                "scope": scope,
            }
            entry["record"] = {**entry["record"], "envelope_id": grant["granted_at"], "caller": "xbrl-thread"}
            envelope = {
                "id": grant["granted_at"],
                "grant": grant,
                "scope": scope,
                "caps": caps,
                "records": [entry["record"]],
                "spend": {"balance": 571.7, "day_spent": 4.04, "month_spent": 9.97, "standing_day_spent": 1.5},
                "session_spend": [{"session": SESSION, "shape": "cpu-highram", "day_spent": 2.08, "month_spent": 2.08}],
            }
        out = {
            "records": [entry],
            "grant": grant,
            "envelope": envelope,
            "budget": {
                "balance": 571.7,
                "day_spent": 4.04,
                "month_spent": 9.97,
                "caps": {"daily": 10, "monthly": 100, "standing_daily": 2.0},
            },
            # t4's rate is null on purpose: the core's rate table prices
            # only the CPU tiers, so the UI must render "Cost unconfirmed".
            "specs": [
                {"id": "cpu-highram", "label": "High-RAM CPU", "gpu": None, "tpu": None, "high_mem": True, "rate_cu_per_hour": 0.26, "agent_startable": True},
                {"id": "cpu", "label": "Standard CPU", "gpu": None, "tpu": None, "high_mem": False, "rate_cu_per_hour": 0.08, "agent_startable": False},
                {"id": "t4", "label": "T4 GPU", "gpu": "T4", "tpu": None, "high_mem": False, "rate_cu_per_hour": None, "agent_startable": False},
            ],
            "session_gate": {"enabled": True, "agent_classes": ["high_ram"], "bypass_actors": ["panel"], "infra_actor": "standing"},
            "bridge": None,
            "orphans": [dict(ORPHAN)],
        }
        if part is not None:
            slices = {
                "records": {"records": out["records"]},
                "specs": {"specs": out["specs"], "session_gate": out["session_gate"]},
                "budget": {"budget": out["budget"]},
                "grant": {"grant": out["grant"], "envelope": out["envelope"]},
                "meta": {"bridge": out["bridge"]},
                "orphans": {"orphans": out["orphans"]},
            }
            out = slices[part]
    elif cmd == "acquire":
        shape = argv[argv.index("--shape") + 1] if "--shape" in argv else "cpu-highram"
        out = {"action": "acquired", "record": record(SESSION, shape)}
    elif cmd == "extend":
        hours = float(argv[argv.index("--hours") + 1]) if "--hours" in argv else None
        if os.environ.get("STUB_EXTEND_REFUSE") == "1":
            print(json.dumps({"action": "refused-budget", "budget": {"ok": False, "reason": "daily-budget", "projected": 16.0, "cap": 10}}))
            return 1
        out = {"action": "extended", "session": argv[1], "hours": hours, "old_deadline": "2026-11-02T00:00:00+00:00", "new_deadline": "2026-11-02T02:00:00+00:00"}
    elif cmd == "adopt":
        if os.environ.get("STUB_ADOPT_REFUSE") == "1":
            print(json.dumps({"action": "adopt-deadline-past", "session": argv[1]}))
            return 1
        shape = argv[argv.index("--shape") + 1] if "--shape" in argv else "cpu-highram"
        out = {"action": "adopted", "record": record(argv[1], shape), "deadline_source": "manual"}
    elif cmd == "shorten":
        if os.environ.get("STUB_SHORTEN_REFUSE") == "1":
            print(json.dumps({"action": "shorten-not-earlier", "session": argv[1]}))
            return 1
        out = {"action": "shortened", "session": argv[1], "old_deadline": "2026-11-02T02:00:00+00:00", "new_deadline": "2026-11-02T00:00:00+00:00"}
    elif cmd == "release":
        out = {"action": "released", "session": argv[1] if len(argv) > 1 else None}
    elif cmd == "release-session":
        if len(argv) > 1 and argv[1] == "recorded-session":
            print(json.dumps({"action": "session-has-record", "session": argv[1]}))
            return 1
        out = {"action": "session-released", "session": argv[1] if len(argv) > 1 else None, "stop": "stopped"}
    elif cmd == "grant":
        out = {"action": "granted", "until": DEADLINE}
    elif cmd == "extend-grant":
        hours = float(argv[argv.index("--hours") + 1]) if "--hours" in argv else None
        if os.environ.get("STUB_EXTEND_REFUSE") == "1":
            print(json.dumps({"action": "grant-inactive"}))
            return 1
        out = {"action": "grant-extended", "hours": hours, "old_until": "2026-11-02T00:00:00+00:00", "new_until": "2026-11-02T04:00:00+00:00"}
    elif cmd == "budget-caps":
        if os.environ.get("STUB_CAPS_INVALID") == "1":
            print(json.dumps({"action": "caps-invalid", "error": "daily cap must be a non-negative number or null"}))
            return 1
        caps = {"daily": 10, "monthly": 100, "standing_daily": 2.0}
        previous = dict(caps)
        cap_flags = {"--daily": "daily", "--monthly": "monthly", "--standing-daily": "standing_daily"}
        for flag, key in cap_flags.items():
            if flag in argv:
                raw = argv[argv.index(flag) + 1]
                if raw == "none":
                    caps[key] = None
                else:
                    value = float(raw)
                    caps[key] = int(value) if value.is_integer() else value
        out = {"action": "caps-updated", "caps": caps, "previous": previous}
    elif cmd == "ungrant":
        out = {"action": "grant-ended"}
    else:
        out = {"action": "unknown-command"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
