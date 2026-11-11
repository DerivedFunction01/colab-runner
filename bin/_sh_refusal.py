#!/usr/bin/env python3
"""colab-sh's refusal text: the claim gate's evidence rendering,
the detached alternative a refusal prints, and the bridge-busy
classifier (E3).

Extracted from bin/colab-sh (consolidation Lane B step 5) with
no behaviour change; bin/colab-sh binds these names from here,
so its module surface is unchanged. Pure functions of their
arguments — no local state, no channel use.
"""

import shlex


def _evidence_text(verdict: dict) -> str:
    parts = []
    for item in verdict.get("evidence") or []:
        kind = item.get("kind")
        if kind == "claim":
            parts.append(f"claim claimed at {item.get('claimed_at')}")
        elif kind == "run":
            parts.append(f"run {item.get('run_id')} ({item.get('state')})")
        elif kind == "job":
            parts.append(f"job {item.get('run_id')} registered on the lease record")
        elif kind == "detached":
            parts.append(f"detached launch {item.get('id')} running")
        elif kind == "long-hold":
            parts.append(f"long hold until {item.get('until')}")
        elif kind == "active-caller":
            parts.append(f"caller {item.get('caller')} marked active in config/callers.json")
        elif kind == "attach":
            parts.append(f"attach via {item.get('seam')} at {item.get('ts')}")
    return "; ".join(parts) or "no detail"


def detached_alternative(args, session: str, caller: str | None, script_args: list) -> str:
    """The --detach form of the caller's own refused command: the
    async route that stays open to every caller."""
    parts = ["colab-sh", "--detach", "-s", session]
    if caller:
        parts += ["--caller", caller]
    if args.dir != "/content":
        parts += ["--dir", args.dir]
    for kv in args.env:
        parts += ["--env", kv]
    if args.file:
        parts += ["-f", args.file]
        if script_args:
            parts += ["--", *script_args]
    elif args.command:
        parts += ["--", *args.command]
    else:
        parts += ["<your-script-on-stdin>"]
    return " ".join(shlex.quote(p) if any(c in p for c in " <>") else p for p in parts)


def refusal_message(session: str, caller: str | None, verdict: dict, alternative: str) -> str:
    who = f"caller {caller!r}" if caller else "an unattributed caller"
    if verdict.get("blocked_by") == "error":
        head = f"the sync-claim check failed ({verdict.get('error')}); refusing — fail closed"
    elif verdict.get("blocked_by") == "active-caller":
        head = "this caller is marked active in the caller registry — active callers launch detached; synchronous ops are not allowed for them"
    elif verdict.get("blocked_by") == "claim":
        head = f"the sync claim is held by {verdict.get('holder')!r} until {verdict.get('expires_at')}"
    else:
        head = f"caller {verdict.get('holder')!r} is active on this session and synchronous use is off while multiple threads are active"
    return (
        f"colab-sh: synchronous attach to session {session!r} refused for {who} — {head} "
        f"(evidence: {_evidence_text(verdict)}).\n"
        f"Detached alternative (open to all callers): {alternative}"
    )


def _is_busy(proc) -> bool:
    """A bridge-busy (429-class) failure: ssh's generic 255 with
    the bridge's busy answer in its output. Anything else is a
    real result and propagates untouched."""
    if proc is None or proc.returncode != 255:
        return False
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return "429" in text or "too many requests" in text.lower()
