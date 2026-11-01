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

The `state` shape mirrors sync/lease.py's state() exactly: records
are status entries wrapping the lease record, the budget carries its
caps, and every spec carries its catalog label.
"""

import json
import os
import sys

DEADLINE = "2099-01-01T00:00:00+00:00"


def record(profile):
    return {
        "profile": profile,
        "session": profile,
        "acquired_at": "2026-11-01T00:00:00+00:00",
        "deadline": DEADLINE,
        "last_used_at": "2026-11-01T00:00:00+00:00",
        "last_pulse_at": "2026-11-01T00:00:00+00:00",
        "last_pulse_result": "ok",
        "jobs": [],
    }


def state_entry(profile):
    return {
        "action": "active",
        "record": record(profile),
        "keepalive_heartbeat_age_s": 12,
        "profile": profile,
        "warm": None,
    }


def main(argv):
    log = os.environ.get("STUB_LEASE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(" ".join(argv) + "\n")
    if not argv:
        return 2
    cmd = argv[0]
    if os.environ.get("STUB_LEASE_FAIL") == cmd:
        print("stub failure", file=sys.stderr)
        return 3
    if cmd == "state":
        granted = os.environ.get("STUB_GRANT") == "1"
        grant = None
        envelope = None
        entry = state_entry("workload")
        if granted:
            caps = {"daily": 10, "monthly": 100, "standing_daily": 2.0}
            scope = {"caps": caps, "lobby_profile": "lobby", "burst_profile": "workload", "profiles": ["lobby", "workload"]}
            grant = {
                "kind": "two-tier",
                "granted_at": "2026-11-01T00:00:00+00:00",
                "until": DEADLINE,
                "lobby_profile": "lobby",
                "burst_profile": "workload",
                "scope": scope,
            }
            entry["record"] = {**entry["record"], "envelope_id": grant["granted_at"], "via": "xbrl-thread"}
            envelope = {
                "id": grant["granted_at"],
                "grant": grant,
                "scope": scope,
                "caps": caps,
                "records": [entry["record"]],
                "spend": {"balance": 571.7, "day_spent": 4.04, "month_spent": 9.97, "standing_day_spent": 1.5},
                "profile_spend": [{"profile": "workload", "day_spent": 2.08, "month_spent": 2.08}],
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
                {"id": "workload", "label": "High-RAM", "session": "workload", "gpu": None, "high_mem": True, "rate_cu_per_hour": 0.26, "timeout": 18000},
                {"id": "lobby", "label": "Lobby", "session": "lobby", "gpu": None, "high_mem": False, "rate_cu_per_hour": 0.08, "timeout": 3600},
                {"id": "standard", "label": "Standard", "session": "standard", "gpu": None, "high_mem": False, "rate_cu_per_hour": 0.08, "timeout": 3600},
                {"id": "t4", "label": "T4", "session": "t4", "gpu": "T4", "high_mem": False, "rate_cu_per_hour": None, "timeout": 18000},
            ],
        }
    elif cmd == "acquire":
        out = {"action": "acquired", "record": record(argv[1] if len(argv) > 1 else "workload")}
    elif cmd == "extend":
        hours = float(argv[argv.index("--hours") + 1]) if "--hours" in argv else None
        if os.environ.get("STUB_EXTEND_REFUSE") == "1":
            print(json.dumps({"action": "refused-budget", "budget": {"ok": False, "reason": "daily-budget", "projected": 16.0, "cap": 10}}))
            return 1
        out = {"action": "extended", "profile": argv[1], "hours": hours, "old_deadline": "2026-11-02T00:00:00+00:00", "new_deadline": "2026-11-02T02:00:00+00:00"}
    elif cmd == "release":
        out = {"action": "released", "profile": argv[1] if len(argv) > 1 else None}
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
