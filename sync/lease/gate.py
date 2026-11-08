import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog

def shape_class(shape: dict) -> str:
    """A catalog entry's shape class: "gpu" / "tpu" for accelerator
    shapes (an accelerator is its own class even with high_mem),
    else "high_ram" for a high-memory CPU shape, else "standard"."""
    if shape.get("gpu"):
        return "gpu"
    if shape.get("tpu"):
        return "tpu"
    return "high_ram" if shape.get("high_mem") else "standard"


def session_gate_policy(repo: Path) -> dict:
    """The effective session_gate policy: config/lease.json's block
    over the built-in defaults. The defaults enforce — an absent or
    partial block never opens access."""
    raw = foundation.load_config(repo).get("session_gate")
    if not isinstance(raw, dict):
        raw = {}

    def str_list(key, default):
        value = raw.get(key)
        if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
            return list(value)
        return list(default)

    infra = raw.get("infra_actor")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "agent_classes": str_list("agent_classes", foundation.GATE_DEFAULT_AGENT_CLASSES),
        "bypass_actors": str_list("bypass_actors", foundation.GATE_DEFAULT_BYPASS_ACTORS),
        "infra_actor": infra if isinstance(infra, str) and infra else foundation.GATE_DEFAULT_INFRA_ACTOR,
    }


def gate_journal_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "session-gate-journal.jsonl"


def append_gate_journal(repo: Path, event: dict) -> bool:
    """Append one gate event (a refusal, a bypass or infra
    admission, a shape mismatch) to var/session-gate-journal.jsonl.
    Best-effort like the grant ledger — a failed journal write
    never breaks the admission decision it records."""
    try:
        p = gate_journal_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
        return True
    except OSError:
        return False


def check_session_admission(repo: Path, shape_id: str, via: str | None = None, shape: dict | None = None, caller: str | None = None, standing: bool = False) -> dict:
    """Admission verdict for creating a session of `shape_id`
    (pure: no journaling, no colab calls). The verdict names the
    shape, its class, the actor evidence, and the basis:
    disabled / agent-allowed / bypass / infra / refused. The
    caller rides the verdict as attribution evidence only — it
    never changes the shape decision.

    Bypass: the env claim names a whitelisted bypass actor; the
    panel claim additionally requires the via tag to corroborate
    it (the panel's two halves — see the section note). Infra: the
    standing exemption — the infra actor admitting the standing
    duty's own lease creation (standing=True, a marker only the
    standing duty's acquire passes), nothing else."""
    if shape is None:
        shape = catalog.catalog_shapes(repo).get(shape_id) or {}
    policy = session_gate_policy(repo)
    cls = shape_class(shape)
    env = os.environ.get(foundation.ACTOR_ENV_CLAIM) or None
    verdict = {"allowed": False, "shape": shape_id, "shape_class": cls, "via": via, "env_actor": env, "bypass": False, "caller": caller}
    if not policy["enabled"]:
        return {**verdict, "allowed": True, "basis": "disabled"}
    if cls in policy["agent_classes"]:
        return {**verdict, "allowed": True, "basis": "agent-allowed"}
    if env is not None and env in policy["bypass_actors"] and (env != foundation.PANEL_ACTOR or via == foundation.PANEL_ACTOR):
        return {**verdict, "allowed": True, "basis": "bypass", "bypass": True}
    if via is not None and via == policy["infra_actor"] and standing:
        return {**verdict, "allowed": True, "basis": "infra"}
    return {**verdict, "basis": "refused", "action": "shape-refused", "actor": via or env or "agent", "agent_classes": policy["agent_classes"]}


def enforce_session_admission(repo: Path, shape_id: str, via: str | None = None, shape: dict | None = None, seam: str = "acquire", caller: str | None = None, creation: bool = True, duty: bool = False, standing: bool = False) -> dict | None:
    """check_session_admission plus journaling, for the creation
    seams: a refusal returns the verdict (action shape-refused) for
    the caller to surface; an admission returns None, journaling
    first when it rode a bypass or the infra exemption —
    agent-allowed admissions are the common case and stay
    unjournaled. Journal events carry the resolved caller beside
    the actor evidence (attribution joins, never replaces).

    Creation authority (caller-attribution plan): past the shape
    check, a fresh creation (creation=True) by an agent-class actor
    is refused regardless of shape or claim — creation belongs to
    the panel (bypass basis), the standing exemption (infra
    basis), and the lease duty consuming demands (duty=True, an
    internal marker only auto_acquire passes). The refusal is
    journaled as caller-start-refused naming the caller and seam.
    With the gate disabled the whole admission layer is off, this
    refusal included. Non-creation consults (a demand write) pass
    creation=False and see the shape check only."""
    verdict = check_session_admission(repo, shape_id, via=via, shape=shape, caller=caller, standing=standing)
    if not verdict["allowed"]:
        append_gate_journal(repo, {"kind": "shape-refused", "seam": seam, "shape": shape_id, "shape_class": verdict["shape_class"], "via": via, "env_actor": verdict["env_actor"], "caller": caller, "agent_classes": verdict.get("agent_classes")})
        return verdict
    if verdict["basis"] in ("bypass", "infra"):
        append_gate_journal(repo, {"kind": f"shape-{verdict['basis']}", "seam": seam, "shape": shape_id, "shape_class": verdict["shape_class"], "via": via, "env_actor": verdict["env_actor"], "caller": caller})
        return None
    if creation and not duty and verdict["basis"] == "agent-allowed" and not _actor_privileged(repo, via, verdict, standing=standing):
        append_gate_journal(repo, {"kind": "caller-start-refused", "seam": seam, "shape": shape_id, "shape_class": verdict["shape_class"], "via": via, "env_actor": verdict["env_actor"], "caller": caller})
        return {**verdict, "allowed": False, "basis": "agent-creation-denied", "action": "caller-start-refused", "actor": via or verdict["env_actor"] or "agent"}
    return None


def _actor_privileged(repo: Path, via: str | None, verdict: dict, standing: bool = False) -> bool:
    """Whether the presenting actor carries creation authority
    independent of the shape class. The shape verdict's basis
    cannot answer this: an agent-allowed shape admits on basis
    agent-allowed without ever consulting the bypass list, so a
    bypass actor creating an agent-class shape would read as a
    plain agent. The evidence rules mirror
    check_session_admission's: a whitelisted env claim (the panel
    claim additionally needing its via half), or the infra actor
    admitting the standing duty's own creation."""
    policy = session_gate_policy(repo)
    env = verdict.get("env_actor")
    if env is not None and env in policy.get("bypass_actors", []) and (env != foundation.PANEL_ACTOR or via == foundation.PANEL_ACTOR):
        return True
    return via is not None and via == policy.get("infra_actor") and standing


def renewal_admission(repo: Path, shape_id: str | None, shape: dict) -> dict:
    """Admission for a renewal re-creation: no actor bypass applies
    (a renewal is the system's continuation of an existing window,
    never a panel or probe act) and standing records never reach
    the renewal drivers, so this is the bare class check under the
    policy."""
    policy = session_gate_policy(repo)
    cls = shape_class(shape)
    allowed = not policy["enabled"] or cls in policy["agent_classes"]
    return {"allowed": allowed, "shape": shape_id, "shape_class": cls, "agent_classes": policy["agent_classes"]}
