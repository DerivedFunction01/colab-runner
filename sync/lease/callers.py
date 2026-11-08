import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation

# --- Caller attribution -------------------------------------------------------
# Local why: caller (which thread) is never a substitute for actor
# (which channel acted); machinery callers are constants so a
# missing local registry never breaks the panel or the duties.
# Mechanics: docs/lease-workflow.md.

CALLER_ENV = "COLAB_CALLER"
MACHINERY_CALLERS = (foundation.PANEL_ACTOR, foundation.GATE_DEFAULT_INFRA_ACTOR, "test", "lease")
_CALLER_PART = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def callers_path(repo: Path) -> Path:
    return repo / "config" / "callers.json"


def caller_registry(repo: Path) -> set:
    """The valid caller bases: the machinery constants plus the
    local registry's thread callers. A missing or malformed local
    file yields the machinery set alone — fail-closed for thread
    claims, never a crash."""
    names = set(MACHINERY_CALLERS)
    raw = foundation.load_json(callers_path(repo), {})
    listed = raw.get("callers") if isinstance(raw, dict) else None
    if isinstance(listed, list):
        for name in listed:
            if isinstance(name, str) and _CALLER_PART.fullmatch(name):
                names.add(name)
    return names


def validate_caller(repo: Path, raw) -> str | None:
    """Validate one raw claim: the normalized caller (base, with
    its lane suffix when carried) or None — for an absent claim,
    an unregistered base, or a malformed shape alike. Callers that
    must distinguish "no claim" from "bad claim" use
    caller_claim_error."""
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        return None
    base, sep, suffix = raw.partition(":")
    if not _CALLER_PART.fullmatch(base):
        return None
    if sep and not _CALLER_PART.fullmatch(suffix):
        return None
    if base not in caller_registry(repo):
        return None
    return f"{base}:{suffix}" if sep else base


def resolve_caller(repo: Path, flag: str | None = None, env: str | None = None) -> str | None:
    """The seam's caller claim, resolved: the flag when one was
    presented, else the env value, validated. None when no claim
    was presented or the presented claim is invalid."""
    raw = flag if flag is not None else env
    return validate_caller(repo, raw)


def caller_claim_error(repo: Path, flag: str | None = None, env: str | None = None) -> str | None:
    """Why the presented claim failed, or None: no claim presented,
    or the claim validated. The wrapper seams refuse on a presented
    but invalid claim (an unregistered caller is refused at claim
    time) while an absent claim simply leaves the call
    unattributed."""
    raw = flag if flag is not None else env
    if raw is None:
        return None
    if validate_caller(repo, raw) is not None:
        return None
    return f"unregistered caller {raw!r} (registry: config/callers.json; machinery callers always valid)"


def caller_claims_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "caller-claims.jsonl"


def append_caller_claim(repo: Path, session: str, caller: str | None, seam: str) -> bool:
    """Journal one attach claim (the colab-sh seam writes these):
    who attached to which session, when, through which wrapper.
    The claims are the attribution evidence for sessions that live
    outside lease records. Best-effort like every journal here."""
    try:
        p = caller_claims_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "session": session, "caller": caller, "seam": seam}) + "\n")
        return True
    except OSError:
        return False


def load_caller_claims(repo: Path) -> dict:
    """The latest attributed claim per session: {session: {"caller",
    "ts"}}. Claims with no caller are attach evidence only and do
    not attribute."""
    out: dict = {}
    try:
        lines = caller_claims_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            claim = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(claim, dict) and claim.get("session") and claim.get("caller"):
            out[claim["session"]] = {"caller": claim["caller"], "ts": claim.get("ts")}
    return out
