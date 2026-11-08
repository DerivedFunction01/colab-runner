#!/usr/bin/env python3
"""The Colab lease: a session held by a caller at a shape, treated as a VM.

All session-lifecycle logic for the lease model lives here (plans:
.plans/2026-10-29-colab-lease-model.md for the model,
.plans/2026-11-05-profile-retirement.md for the current shape of it).
The module is written against an injected `colab` callable with
bin/colab-run's signature — `colab(*args, timeout=...) ->
CompletedProcess` — so the fake CLI tests it with no network.
bin/colab-lease is the thin CLI; bin/colab-run and bin/colab-check
call acquire/finish_job; the reaper hook calls reap and, when
enabled in config, auto_acquire.

A lease is {session, caller, shape, window}: the session's name is
generated at creation (sync/naming.py) and never reused, the caller
is the governed attribution claim, and the shape is a catalog shape
descriptor from the CLI-parsed catalog. State: one record per
lease, var/lease-<session>.json (override the var dir with
$COLAB_RUNNER_VAR, used by tests). There is no legacy model: a
record, grant, or journal segment is read exactly as written, in
the current shape/caller vocabulary, and anything written in the
retired profile vocabulary is simply not interpreted. A two-tier
grant lives in var/grant.json. The budget
guard's spend is attributed: completed session segments are
journaled to var/lease-spend-journal.jsonl and live records
contribute their running segment, so spend counts only this repo's
own leases. Config: config/lease.json. Nothing here prints file
contents from the transient registry; the sessions binding is read
for its endpoint on the release fallback path, and for its tunnel
host when a session is born (acquire / renewal swap / adoption).
"""

import contextlib
import fcntl
import importlib.util as _ilu
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

_naming_spec = _ilu.spec_from_file_location("colab_naming", Path(__file__).resolve().parent / "naming.py")
naming = _ilu.module_from_spec(_naming_spec)
_naming_spec.loader.exec_module(naming)

_liveness_spec = _ilu.spec_from_file_location("colab_liveness_entry", Path(__file__).resolve().parent / "liveness_entry.py")
liveness_entry = _ilu.module_from_spec(_liveness_spec)
_liveness_spec.loader.exec_module(liveness_entry)

_extend_guard_spec = _ilu.spec_from_file_location("colab_extend_guard", Path(__file__).resolve().parent / "extend_guard.py")
extend_guard = _ilu.module_from_spec(_extend_guard_spec)
_extend_guard_spec.loader.exec_module(extend_guard)


def var_dir(repo: Path) -> Path:
    return Path(os.environ.get("COLAB_RUNNER_VAR", repo / "var"))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def load_config(repo: Path) -> dict:
    return load_json(repo / "config" / "lease.json", {"enabled": False})


def shape_id_for(gpu, tpu, high_mem) -> str:
    """The mechanical catalog id for a shape's fields — the same
    derivation the dynamic catalog always used (a GPU variant's
    lowercased name, a TPU's prefixed name, "cpu" for plain CPU;
    "-highram" appended for a high-memory variant)."""
    if gpu:
        return str(gpu).lower() + ("-highram" if high_mem else "")
    if tpu:
        return "tpu-" + str(tpu).lower() + ("-highram" if high_mem else "")
    return "cpu" + ("-highram" if high_mem else "")


# --- The dynamic shape catalog ------------------------------------------------
# Local why: the option set is parsed, never hand-listed, so a CLI
# format change surfaces as `catalog_error` instead of an invented
# or missing catalog; CPU shapes serve even when parsing fails.
# Mechanics: docs/lease-workflow.md.

CLI_CATALOG_TTL_S = 24 * 3600
SHAPE_TIMEOUT_S = 18000  # the configured accelerator shapes' timeout


def parse_cli_shapes(help_text: str) -> dict | None:
    """The accelerator space from `colab new --help`: {"gpu": [...],
    "tpu": [...], "no_high_mem": [...]}. The help is a wrapped,
    box-drawn table, so the text is flattened before matching. None
    when either variant list is absent — a format change must shrink
    the catalog loudly (catalog_error), never silently invent shapes."""
    flat = re.sub(r"\s+", " ", help_text.replace("│", " "))

    def variants(kind: str) -> list[str] | None:
        m = re.search(kind + r" accelerator variant\. Supported: ([^.]+)\.", flat)
        if not m:
            return None
        out = [v for v in re.split(r"[,\s]+", m.group(1)) if v]
        return out or None

    gpu, tpu = variants("GPU"), variants("TPU")
    if gpu is None or tpu is None:
        return None
    m = re.search(r"single shape \(([^)]+)\)", flat)
    no_high_mem = [v for v in re.split(r"[,\s]+", m.group(1)) if v] if m else []
    return {"gpu": gpu, "tpu": tpu, "no_high_mem": no_high_mem}


def _catalog_cache_path(repo: Path) -> Path:
    return var_dir(repo) / "cli-catalog.json"


def _cli_shapes(repo: Path, colab=None):
    """(shapes, error): the parsed CLI space, from the day-cache when
    fresh, else by running `colab new --help` through the injected
    callable. Without a callable only the cache can serve. A failed
    refresh falls back to any cache and reports the failure; with no
    cache at all the dynamic catalog is empty and the error stands."""
    cache = load_json(_catalog_cache_path(repo), None)
    cached = None
    if isinstance(cache, dict) and cache.get("gpu") and cache.get("tpu"):
        cached = {"gpu": cache["gpu"], "tpu": cache["tpu"], "no_high_mem": cache.get("no_high_mem", [])}
        try:
            age = (datetime.now(timezone.utc) - parse_dt(cache["fetched_at"])).total_seconds()
        except Exception:  # noqa: BLE001
            age = CLI_CATALOG_TTL_S + 1
        if age <= CLI_CATALOG_TTL_S:
            return cached, None
    if colab is None:
        return cached, None
    try:
        r = colab("new", "--help", timeout=60)
        shapes = parse_cli_shapes(r.stdout or "") if r.returncode == 0 else None
        if shapes is None:
            raise ValueError("unparseable `colab new --help` output")
    except Exception as exc:  # noqa: BLE001
        if cached is not None:
            return cached, f"cli catalog refresh failed ({exc}); serving the cached shape list"
        return None, f"cli catalog unavailable ({exc}); serving the CPU shapes only"
    try:
        p = _catalog_cache_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), **shapes}, indent=2))
    except OSError:
        pass
    return shapes, None


def _shape_entries(shapes: dict | None) -> dict:
    """Catalog entries for every CLI shape: {id: {id, label, gpu,
    tpu, high_mem, timeout}} — identity, label, and shape fields
    only; the rate joins in catalog_shapes (it has another home).
    Ids and labels are mechanical. With no parsed CLI space, the
    two CPU shapes still serve — plain CPU and High-RAM CPU exist
    in every Colab shape space the CLI has ever printed."""
    out: dict = {}

    def add(label, gpu, tpu, high_mem):
        sid = shape_id_for(gpu, tpu, high_mem)
        if sid not in out:
            out[sid] = {"id": sid, "label": label, "gpu": gpu, "tpu": tpu, "high_mem": high_mem, "timeout": SHAPE_TIMEOUT_S}

    if shapes:
        for gpu in shapes["gpu"]:
            for hm in (False, True):
                if hm and gpu in shapes["no_high_mem"]:
                    continue
                add(gpu + (" High-RAM" if hm else ""), gpu, None, hm)
        for tpu in shapes["tpu"]:
            for hm in (False, True):
                if hm and tpu in shapes["no_high_mem"]:
                    continue
                add("TPU " + tpu + (" High-RAM" if hm else ""), None, tpu, hm)
    for hm in (False, True):
        add("CPU" + (" High-RAM" if hm else ""), None, None, hm)
    return out


# --- Per-shape measured rates ---------------------------------------------------
# Local why: an absent rate stays null (unconfirmed); no cost
# projection is fabricated for it.


def shape_rates_path(repo: Path) -> Path:
    return var_dir(repo) / "shape-rates.json"


def load_shape_rates(repo: Path) -> dict:
    raw = load_json(shape_rates_path(repo), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def shape_rate(repo: Path, shape_id: str):
    """A shape's measured CU/hour rate, or None (unconfirmed):
    the measured-rate table first, then the config tier map for
    the CPU shapes."""
    table = load_shape_rates(repo)
    if shape_id in table:
        return table[shape_id]
    if shape_id in ("cpu", "cpu-highram"):
        tier = load_config(repo).get("rate_cu_per_hour")
        if isinstance(tier, dict):
            return tier.get("high_mem" if shape_id == "cpu-highram" else "standard")
    return None


def catalog_shapes(repo: Path, colab=None) -> dict:
    """The full shape catalog: every CLI shape with its measured
    rate joined (null = unconfirmed). Callers without a colab
    callable get whatever the day-cache holds, plus the CPU
    shapes, which always serve."""
    shapes, _err = _cli_shapes(repo, colab)
    entries = _shape_entries(shapes)
    for sid, entry in entries.items():
        entry["rate_cu_per_hour"] = shape_rate(repo, sid)
    return entries


def default_shape(repo: Path) -> str | None:
    """The config's default shape id (config/lease.json's
    `default_shape`) — the fallback a manifest or wrapper resolves
    when it names no shape. None when the key is absent: callers
    fail loudly rather than substituting a shape."""
    name = load_config(repo).get("default_shape")
    return name if isinstance(name, str) and name else None


def standing_caller(repo: Path) -> str:
    """The standing duty's caller: config/lease.json's
    standing.caller, defaulting to the machinery infra actor —
    the standing lease is identified by its caller (and its
    record's standing flag), never by a profile name."""
    raw = load_config(repo).get("standing", {}).get("caller")
    return raw if isinstance(raw, str) and raw else GATE_DEFAULT_INFRA_ACTOR


def standing_shape(repo: Path) -> str:
    """The standing lease's shape: config/lease.json's
    standing.shape when set, else the plain CPU shape."""
    raw = load_config(repo).get("standing", {}).get("shape")
    if isinstance(raw, str) and raw:
        return raw
    return "cpu"


# --- Session shape gate -------------------------------------------------------
# Local why: a bare panel tag is not a bypass — the bypass needs the
# server-carried env claim too — and the gate is admission only,
# never eviction. It is a journaled guardrail at the same trust
# level, not a security boundary. Mechanics:
# docs/lease-workflow.md.

ACTOR_ENV_CLAIM = "COLAB_LEASE_ACTOR"
PANEL_ACTOR = "panel"
GATE_DEFAULT_AGENT_CLASSES = ("high_ram",)
GATE_DEFAULT_BYPASS_ACTORS = (PANEL_ACTOR,)
GATE_DEFAULT_INFRA_ACTOR = "standing"


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
    raw = load_config(repo).get("session_gate")
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
        "agent_classes": str_list("agent_classes", GATE_DEFAULT_AGENT_CLASSES),
        "bypass_actors": str_list("bypass_actors", GATE_DEFAULT_BYPASS_ACTORS),
        "infra_actor": infra if isinstance(infra, str) and infra else GATE_DEFAULT_INFRA_ACTOR,
    }


def gate_journal_path(repo: Path) -> Path:
    return var_dir(repo) / "session-gate-journal.jsonl"


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
        shape = catalog_shapes(repo).get(shape_id) or {}
    policy = session_gate_policy(repo)
    cls = shape_class(shape)
    env = os.environ.get(ACTOR_ENV_CLAIM) or None
    verdict = {"allowed": False, "shape": shape_id, "shape_class": cls, "via": via, "env_actor": env, "bypass": False, "caller": caller}
    if not policy["enabled"]:
        return {**verdict, "allowed": True, "basis": "disabled"}
    if cls in policy["agent_classes"]:
        return {**verdict, "allowed": True, "basis": "agent-allowed"}
    if env is not None and env in policy["bypass_actors"] and (env != PANEL_ACTOR or via == PANEL_ACTOR):
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
    if env is not None and env in policy.get("bypass_actors", []) and (env != PANEL_ACTOR or via == PANEL_ACTOR):
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


# --- Caller attribution -------------------------------------------------------
# Local why: caller (which thread) is never a substitute for actor
# (which channel acted); machinery callers are constants so a
# missing local registry never breaks the panel or the duties.
# Mechanics: docs/lease-workflow.md.

CALLER_ENV = "COLAB_CALLER"
MACHINERY_CALLERS = (PANEL_ACTOR, GATE_DEFAULT_INFRA_ACTOR, "test", "lease")
_CALLER_PART = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def callers_path(repo: Path) -> Path:
    return repo / "config" / "callers.json"


def caller_registry(repo: Path) -> set:
    """The valid caller bases: the machinery constants plus the
    local registry's thread callers. A missing or malformed local
    file yields the machinery set alone — fail-closed for thread
    claims, never a crash."""
    names = set(MACHINERY_CALLERS)
    raw = load_json(callers_path(repo), {})
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
    return var_dir(repo) / "caller-claims.jsonl"


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


def session_record_path(repo: Path, session: str) -> Path:
    return var_dir(repo) / f"lease-{session}.json"


def _valid_record(data) -> dict | None:
    return data if isinstance(data, dict) and data.get("session") else None


def _record_files(repo: Path) -> list[Path]:
    """Every file that may hold a lease record: the per-session
    files under var/ (the name a file carries says nothing; its
    record's session is the key)."""
    var = var_dir(repo)
    files = []
    if var.exists():
        for path in sorted(var.glob("lease-*.json")):
            if path.name in ("lease-spend.json", "lease-request.json"):
                continue
            files.append(path)
    return files


def all_records(repo: Path) -> list[dict]:
    """Every lease record on disk, exactly as written. One record
    per session: when two files hold the same session, the
    session-keyed file wins. Order: acquisition order."""
    by_session: dict[str, dict] = {}
    canonical: set[str] = set()
    for path in _record_files(repo):
        rec = _valid_record(load_json(path, None))
        if rec is None:
            continue
        session = rec["session"]
        if path.name == f"lease-{session}.json":
            by_session[session] = rec
            canonical.add(session)
        elif session not in canonical:
            by_session.setdefault(session, rec)
    recs = list(by_session.values())
    recs.sort(key=lambda r: (r.get("acquired_at") or "", r.get("session") or ""))
    return recs


def load_record(repo: Path, session: str | None = None) -> dict | None:
    """With a session: that session's record. Without: the most
    recently used record (the lease a job call most plausibly
    belongs to), or None when no lease is on disk."""
    recs = all_records(repo)
    if session is not None:
        return next((r for r in recs if r.get("session") == session), None)
    if not recs:
        return None
    return max(recs, key=lambda r: (r.get("last_used_at") or "", r.get("acquired_at") or ""))


def save_record(repo: Path, rec: dict) -> None:
    """Write one lease's record at its session path. Any other
    file holding a record for the same session is removed, so a
    stale copy can never resurrect the lease's old shape."""
    session = rec["session"]
    p = session_record_path(repo, session)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Staged sibling + rename, never an in-place write: a writer
    # whose write is denied mid-save (the sandbox write-denial
    # flap truncates in-place targets, then refuses) must leave
    # the standing record intact — a torn sibling is discarded
    # and the failure surfaces to the caller instead.
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(rec, indent=2))
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for other in _record_files(repo):
        if other == p:
            continue
        held = _valid_record(load_json(other, None))
        if held is not None and held.get("session") == session:
            other.unlink(missing_ok=True)
    # Session liveness: a saved live record carries its derived
    # scheduler entry (sync/liveness_entry.py). The sync never
    # fails the save — drift is repaired by the reaper janitor.
    try:
        liveness_entry.sync_for_record(repo, rec)
    except Exception:
        pass


def delete_record(repo: Path, session: str) -> None:
    """Remove a lease's record, wherever it lives — for the same
    reason save cleans up: no stale copy may resurrect a released
    lease."""
    for path in _record_files(repo):
        held = _valid_record(load_json(path, None))
        if held is not None and held.get("session") == session:
            path.unlink(missing_ok=True)
    # Session liveness: the derived scheduler entry dies with
    # the record, in the same call.
    try:
        liveness_entry.remove_for_session(repo, session)
    except Exception:
        pass


# --- Generated session names ----------------------------------------------------
# Local why: names are never reused, so a claims-journal attribution
# can never silently come to name a different lease.


def _taken_session_names(repo: Path, colab=None) -> set:
    taken: set = set()
    for rec in all_records(repo):
        taken.add(rec["session"])
    taken |= set(load_caller_claims(repo).keys())
    try:
        lines = spend_journal_path(repo).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            seg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(seg, dict) and seg.get("session"):
            taken.add(seg["session"])
    if colab is not None:
        live = live_sessions(colab, repo)
        if live:
            taken |= {e["session"] for e in live}
    return taken


def generate_session_name(repo: Path, colab=None) -> str:
    """A fresh generated session name, checked against every name
    ever taken (see the section note)."""
    return naming.generate_name(_taken_session_names(repo, colab))


def parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def parse_balance(usage_text: str) -> float | None:
    m = re.search(r"balance:?\s*([0-9]+(?:\.[0-9]+)?)", usage_text, re.IGNORECASE)
    return float(m.group(1)) if m else None


_SESSION_READ_ATTEMPTS = 3
_SESSION_READ_BACKOFF_S = (2, 5)

# The reap pass runs on the hook's fixed budget (420 s) and repeats
# every couple of minutes, so its classifications take a smaller
# read budget than the interactive paths: a stall classifies
# unknown sooner — unknown is never read as dead — and the next
# pass retries. Two attempts at 45 s keeps one record's worst case
# near 90 s instead of the standard ~370 s.
_REAP_READ_ATTEMPTS = 2
_REAP_READ_TIMEOUT_S = 45


def session_state(colab, session: str, diag: dict | None = None, attempts: int | None = None, timeout: int = 120) -> str:
    """Tri-state: "alive" (listed), "absent" (list obtained, name not in
    it), "unknown" (the CLI call failed or returned nothing). Unknown must
    never be treated as dead: on 2026-10-29 the lease record vanished
    while its session lived, and a hiccup-reads-as-death path is the
    standing suspect class. Callers reconcile only on "absent".

    The read is retried (bounded, with backoff): a single transient
    failure — a subprocess timeout under the account's parallel load
    is the observed class — otherwise poisons a whole guard or
    reaper cycle with an unknown it did not earn (2026-11-07: a
    healthy, pulsing session classified extend-unknown and twice
    kept-unknown overnight). Only exhausted retries classify
    unknown, and `diag`, when passed, records why: the attempt
    count and the last failure's shape, so an unknown is never
    silent again. The auth prompt is definitive, never retried."""
    last_error = "no-attempt"
    tries = attempts if attempts is not None else _SESSION_READ_ATTEMPTS
    for attempt in range(tries):
        if attempt:
            time.sleep(_SESSION_READ_BACKOFF_S[min(attempt - 1, len(_SESSION_READ_BACKOFF_S) - 1)])
        try:
            out = colab("sessions", timeout=timeout).stdout
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:120] if str(exc) else type(exc).__name__
            continue
        if out and "Enter the authorization code" in out:
            # The CLI's interactive login prompt is not a session list: on
            # 2026-11-01 it read as "absent" here and a live lease's record
            # was deleted under it. The bin/colab-run guard now stops the
            # prompt from ever reaching callers; this is the same rule at
            # the seam, for any path that bypasses the wrapper.
            if diag is not None:
                diag.update({"attempts": attempt + 1, "read_error": "auth-prompt"})
            return "unknown"
        if not out or not out.strip():
            last_error = "empty-output"
            continue
        return "alive" if session in out else "absent"
    if diag is not None:
        diag.update({"attempts": tries, "read_error": last_error})
    return "unknown"


def session_alive(colab, session: str) -> bool:
    return session_state(colab, session) == "alive"


# --- Attributed spend accounting ----------------------------------------------
# Local why: the balance carries no per-source breakdown, so spend
# is built only from this repo's own lease segments; a late-detected
# death overcounts by at most the detection lag, and an unconfirmed
# rate contributes nothing. Mechanics: docs/lease-workflow.md.


def spend_journal_path(repo: Path) -> Path:
    return var_dir(repo) / "lease-spend-journal.jsonl"


def append_spend_segment(repo: Path, rec: dict, end_dt: datetime) -> bool:
    """Journal the completed segment of the session `rec` currently
    names: [rec.acquired_at, end_dt] at the record's rate snapshot.
    Called at every seam that ends a session's assignment under a
    record. Best-effort — a failed journal write never breaks the
    lifecycle action it records."""
    try:
        start = parse_dt(rec["acquired_at"])
    except (KeyError, ValueError):
        return False
    if end_dt <= start:
        return False
    rate = rec.get("rate_cu_per_hour")
    if rate is None and rec.get("shape"):
        rate = shape_rate(repo, rec["shape"])
    line = {"session": rec.get("session"), "shape": rec.get("shape"), "start": rec["acquired_at"], "end": end_dt.isoformat(), "rate": rate}
    if rec.get("caller"):
        line["caller"] = rec["caller"]
    try:
        p = spend_journal_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        return True
    except OSError:
        return False


def _journal_segments(repo: Path) -> list[dict]:
    out = []
    try:
        lines = spend_journal_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            seg = json.loads(line)
            out.append({"session": seg.get("session"), "caller": seg.get("caller"), "shape": seg.get("shape"), "start": parse_dt(seg["start"]), "end": parse_dt(seg["end"]), "rate": seg.get("rate")})
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def _overlap_cu(start: datetime, end: datetime, rate, period_start: datetime, period_end: datetime) -> float:
    if rate is None:
        return 0.0
    seconds = (min(end, period_end) - max(start, period_start)).total_seconds()
    return rate * seconds / 3600.0 if seconds > 0 else 0.0


def attributed_spend(repo: Path, now_dt: datetime | None = None) -> dict:
    """This repo's own lease burn for the current UTC day and month:
    {"day_spent", "month_spent", "sessions": {name: {...}},
    "callers": {caller: {...}}}, derived from the segment journal
    plus live records' running segments (see the accounting note
    above). A segment counts toward the session and caller it was
    written with; a caller-less segment counts toward its session
    and the totals only."""
    now = now_dt or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    totals = {"day": 0.0, "month": 0.0}
    per_session: dict[str, dict] = {}
    per_caller: dict[str, dict] = {}

    def add(session, caller, start, end, rate):
        d = _overlap_cu(start, end, rate, day_start, now)
        m = _overlap_cu(start, end, rate, month_start, now)
        if not d and not m:
            return
        totals["day"] += d
        totals["month"] += m
        if session:
            entry = per_session.setdefault(session, {"day_spent": 0.0, "month_spent": 0.0})
            entry["day_spent"] += d
            entry["month_spent"] += m
        if caller:
            entry = per_caller.setdefault(caller, {"day_spent": 0.0, "month_spent": 0.0})
            entry["day_spent"] += d
            entry["month_spent"] += m

    for seg in _journal_segments(repo):
        add(seg["session"], seg["caller"], seg["start"], seg["end"], seg["rate"])
    for rec in all_records(repo):
        try:
            start = parse_dt(rec["acquired_at"])
        except (KeyError, ValueError):
            continue
        rate = rec.get("rate_cu_per_hour")
        if rate is None and rec.get("shape"):
            rate = shape_rate(repo, rec["shape"])
        add(rec.get("session"), rec.get("caller"), start, now, rate)
    for bucket in (per_session, per_caller):
        for entry in bucket.values():
            entry["day_spent"] = round(entry["day_spent"], 2)
            entry["month_spent"] = round(entry["month_spent"], 2)
    return {"day_spent": round(totals["day"], 2), "month_spent": round(totals["month"], 2), "sessions": per_session, "callers": per_caller}


def budget_check(colab, repo: Path, cfg: dict, shape: dict, hours: float | None = None, caps_override: dict | None = None, now_dt: datetime | None = None, standing_lease: bool = False, exclude_session: str | None = None) -> dict:
    """Project this lease's window cost against the daily/monthly
    budgets. Spend is attributed (see the accounting note above):
    day/month figures are this repo's own lease burn from the
    segment journal plus live records — balance movements the
    repo did not cause (external usage, credit expirations) never
    enter them. The balance itself remains a floor: a window whose
    projected cost exceeds the whole balance refuses
    ("balance-insufficient"), so a real credit loss still cannot be
    overspent. The projected window is `hours` when given (the
    acquire's effective window), else the config max_hours.

    Pricing is per shape: the shape entry's rate_cu_per_hour (the
    catalog's measured rate for the shape). A shape whose rate is
    null has an unconfirmed rate: no projection is fabricated —
    the check returns ok with "rate_unconfirmed": true and a null
    projection.

    `caps_override` (a grant's snapshot caps, served shape) replaces
    the config caps for this check; without it the live config caps
    price the window, exactly as before the envelope.

    `standing_lease` marks the standing duty's lease: it
    additionally answers to standing.daily_cu_cap, checked against
    the standing caller's own day spend (reason "standing-budget");
    no other lease is ever refused by that sub-cap.

    Concurrency: every other live lease's remaining window (rate x
    time to its deadline) is already committed burn, added to this
    window's projection for the floor and the caps and reported as
    "committed_projected" — concurrent windows are priced as the
    sum they commit, so two windows must not each pass a guard
    their sum would fail. The other leases' elapsed burn is already
    inside day/month spent via their live records. For the daily
    check the committed sum is clipped to the current UTC day —
    the same clipping day_spent gets (owner ruling 2026-11-07):
    a multi-day window charges only today's portion against
    today's cap, and each later day re-prices as it arrives. The
    balance floor and the monthly check keep the full windows:
    a window may not promise more than the account holds, and
    the month is the monthly window's own horizon. Returns
    {"ok": bool, ...}."""
    try:
        usage_res = colab("usage", timeout=120)
    except Exception:  # noqa: BLE001
        # The usage read rides the stall-prone egress proxy: a timeout
        # is a refusal (the guard cannot price without a balance),
        # never a crash out of acquire — same rule as the heartbeat
        # and keepalive bounds.
        return {"ok": False, "reason": "usage-unavailable"}
    if "colab auth required" in (usage_res.stderr or ""):
        # The wrapper guard's stop-and-surface signal (2026-11-01):
        # a dead credential is its own refusal reason, never a parse
        # failure — the panel and the reaper both key on it.
        return {"ok": False, "reason": "auth-required"}
    usage = usage_res.stdout
    balance = parse_balance(usage)
    if balance is None:
        return {"ok": False, "reason": "usage-unparseable", "usage": usage.strip()[-200:]}
    rate = shape.get("rate_cu_per_hour")
    window = float(hours) if hours is not None else float(cfg.get("max_hours", 8))
    projected = rate * window if rate is not None else None
    # Envelope pricing (two-tier envelope plan): a caps override —
    # a grant's snapshot, in the served caps shape — replaces the
    # live config caps for the checks below, key by key; a null in
    # the snapshot is a real no-cap, exactly as in config.
    def cap(key, live):
        if caps_override is not None and key in caps_override:
            return caps_override[key]
        return live
    spend = attributed_spend(repo, now_dt)
    day_spent = spend["day_spent"]
    month_spent = spend["month_spent"]
    if projected is None:
        return {"ok": True, "balance": balance, "projected": None, "rate_unconfirmed": True, "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
    now = now_dt or datetime.now(timezone.utc)
    committed = projected + _committed_projection(repo, now, exclude_session)
    committed_extra = round(committed - projected, 2)
    if committed > balance:
        # The floor: caps throttle the repo's own burn, but no
        # window may promise more than the account actually holds.
        out = {"ok": False, "reason": "balance-insufficient", "projected": round(projected, 2), "balance": balance, "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
        if committed_extra:
            out["committed_projected"] = committed_extra
        return out
    daily_cap = cap("daily", cfg.get("daily_cu_budget"))
    monthly_cap = cap("monthly", cfg.get("monthly_cu_budget"))
    if daily_cap is not None and day_spent + committed > daily_cap:
        # Retry the comparison with other windows clipped to the
        # current UTC day before refusing on the daily cap: the
        # unclipped sum stays in the verdict as committed_full so
        # the overage's composition is never hidden.
        day_end = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        committed_day = projected + _committed_projection(repo, now, exclude_session, clip_end=day_end)
        if day_spent + committed_day > daily_cap:
            out = {"ok": False, "reason": "daily-budget", "day_spent": round(day_spent, 2), "projected": round(projected, 2), "cap": daily_cap, "balance": balance}
            clipped_extra = round(committed_day - projected, 2)
            if clipped_extra:
                out["committed_projected"] = clipped_extra
            if committed_extra != clipped_extra:
                out["committed_full"] = committed_extra
            return out
    if monthly_cap is not None and month_spent + committed > monthly_cap:
        out = {"ok": False, "reason": "monthly-budget", "month_spent": round(month_spent, 2), "projected": round(projected, 2), "cap": monthly_cap, "balance": balance}
        if committed_extra:
            out["committed_projected"] = committed_extra
        return out
    if standing_lease:
        standing_cfg = cfg.get("standing", {})
        standing_cap = cap("standing_daily", standing_cfg.get("daily_cu_cap"))
        standing_spent = spend["callers"].get(standing_caller(repo), {}).get("day_spent", 0.0)
        if standing_cap is not None and standing_spent + projected > float(standing_cap):
            return {"ok": False, "reason": "standing-budget", "standing_day_spent": round(standing_spent, 2), "projected": round(projected, 2), "cap": standing_cap, "balance": balance}
    out = {"ok": True, "balance": balance, "projected": round(projected, 2), "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
    if committed_extra:
        out["committed_projected"] = committed_extra
    return out


def _committed_projection(repo: Path, now_dt: datetime, exclude_session: str | None = None, clip_end: datetime | None = None) -> float:
    """Every other live lease's remaining committed window cost:
    rate x the time left to its record's deadline, summed. Zero for
    an expired window or an unconfirmed rate (nothing fabricated).
    The lease being priced excludes itself (exclude_session) — its
    own window is the projection it rides beside. `clip_end` caps
    each window at that instant (the daily check passes the end
    of the current UTC day, so a multi-day window charges only
    the portion that falls inside today)."""
    total = 0.0
    for rec in all_records(repo):
        if exclude_session is not None and rec.get("session") == exclude_session:
            continue
        try:
            end = parse_dt(rec["deadline"])
        except (KeyError, ValueError):
            continue
        if clip_end is not None and end > clip_end:
            end = clip_end
        remaining_h = (end - now_dt).total_seconds() / 3600.0
        if remaining_h <= 0:
            continue
        rate = rec.get("rate_cu_per_hour")
        if rate is None and rec.get("shape"):
            rate = shape_rate(repo, rec["shape"])
        if rate is not None:
            total += rate * remaining_h
    return total


def local_sessions_path() -> Path:
    """The local CLI's session-bindings file. Its contents are never
    printed or logged: the release fallback reads one endpoint from it,
    and a driver-1 handover merges a child binding into it."""
    return Path.home() / ".config" / "colab-cli" / "sessions.json"


def session_host(session: str) -> str | None:
    """The tunnel host a session is reached at, read from the local
    binding — no network call, so no approval is minted by reading it.
    The binding's `url` carries the host; a binding whose endpoint is
    itself a URL (older shapes, test fakes) parses the same way. None
    when the binding or a parseable host is absent: absence is not an
    error and never blocks the lifecycle."""
    binding = load_json(local_sessions_path(), {})
    if not isinstance(binding, dict):
        return None
    entry = binding.get(session)
    if not isinstance(entry, dict):
        return None
    for key in ("url", "endpoint"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw:
            host = urlparse(raw).hostname
            if host:
                return host
    return None


def created_shape_mismatch(session: str, shape: dict) -> dict | None:
    """Post-start shape assertion (session shape gate): the shape
    the server actually assigned, read from the local binding's
    endpoint host (an m-hm- host is high-RAM, an m-s- host is
    standard), against the shape's declared RAM class — the RAM
    dimension only, the one the incident proved can silently differ
    from the name the session was created under. A dict naming the
    mismatch, or None — including when the evidence is unreadable
    or unparseable: the assertion never acts on evidence it cannot
    read."""
    host = session_host(session)
    if not host:
        return None
    actual = _endpoint_shape(host)
    if actual is None:
        return None
    actual_high = actual == "high_ram"
    if actual_high == bool(shape.get("high_mem")):
        return None
    return {"action": "shape-mismatch", "session": session, "declared_high_mem": bool(shape.get("high_mem")), "actual_class": actual, "host": host}


# The fallback's unassign replay, run under the CLI's own interpreter.
# Local why: the stock unassign POST is bodiless and the tunnel
# endpoint answers 411; replaying the client's two steps with an
# explicit empty-JSON body (data=b"{}") is the form that succeeds.
UNASSIGN_FALLBACK_SCRIPT = """
import os
from urllib.parse import urljoin

from pydantic import BaseModel

from colab_cli.client import COLAB_XSRF_TOKEN_HEADER, TUN_ENDPOINT, GetUnassignRequest
from colab_cli.common import state

endpoint = os.environ["UNASSIGN_ENDPOINT"]
client = state.client
url = urljoin(client.colab_domain, f"{TUN_ENDPOINT}/unassign/{endpoint}")
resp = client._issue_request(url, schema=GetUnassignRequest)
headers = {COLAB_XSRF_TOKEN_HEADER["key"]: resp.token}
client._issue_request(url, method="POST", headers=headers, schema=BaseModel, data=b"{}")
"""


def stop_session_safe(colab, session: str) -> str:
    """Stop a session; never raise. `colab stop` hangs on a wedged kernel
    (measured) and its own unassign POST is bodiless (411, above), so
    fall back to a direct server-side unassign through the CLI's own
    library using the endpoint in the local binding. The fallback's
    verdict is verified against the server-side sessions read, never
    assumed from the call: "unassigned-fallback" is returned only once
    the session no longer reads alive."""
    try:
        r = colab("stop", "-s", session, timeout=120)
        if r.returncode == 0 and not session_alive(colab, session):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        binding = load_json(local_sessions_path(), {})
        endpoint = binding[session]["endpoint"]
        venv_py = Path.home() / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "python"
        env = dict(os.environ)
        env["UNASSIGN_ENDPOINT"] = endpoint
        for key in ("NO_PROXY", "no_proxy"):
            suffix = "google.com,googleapis.com,googleusercontent.com,colab.dev"
            env[key] = f"{env[key]},{suffix}" if env.get(key) else suffix
        r = subprocess.run(
            [str(venv_py), "-c", UNASSIGN_FALLBACK_SCRIPT],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if r.returncode == 0:
            # A landed unassign can take a few seconds to disappear
            # from the assignments read; confirm over a short window
            # before reporting either way.
            for attempt in range(3):
                if not session_alive(colab, session):
                    return "unassigned-fallback"
                if attempt < 2:
                    time.sleep(2)
    except Exception:  # noqa: BLE001
        pass
    return "stop-failed"


# The heartbeat read is deliberately short-bounded: the file
# channel can stall indefinitely, so a stall degrades the age to
# unknown (None) instead of wedging status/state reads behind it.
HEARTBEAT_READ_TIMEOUT_S = 10


def heartbeat_age(colab, repo: Path, session: str) -> float | None:
    """Seconds since the keepalive's last heartbeat, via the file channel.
    None when there is no readable heartbeat (loop never ran, log gone,
    or the download failed) — including a download that stalls past
    HEARTBEAT_READ_TIMEOUT_S, which reads as unknown, not as an error."""
    dest = var_dir(repo) / "keepalive-tail.log"
    try:
        r = colab("download", "-s", session, "/content/keepalive.log", str(dest), timeout=HEARTBEAT_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return None
        lines = dest.read_text(errors="replace").strip().split("\n")
        ts = json.loads(lines[-1])["ts"]
        return max(0.0, datetime.now(timezone.utc).timestamp() - float(ts))
    except Exception:  # noqa: BLE001
        return None


def prime_session_host(colab, repo: Path, session: str) -> dict:
    """One cheap session-targeting call through the funnel, made by an
    initiating entry point right after an acquire changed the host
    (initiation-time approval plan, Step 2): the attempt itself is
    the product — it surfaces this caller scope's approval card for
    the new tunnel host in the same minute as the initiation, while
    the operator is present, instead of the scope discovering the
    host on its next background call. The probe is the heartbeat
    download (bounded, already the lease's liveness read); its
    outcome is reported, never raised, and never blocks the acquire
    result it rides on."""
    try:
        age = heartbeat_age(colab, repo, session)
    except Exception:  # noqa: BLE001
        return {"primed": False}
    return {"primed": True, "heartbeat_age_s": round(age, 1) if age is not None else None}


# The keepalive launch is bounded and non-raising for the same
# reason: its upload rides the stall-prone file channel. A failed
# launch is a result ("launch-failed"), never an exception — the
# lease stands and the next reap retries.
KEEPALIVE_UPLOAD_TIMEOUT_S = 30
KEEPALIVE_LAUNCH_TIMEOUT_S = 90


# Renewal: a window may outlive several sessions. The renewal
# kit's install rides the same stall-prone file channel as the
# keepalive, so its timeouts are bounded the same way and a failed
# install is a result, never an exception. Mechanics:
# docs/lease-workflow.md.
SESSION_CAP_HOURS = 12.0
RENEWAL_MARGIN = timedelta(minutes=30)
KIT_UPLOAD_TIMEOUT_S = 30
KIT_LAUNCH_TIMEOUT_S = 90
HANDOVER_READ_TIMEOUT_S = 30
RENEWAL_KIT_REMOTE = "/content/renewal-kit"
BRIDGE_KIT_REMOTE = "/content/bridge-kit"
BRIDGE_REMOTE = "/content/bridge"
BRIDGE_PROTOCOL = 2
BRIDGE_READ_TIMEOUT_S = 10
BRIDGE_INSTALL_COOLDOWN_S = 600
BRIDGE_HEARTBEAT_STALE_S = 45


def ensure_keepalive(colab, repo: Path, session: str) -> dict:
    """Launch the detached keepalive loop on the session (single-instance
    on the VM side; safe to call repeatedly). The loop exists because an
    idle Colab runtime is reaped — the first away lease died at ~3 h —
    so a leased session must never sit truly idle (operator ruling, 2026-10-30).
    Never raises: a stalled or failed launch returns launch-failed and
    the caller carries on (the reaper retries on its next cycle)."""
    cfg = load_config(repo).get("keepalive", {})
    if not cfg.get("enabled"):
        return {"keepalive": "disabled"}
    try:
        colab("upload", "-s", session, str(repo / "snippets" / "keepalive.py"), "/content/keepalive.py", timeout=KEEPALIVE_UPLOAD_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"keepalive": "launch-failed", "stage": "upload"}
    launcher = var_dir(repo) / "launch-keepalive.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    max_cycles = os.environ.get("KEEPALIVE_MAX_CYCLES", "0")
    interval = os.environ.get("KEEPALIVE_INTERVAL_SECONDS", str(cfg.get("interval_seconds", 60)))
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        f"env.update({{'COLAB_RELEASE_TAG': 'keepalive', 'KEEPALIVE_MAX_CYCLES': '{max_cycles}', 'KEEPALIVE_INTERVAL_SECONDS': '{interval}'}})\n"
        "subprocess.Popen(['nohup', 'python3', 'keepalive.py'], stdout=open('keepalive.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('keepalive launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KEEPALIVE_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"keepalive": "launch-failed", "stage": "launch"}
    return {"keepalive": "launched" if r.returncode == 0 else "launch-failed", "stdout_tail": r.stdout.strip()[-120:]}


def keepalive_fresh(colab, repo: Path, session: str) -> tuple[bool, float | None]:
    cfg = load_config(repo).get("keepalive", {})
    age = heartbeat_age(colab, repo, session)
    return (age is not None and age <= float(cfg.get("stale_seconds", 600))), age


def kernel_pulse(colab, repo: Path, session: str) -> str:
    """One trivial execution in the session's kernel. The detached
    keepalive loop cannot produce kernel activity, and lease #2 died at
    ~6 h with the loop beating perfectly — so the reaper pulses the
    kernel itself (operator ruling, 2026-10-30). Returns ok | busy | failed; a
    local timeout reads as busy, because a kernel that cannot take the
    pulse is a kernel already executing, which is the activity being
    proven. The pulse body is an inert assignment."""
    pulse = var_dir(repo) / "lease-pulse.py"
    pulse.parent.mkdir(parents=True, exist_ok=True)
    pulse.write_text("__lease_pulse__ = 'ok'\nprint('lease pulse')\n")
    try:
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(pulse), timeout=90)
        return "ok" if r.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        return "busy"
    except Exception:  # noqa: BLE001
        return "failed"


def _shape_spec_for(repo: Path, shape_id: str | None, rec: dict | None = None) -> dict:
    """A shape's descriptor fields ({gpu, tpu, high_mem}): the
    catalog entry when the catalog knows the shape, else the
    descriptor the lease record itself carries (stamped at
    creation/translation, so a lease's own shape survives a cold
    catalog cache), else plain CPU."""
    entry = catalog_shapes(repo).get(shape_id) if shape_id else None
    if entry is not None:
        return {"gpu": entry.get("gpu"), "tpu": entry.get("tpu"), "high_mem": bool(entry.get("high_mem"))}
    if rec is not None and isinstance(rec.get("shape_spec"), dict):
        spec = rec["shape_spec"]
        return {"gpu": spec.get("gpu"), "tpu": spec.get("tpu"), "high_mem": bool(spec.get("high_mem"))}
    return {"gpu": None, "tpu": None, "high_mem": False}


def install_renewal_kit(colab, repo: Path, rec: dict, now_dt: datetime | None = None) -> dict:
    """Install the renewal kit in a renew-flagged lease's session
    (driver 1 of the lease-extension plan): snippets/renewal.py plus
    its guard, the window config, and the account token — staged under
    /content/renewal-kit and placed by the snippet's own --setup step,
    which builds the CLI venv in-VM and moves the token to its
    canonical path at mode 600. Token contents are never printed,
    logged, or written anywhere but the two canonical locations (the
    exposure is the plan's stated design property, proven 2026-11-01).

    Bounded and non-raising like the keepalive launch it rides beside:
    a stalled or failed step returns {"kit": "install-failed", ...} —
    the lease stands, and the reaper's driver 2 covers a kit that
    never comes up. {"kit": "installed"} means the files were
    delivered and the setup launch was accepted; the snippet's own
    ready marker in-VM is the deeper proof."""
    session = rec["session"]
    token_src = Path.home() / ".config" / "colab-cli" / "token.json"
    if not token_src.exists():
        return {"kit": "install-failed", "stage": "token-missing"}
    spec = _shape_spec_for(repo, rec.get("shape"), rec)
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    cfg_local = var / f"renewal-config-{session}.json"
    cfg_local.write_text(json.dumps({
        "shape": rec.get("shape"),
        "session": session,
        "deadline": rec.get("deadline"),
        "acquired_at": rec.get("acquired_at"),
        "cap_seconds": SESSION_CAP_HOURS * 3600,
        "margin_seconds": RENEWAL_MARGIN.total_seconds(),
        "high_mem": spec["high_mem"],
        "gpu": spec["gpu"],
        "tpu": spec["tpu"],
    }, indent=2))
    if not _ensure_remote_dirs(colab, repo, session, [RENEWAL_KIT_REMOTE], "renewal"):
        return {"kit": "install-failed", "stage": "mkdir"}
    uploads = [
        (str(repo / "snippets" / "renewal.py"), f"{RENEWAL_KIT_REMOTE}/renewal.py"),
        (str(repo / "snippets" / "_remote_guard.py"), f"{RENEWAL_KIT_REMOTE}/_remote_guard.py"),
        (str(token_src), f"{RENEWAL_KIT_REMOTE}/token.json"),
        (str(cfg_local), f"{RENEWAL_KIT_REMOTE}/renewal.json"),
    ]
    for src, dest in uploads:
        try:
            r = colab("upload", "-s", session, src, dest, timeout=KIT_UPLOAD_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return {"kit": "install-failed", "stage": "upload"}
        if r.returncode != 0:
            return {"kit": "install-failed", "stage": "upload"}
    launcher = var / f"launch-renewal-kit-{session}.py"
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        "env['COLAB_RELEASE_TAG'] = 'renewal-kit'\n"
        f"subprocess.Popen(['nohup', 'python3', '{RENEWAL_KIT_REMOTE}/renewal.py', '--setup', '--config', '{RENEWAL_KIT_REMOTE}/renewal.json'], stdout=open('{RENEWAL_KIT_REMOTE}/setup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('renewal kit setup launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KIT_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"kit": "install-failed", "stage": "launch"}
    if r.returncode != 0:
        return {"kit": "install-failed", "stage": "launch"}
    return {"kit": "installed"}


def acquire(colab, repo: Path, shape_id: str, now_dt: datetime | None = None, hours: float | None = None, max_hours_cap: float | None = None, via: str | None = None, caller: str | None = None, duty: bool = False, standing: bool = False, create: bool = False) -> dict:
    """Adopt a live lease at this shape or create one. The only
    entry point that ever creates a session under the lease model.
    A session is shared capacity, not a caller's possession: any
    caller may use an existing available (live, unexpired) lease
    at the shape — its own first, then an unstamped one, then any
    other caller's. The record keeps its creator's caller stamp as
    history; what attribution needs is who called, and every use
    by another caller is journaled as a claim (below), never a
    restamp. A fresh lease is created only when no available lease
    at the shape can be used, or when the call is a capacity duty
    (duty/standing): a demand the hook consumes and the standing
    duty exist to add or hold a lease of their own, so they adopt
    only their own (or an unstamped) record and otherwise create.
    An explicit create (create=True — the panel's Start) never
    adopts at all: starting is creating a parallel lease, and
    using the current one is extend's job.

    `hours` sets this lease's window: the deadline is now + hours
    and the budget projection prices that window, exactly as given —
    an explicit window is the operator's instruction, never silently
    shortened. Without `hours` the window is the config max_hours.
    A caller may pass `max_hours_cap` to clamp its own windows (the
    standing duty passes its session cap); that parameter is the
    only clamp.

    Envelope attribution (two-tier envelope plan): when a grant is
    active and this shape is inside its scope, a created or
    adopted record is stamped with the grant's id (`envelope_id` =
    its granted_at). A created record also carries the requester
    tag `via` when the caller supplies one (a demand request's tag,
    "panel", "runner", "standing"); an adopted record keeps the
    via of whoever created it. While a grant is active, an
    out-of-scope creation carries neither stamp — attribution is
    envelope-scoped, so a lease outside the envelope is
    indistinguishable from an ordinary lease.

    Caller attribution (caller-attribution plan): `caller` is the
    resolved thread claim from the wrapper seam (the seams
    validate; the core stamps what it is given). A created record
    carries it; an adopted record keeps its creator's caller,
    filling the field only when the record predates attribution
    and the adopting call supplies one. A use by a different
    caller is journaled as a claim on the session — attribution
    records who called; it never gates use."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    shapes = catalog_shapes(repo, colab)
    if shape_id not in shapes:
        return {"action": "unknown-shape", "shape": shape_id}
    shape = shapes[shape_id]
    grant = active_grant(repo, now_dt)
    in_scope = shape_in_grant(repo, grant, shape_id)
    at_shape = [r for r in all_records(repo) if r.get("shape") == shape_id]
    if create:
        # An explicit create mints a parallel lease: the adoption
        # pool is empty by construction, so the create path below
        # runs with its budget guard and shape gate unchanged.
        pool = []
    elif duty or standing:
        # Capacity duties adopt only their own (or an unstamped)
        # record; every other call may use any available lease.
        pool = [r for r in at_shape if r.get("caller") in (caller, None)]
    else:
        pool = at_shape
    # Preference: the caller's own record, then an unstamped one
    # (adoptable by the first caller to ask — the adoption stamps
    # it, below), then any other caller's; latest deadline breaks
    # ties within a tier.
    pool.sort(key=lambda r: (caller is not None and r.get("caller") == caller, r.get("caller") is None, r.get("deadline") or ""), reverse=True)
    rec = None
    for cand in pool:
        session = cand["session"]
        state = session_state(colab, session)
        if state == "unknown":
            # Never stop, delete, or recreate on an unreadable session
            # list: on 2026-11-01 an auth-dead CLI made a live lease
            # read as dead through the old boolean check and its
            # record was destroyed under a healthy session (the
            # 2026-10-29 incident's class). The record stays exactly
            # as it is; a later call retries.
            return {"action": "acquire-unknown", "record": cand, "session": session}
        if now_dt < parse_dt(cand["deadline"]) and state == "alive":
            rec = cand
            break
        # Not usable. The caller's own (or an unstamped) record is
        # cleared here exactly as before, so its window reopens
        # for the create below; another caller's dead or expired
        # lease is left for its owner and the reaper — using a
        # session never destroys someone else's record.
        if cand.get("caller") in (caller, None):
            stop_session_safe(colab, session)  # expired or confirmed dead: clear before recreating
            if session_state(colab, session) == "alive":
                # The stop did not take: keep the record of a session that
                # is still billing (release()'s rule, same reason).
                return {"action": "acquire-stop-failed", "record": cand, "session": session}
            append_spend_segment(repo, cand, now_dt or datetime.now(timezone.utc))
            delete_record(repo, session)
    if rec is not None:
        session = rec["session"]
        rec["last_used_at"] = now_dt.isoformat()
        newly_attributed = in_scope and rec.get("envelope_id") != grant["granted_at"]
        if newly_attributed:
            rec["envelope_id"] = grant["granted_at"]
        if caller and not rec.get("caller"):
            rec["caller"] = caller
        save_record(repo, rec)
        if newly_attributed:
            _note_envelope_lease_started(repo, rec)
        if _capture_session_host(repo, rec, "adopted", spool_on_first=False):
            save_record(repo, rec)
        fresh, _age = keepalive_fresh(colab, repo, session)
        ka = {"keepalive": "ok"} if fresh else ensure_keepalive(colab, repo, session)
        out = {"action": "adopted", "record": rec, **ka}
        if caller and rec.get("caller") not in (None, caller):
            # Riding another caller's lease: the use is journaled
            # as this caller's claim on the session; the record's
            # creator stamp is untouched.
            append_caller_claim(repo, session, caller, "acquire")
            out["riding"] = True
            out["owner_caller"] = rec.get("caller")
        return out
    effective_hours = float(hours) if hours is not None else float(cfg.get("max_hours", 8))
    if max_hours_cap is not None:
        # Only a caller passing its own cap clamps the window. Until
        # 2026-11-01 the config max_hours clamped every acquire, so
        # an ordered 24 h window silently came up as 8 h.
        effective_hours = min(effective_hours, float(max_hours_cap))
    # In-scope acquires price against the envelope's snapshot caps,
    # never the live config (Step 3): the grant tap authorized those
    # numbers. Out-of-scope and no-grant pricing is unchanged.
    caps_override = grant_caps(grant) if in_scope else None
    budget = budget_check(colab, repo, cfg, shape, hours=effective_hours, caps_override=caps_override, now_dt=now_dt, standing_lease=standing)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    # Session shape gate: the creation branch only — adopting
    # or reusing a live session above is never gated. `duty`
    # marks the lease duty creating on a demand (auto_acquire
    # is its only passer): the shape check still applies, the
    # agent creation denial does not.
    refusal = enforce_session_admission(repo, shape_id, via=via, shape=shape, seam="acquire", caller=caller, duty=duty, standing=standing)
    if refusal is not None:
        return refusal
    session = generate_session_name(repo, colab)
    args = ["new", "-s", session] + _session_shape_args(shape)
    r = colab(*args, timeout=600)
    if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
        return {"action": "create-failed", "error": (r.stderr or r.stdout).strip()[-300:]}
    if r.returncode == 0 and session_gate_policy(repo)["enabled"]:
        # Post-start shape assertion: the shape the CLI was
        # handed is not proof of the shape the server assigned
        # (the 2026-11-04 Standard session bound under a
        # High-RAM profile name). Only a fresh create is
        # asserted — the "already" path above is a reuse, which
        # is never gated. A proven mismatch is stopped and
        # refused: no lease record is written for a session of
        # the wrong shape. The assertion is part of the gate: a
        # policy disabled in config stands it down too.
        mismatch = created_shape_mismatch(session, shape) if session_state(colab, session) == "alive" else None
        if mismatch is not None:
            stop = stop_session_safe(colab, session)
            append_gate_journal(repo, {"kind": "shape-mismatch", "seam": "acquire", "shape": shape_id, "declared_high_mem": mismatch["declared_high_mem"], "actual_class": mismatch["actual_class"], "stop": stop})
            return {**mismatch, "shape": shape_id, "stop": stop}
    deadline = now_dt + timedelta(hours=effective_hours)
    rec = {
        "session": session,
        "shape": shape_id,
        "shape_spec": {"gpu": shape.get("gpu"), "tpu": shape.get("tpu"), "high_mem": bool(shape.get("high_mem"))},
        "rate_cu_per_hour": shape.get("rate_cu_per_hour"),
        "acquired_at": now_dt.isoformat(),
        "deadline": deadline.isoformat(),
        "last_used_at": now_dt.isoformat(),
        "jobs": [],
        "budget_at_acquire": budget,
    }
    if in_scope:
        rec["envelope_id"] = grant["granted_at"]
    if via and (grant is None or in_scope):
        rec["via"] = via
    if caller:
        rec["caller"] = caller
    if effective_hours > SESSION_CAP_HOURS:
        # A window longer than one session's life is a window lease
        # from birth (lease-extension plan): the reaper renews the
        # session under this same deadline.
        rec["renew"] = True
    save_record(repo, rec)
    ka = ensure_keepalive(colab, repo, session)
    pulse_result = kernel_pulse(colab, repo, session)
    rec["last_pulse_at"] = now_dt.isoformat()
    rec["last_pulse_result"] = pulse_result
    save_record(repo, rec)
    host_changed = _capture_session_host(repo, rec, "acquire", spool_on_first=True)
    if host_changed:
        save_record(repo, rec)
    out = {"action": "created", "record": rec, "pulse": pulse_result, **ka}
    if host_changed:
        out["host_changed"] = True
        out["host"] = rec["host"]
    if rec.get("renew"):
        kit = install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            save_record(repo, rec)
    if in_scope:
        _note_envelope_lease_started(repo, rec)
    return out


def extend(colab, repo: Path, session: str, hours: float, now_dt: datetime | None = None, caller: str | None = None) -> dict:
    """Move one lease's deadline later by `hours` without touching
    its session (plan .plans/2026-11-01-lease-extension.md). The
    budget guard prices only the delta (the shape's rate x hours)
    before anything changes; a refusal changes nothing. A
    successful extension sets renew: true — the lease becomes a
    window lease and the reaper renews its session as needed to
    reach the new end — and, while the session is alive, installs
    the renewal kit if it is not already installed.

    Refusals: extend-no-lease (no record for the session, or the
    session is confirmed absent on a non-window lease — the lease
    is over and a fresh start is a new lease), extend-unknown
    (session state unreadable; never act on an unreadable list),
    extend-standing-managed (the standing lease's lifetime belongs
    to the grant), refused-budget."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    rec = load_record(repo, session)
    if rec is None:
        return {"action": "extend-no-lease"}
    if rec.get("standing") and grant_active(load_grant(repo), now_dt):
        return {"action": "extend-standing-managed"}
    diag: dict = {}
    state = session_state(colab, session, diag)
    if state == "unknown":
        return {"action": "extend-unknown", "session": session, "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent" and not rec.get("renew"):
        return {"action": "extend-no-lease", "session": session}
    shape = catalog_shapes(repo).get(rec.get("shape"))
    if shape is None:
        shape = {"id": rec.get("shape"), "rate_cu_per_hour": rec.get("rate_cu_per_hour"), **_shape_spec_for(repo, rec.get("shape"), rec)}
    budget = budget_check(colab, repo, cfg, shape, hours=float(hours), now_dt=now_dt, standing_lease=bool(rec.get("standing")), exclude_session=session)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    old_deadline = rec["deadline"]
    new_deadline = (parse_dt(old_deadline) + timedelta(hours=float(hours))).isoformat()
    rec["deadline"] = new_deadline
    rec["renew"] = True
    if caller and not rec.get("caller"):
        # Caller attribution: an extension by the owning thread
        # fills a pre-attribution record's caller; an attributed
        # record keeps its creator's caller (as on adopt).
        rec["caller"] = caller
    # An operator extension is the latest action on the
    # deadline: it clears any operator shorten stamp, so the
    # extend guard stops skipping the lease (the stamp means
    # the operator pulled the deadline in on purpose;
    # extending says otherwise). The stamp is never a cap.
    for key in ("shortened_at", "shortened_by", "shortened_from"):
        rec.pop(key, None)
    save_record(repo, rec)
    out = {"action": "extended", "session": session, "shape": rec.get("shape"), "hours": float(hours), "old_deadline": old_deadline, "new_deadline": new_deadline, "record": rec, "budget": budget}
    if caller:
        out["caller"] = caller
    if state == "alive" and rec.get("kit") != "installed":
        kit = install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            save_record(repo, rec)
    return out


def extend_grant(colab, repo: Path, hours: float, now_dt: datetime | None = None) -> dict:
    """Move the two-tier grant's `until` later by `hours`; the
    standing duty follows the new end on its own. Only the standing
    lease's delta is priced (its shape's rate x hours) — against
    the standing sub-cap and the global caps; the burst is neither
    extended nor charged. An absent or expired grant refuses
    (grant-inactive): a new window is a new grant."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    grant = load_grant(repo)
    if not grant_active(grant, now_dt):
        return {"action": "grant-inactive"}
    shape_id = standing_shape(repo)
    shape = catalog_shapes(repo).get(shape_id)
    standing_rec = standing_record(repo)
    if shape is None:
        shape = {"id": shape_id, "rate_cu_per_hour": standing_rec.get("rate_cu_per_hour") if standing_rec else shape_rate(repo, shape_id), **_shape_spec_for(repo, shape_id, standing_rec)}
    budget = budget_check(colab, repo, cfg, shape, hours=float(hours), now_dt=now_dt, standing_lease=True, exclude_session=standing_rec.get("session") if standing_rec else None)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    old_until = grant["until"]
    grant["until"] = (parse_dt(old_until) + timedelta(hours=float(hours))).isoformat()
    p = grant_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=2))
    return {"action": "grant-extended", "hours": float(hours), "old_until": old_until, "new_until": grant["until"], "grant": grant, "budget": budget}


def adopt_orphan(colab, repo: Path, session: str, deadline=None, hours: float | None = None, caller: str | None = None, shape: str | None = None, now_dt: datetime | None = None) -> dict:
    """Bring one orphaned session back under the lease: plant a
    real record for the live session from its derived evidence,
    or at a deadline (and, when the class evidence is ambiguous,
    a shape) the operator plants explicitly. The record carries
    the acquire field shape plus the adoption provenance
    (adopted, adopted_at, deadline_source), so the reaper, the
    guard, and the budget treat it as an ordinary lease from
    here. Caller validation happens at the wrapper seam
    (caller_claim_error there); the core stamps the creator the
    evidence names and journals the operator's claim as a ride
    when the two differ, exactly as acquire's adoption path
    does. The restore is exempt from price-check when it
    restores the evidenced window — it creates no new
    commitment; a planted deadline beyond the evidenced one
    prices only the delta, and with no evidence the delta is
    measured from now."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    if load_record(repo, session) is not None:
        return {"action": "adopt-has-record", "session": session}
    if session.startswith("bridge-"):
        return {"action": "adopt-bridge-managed", "session": session}
    handover = load_json(var_dir(repo) / "lease-handover.json", None)
    if isinstance(handover, dict) and handover.get("child_session") == session:
        return {"action": "adopt-handover-pending", "session": session}
    live = live_sessions(colab, repo)
    if live is None:
        return {"action": "adopt-unknown", "session": session}
    entry = next((e for e in live if e.get("session") == session), None)
    if entry is None:
        return {"action": "adopt-absent", "session": session}
    census = derive_orphan(repo, entry)
    evidenced_deadline = _dt_or_none(census.get("derived_deadline"))

    # Shape resolution (owner ruling on the plan's open
    # question): an evidenced shape is exact and is never
    # overridden; the operator's --shape is consulted only
    # when derivation cannot evidence one, and is validated
    # against the catalog; otherwise the live class must name
    # exactly one candidate, or the adoption refuses with the
    # ambiguity as evidence.
    shapes = catalog_shapes(repo, colab)
    if census.get("shape_evidenced"):
        shape_id = census["shape"]
        if shape is not None and shape != shape_id:
            return {"action": "adopt-shape-conflict", "session": session, "evidenced_shape": shape_id, "shape": shape}
    elif shape is not None:
        if shape not in shapes:
            return {"action": "adopt-shape-unknown", "session": session, "shape": shape}
        shape_id = shape
    elif census.get("shape"):
        shape_id = census["shape"]
    else:
        return {"action": "adopt-shape-ambiguous", "session": session, "shape_class": entry.get("shape_class"), "shape_candidates": census.get("shape_candidates", [])}
    shape_entry = shapes.get(shape_id)
    if shape_entry is None:
        shape_entry = {"id": shape_id, "rate_cu_per_hour": shape_rate(repo, shape_id), **(census.get("shape_spec") or _shape_spec_for(repo, shape_id))}

    if deadline is not None:
        target = _dt_or_none(deadline) if isinstance(deadline, str) else deadline
        if target is not None and target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        source = "manual"
    elif hours is not None:
        target = now_dt + timedelta(hours=float(hours))
        source = "hours"
    elif evidenced_deadline is not None:
        target = evidenced_deadline
        source = census.get("deadline_source") or "renewal-config"
    else:
        return {"action": "adopt-no-deadline", "session": session, "evidence": census}
    if target is None:
        return {"action": "adopt-no-deadline", "session": session, "evidence": census}
    if evidenced_deadline is not None and target < evidenced_deadline:
        # Restore rule, not an invariant: adoption puts back
        # the lease the evidence describes, it never edits it.
        # An earlier deadline would quietly rewrite the lease
        # being restored; the operator adopts at the evidenced
        # deadline and shortens explicitly afterward if that
        # is what they mean.
        return {"action": "adopt-deadline-shortens", "session": session, "deadline": target.isoformat(), "evidenced_deadline": evidenced_deadline.isoformat(), "deadline_source": census.get("deadline_source")}
    if target <= now_dt:
        return {"action": "adopt-deadline-past", "session": session, "deadline": target.isoformat(), "deadline_source": source, "derived_deadline": census.get("derived_deadline"), "derived_source": census.get("deadline_source")}
    budget = None
    base = evidenced_deadline if evidenced_deadline is not None else now_dt
    if target > base:
        delta_hours = (target - base).total_seconds() / 3600.0
        budget = budget_check(colab, repo, cfg, shape_entry, hours=delta_hours, now_dt=now_dt, exclude_session=session)
        if not budget["ok"]:
            return {"action": "refused-budget", "budget": budget}

    diag: dict = {}
    state = session_state(colab, session, diag)
    if state == "unknown":
        return {"action": "adopt-unknown", "session": session, "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent":
        return {"action": "adopt-absent", "session": session}

    acquired_at = census.get("acquired_at") or now_dt.isoformat()
    record_caller = census.get("creator_caller") or caller
    rec: dict = {
        "session": session,
        "shape": shape_id,
        "shape_spec": census.get("shape_spec") or _shape_spec_for(repo, shape_id),
        "rate_cu_per_hour": shape_rate(repo, shape_id),
        "acquired_at": acquired_at,
        "deadline": target.isoformat(),
        "last_used_at": now_dt.isoformat(),
        "jobs": list(census.get("jobs_in_flight") or []),
        "adopted": True,
        "adopted_at": now_dt.isoformat(),
        "deadline_source": source,
    }
    if record_caller:
        rec["caller"] = record_caller
    acquired_dt = _dt_or_none(acquired_at) or now_dt
    if (target - acquired_dt).total_seconds() > SESSION_CAP_HOURS * 3600 or census.get("kit_installed"):
        # The window outlives any one runtime, or the kit is
        # already installed in the session: either way the
        # renewal driver owns the successor line.
        rec["renew"] = True
    if census.get("kit_installed"):
        rec["kit"] = "installed"
    if budget is not None:
        rec["budget_at_adopt"] = budget
    if load_record(repo, session) is not None:
        # Re-checked at the last moment, as acquire does: a
        # record that appeared mid-adoption wins.
        return {"action": "adopt-has-record", "session": session}
    save_record(repo, rec)
    if _capture_session_host(repo, rec, "adopted", spool_on_first=False):
        save_record(repo, rec)
    if caller and caller != record_caller:
        append_caller_claim(repo, session, caller, "adopt")
    clear_unattributed(repo, session)
    out: dict = {"action": "adopted", "record": rec, "deadline_source": source}
    if budget is not None:
        out["budget"] = budget
    if rec.get("renew") and not census.get("kit_installed"):
        kit = install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            save_record(repo, rec)
    return out


def shorten(colab, repo: Path, session: str, deadline=None, hours: float | None = None, caller: str | None = None, now_dt: datetime | None = None) -> dict:
    """Move one lease's deadline earlier: the operator's
    explicit edit, extend's mirror. The target is an absolute
    --deadline or the current deadline minus --hours; it must
    land strictly earlier than the current deadline (a later
    target is extend's job, refused here by name) and strictly
    in the future (a past target is a release wearing a
    deadline's clothes). No budget price-check: shortening
    only reduces the committed projection. On success the
    record is stamped (shortened_at, shortened_by,
    shortened_from): while the stamp stands, the extend guard
    skips the lease — it never re-extends what the operator
    deliberately pulled in. The stamp is not a cap: a later
    operator extend clears it (extend's save deletes the
    stamp), and the operator's latest action wins. The
    renewal kit is reinstalled when the record carries one,
    so the kit's planted config learns the new deadline; a
    kit failure is reported in the verdict and the move
    stands, as with extend."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    rec = load_record(repo, session)
    if rec is None:
        return {"action": "shorten-no-lease", "session": session}
    if rec.get("standing") and grant_active(load_grant(repo), now_dt):
        return {"action": "shorten-standing-managed", "session": session}
    diag: dict = {}
    state = session_state(colab, session, diag)
    if state == "unknown":
        return {"action": "shorten-unknown", "session": session, "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent" and not rec.get("renew"):
        return {"action": "shorten-no-lease", "session": session}
    if (deadline is None) == (hours is None):
        return {"action": "shorten-no-target", "session": session}
    current = parse_dt(rec["deadline"])
    if deadline is not None:
        target = _dt_or_none(deadline) if isinstance(deadline, str) else deadline
        if target is not None and target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    else:
        target = current - timedelta(hours=float(hours))
    if target is None or target >= current:
        return {"action": "shorten-not-earlier", "session": session, "deadline": rec["deadline"], "target": target.isoformat() if target else None}
    if target <= now_dt:
        return {"action": "shorten-deadline-past", "session": session, "deadline": rec["deadline"], "target": target.isoformat()}
    old_deadline = rec["deadline"]
    rec["deadline"] = target.isoformat()
    rec["shortened_at"] = now_dt.isoformat()
    rec["shortened_by"] = caller
    rec["shortened_from"] = old_deadline
    save_record(repo, rec)
    out: dict = {"action": "shortened", "session": session, "shape": rec.get("shape"), "old_deadline": old_deadline, "new_deadline": rec["deadline"], "record": rec}
    if hours is not None:
        out["hours"] = float(hours)
    if caller:
        out["caller"] = caller
    if state == "alive" and rec.get("kit") == "installed":
        out["kit"] = install_renewal_kit(colab, repo, rec, now_dt)
    return out


def _job_record(repo: Path, run_id: str | None = None) -> dict | None:
    """The record a job call belongs to: the one already tracking the
    run, else the most recently used lease (existing callers invoke
    this immediately after acquiring theirs, and acquire stamps
    last_used_at)."""
    recs = all_records(repo)
    if not recs:
        return None
    if run_id is not None:
        for rec in recs:
            if run_id in rec.get("jobs", []):
                return rec
    return max(recs, key=lambda r: r.get("last_used_at", ""))


def touch_job(repo: Path, run_id: str, now_dt: datetime | None = None, session: str | None = None) -> None:
    rec = load_record(repo, session) if session is not None else _job_record(repo, run_id)
    if not rec:
        return
    now_dt = now_dt or datetime.now(timezone.utc)
    if run_id not in rec.setdefault("jobs", []):
        rec["jobs"].append(run_id)
    rec["last_used_at"] = now_dt.isoformat()
    save_record(repo, rec)


def finish_job(repo: Path, run_id: str, now_dt: datetime | None = None, session: str | None = None) -> None:
    rec = load_record(repo, session) if session is not None else _job_record(repo, run_id)
    if not rec:
        return
    now_dt = now_dt or datetime.now(timezone.utc)
    rec["jobs"] = [j for j in rec.get("jobs", []) if j != run_id]
    rec["last_used_at"] = now_dt.isoformat()
    save_record(repo, rec)


def release(colab, repo: Path, reason: str = "manual", session: str | None = None, now_dt: datetime | None = None) -> dict:
    """Release one lease, by session. Without a session: the most
    recently used record — the historical single-lease behaviour
    in a world that usually holds one lease the operator means."""
    rec = load_record(repo, session)
    if not rec:
        return {"action": "no-lease"}
    # Release guard (lease-extension plan): re-read the record
    # immediately before stopping. An extension that landed after the
    # caller's read moves the on-disk deadline into the future, and
    # the extension wins — abort rather than stop a lease whose end
    # just moved.
    fresh = load_record(repo, rec["session"])
    if fresh is not None and fresh.get("deadline") and rec.get("deadline"):
        try:
            moved = parse_dt(fresh["deadline"]) > parse_dt(rec["deadline"])
        except ValueError:
            moved = False
        if moved:
            return {"action": "release-aborted", "reason": reason, "guard": "deadline-moved", "deadline": fresh["deadline"], "record": fresh}
    outcome = stop_session_safe(colab, rec["session"])
    if outcome == "stop-failed":
        # Keep the record: the session may still be billing, and the
        # reaper must keep seeing (and retrying) this lease rather than
        # forgetting a live assignment.
        return {"action": "release-failed", "reason": reason, "stop": outcome, "record_kept": True}
    append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
    delete_record(repo, rec["session"])
    _note_envelope_lease_released(repo, fresh if fresh and fresh.get("envelope_id") else rec, reason)
    return {"action": "released", "reason": reason, "stop": outcome, "was": {k: rec.get(k) for k in ("session", "shape", "caller", "acquired_at", "deadline")}}


def _session_shape_args(shape: dict) -> list[str]:
    return (["--high-mem"] if shape.get("high_mem") else []) + (["--gpu", shape["gpu"]] if shape.get("gpu") else []) + (["--tpu", shape["tpu"]] if shape.get("tpu") else [])


def _swap_session(colab, repo: Path, rec: dict, new_session: str, acquired_at: str, now_dt: datetime) -> None:
    """The record half of a renewal, shared by both drivers: only the
    session and the session's acquired_at change — deadline, jobs,
    and shape are the window's and stay put. The keepalive is
    relaunched on the new session and the kernel pulsed, as at
    acquire."""
    append_spend_segment(repo, rec, now_dt)  # the old session's segment closes here
    old_session = rec["session"]
    rec["session"] = new_session
    rec["acquired_at"] = acquired_at
    rec["last_used_at"] = now_dt.isoformat()
    save_record(repo, rec)
    if old_session != new_session:
        # The record's file is keyed by session: the old session's
        # file still holds the pre-swap record under the old name
        # and must not survive as a second, stale lease.
        delete_record(repo, old_session)
    if _capture_session_host(repo, rec, "renewal", spool_on_first=True):
        save_record(repo, rec)
    ensure_keepalive(colab, repo, new_session)
    rec["last_pulse_at"] = now_dt.isoformat()
    rec["last_pulse_result"] = kernel_pulse(colab, repo, new_session)
    save_record(repo, rec)


def _adopt_handover(colab, repo: Path, cfg: dict, rec: dict, now_dt: datetime) -> dict | None:
    """Renewal driver 1 (nested): pull the handover file the in-VM
    renewal kit wrote on the parent session, merge the child's
    binding into the local sessions file programmatically (contents
    never printed), swap the record onto the child, and stop the
    parent once the child answers. Returns the renewal detail, or
    None when there is no usable handover (yet)."""
    dest = var_dir(repo) / "lease-handover.json"
    try:
        r = colab("download", "-s", rec["session"], "/content/lease-handover.json", str(dest), timeout=HANDOVER_READ_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 or not dest.exists():
        return None
    data = load_json(dest, None)
    if not isinstance(data, dict):
        return None
    child = data.get("child_session")
    binding = data.get("binding")
    if not child or not isinstance(binding, dict) or child not in binding:
        return None
    sessions_file = local_sessions_path()
    # The merge must never rebuild the binding map from a failed
    # read: the CLI writes this file non-atomically, so a read can
    # catch a partial write, and starting from {} would clobber
    # every other session's binding on write-back — blinding the
    # machinery to live sessions (the 2026-11-05 orphan incident).
    # Absent file: start empty. Present but unparseable: abort.
    try:
        raw = sessions_file.read_text()
    except FileNotFoundError:
        local = {}
    except OSError:
        return None
    else:
        try:
            local = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(local, dict):
            return None
    local[child] = binding[child]
    try:
        sessions_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = sessions_file.with_name(sessions_file.name + ".tmp")
        tmp.write_text(json.dumps(local, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, sessions_file)
    except OSError:
        return None
    if session_state(colab, child) != "alive":
        return None  # binding merged; the child does not answer yet — retry next cycle
    old_session = rec["session"]
    rec["kit"] = "installed"  # the kit installs itself in the child
    _swap_session(colab, repo, rec, child, data.get("minted_at") or now_dt.isoformat(), now_dt)
    stop_session_safe(colab, old_session)
    return {"driver": 1, "old_session": old_session, "new_session": child}


def _renew_fresh_session(colab, repo: Path, cfg: dict, rec: dict, now_dt: datetime) -> dict | None:
    """Renewal driver 2 (reaper fallback): create a fresh session for
    the lease's shape against the same window end and swap the record
    onto it. No budget check — the window was priced at acquire/extend
    time and renewal never re-prices it. The fresh session gets the
    renewal kit, so the next renewal can be driver 1. The old session
    is stopped once the record has swapped."""
    # The lease's shape descriptor: the catalog entry when it knows
    # the shape, else the descriptor the record itself carries — a
    # renewal must mint the shape the lease was created at even with
    # a cold catalog cache.
    shape_id = rec.get("shape")
    shape = catalog_shapes(repo).get(shape_id)
    if shape is None:
        shape = {"id": shape_id, "rate_cu_per_hour": rec.get("rate_cu_per_hour"), **_shape_spec_for(repo, shape_id, rec)}
    # Session shape gate: a renewal re-creates the session, so the
    # record's shape is re-checked (renewal_admission — no actor
    # bypass). A refusal recreates nothing: returning None keeps
    # the record for the next cycle, whose reap surfaces
    # renewal-failed until the window's end releases it — the shape
    # is surfaced, never silently perpetuated.
    gate = renewal_admission(repo, shape_id, shape)
    if not gate["allowed"]:
        append_gate_journal(repo, {"kind": "shape-refused", "seam": "renewal", "shape": shape_id, "shape_class": gate["shape_class"], "agent_classes": gate["agent_classes"]})
        return None
    # The renewal child derives from the lease's own name: the
    # generated name with any earlier renewal suffixes stripped, so
    # a lease's sessions read as one family for its whole window.
    base = rec["session"]
    while re.search(r"-r\d+$", base):
        base = re.sub(r"-r\d+$", "", base)
    new_session = f"{base}-r{int(now_dt.timestamp())}"
    try:
        r = colab(*(["new", "-s", new_session] + _session_shape_args(shape)), timeout=600)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
        return None
    old_session = rec["session"]
    _swap_session(colab, repo, rec, new_session, now_dt.isoformat(), now_dt)
    kit = install_renewal_kit(colab, repo, rec, now_dt)
    if kit.get("kit") == "installed":
        rec["kit"] = "installed"
        save_record(repo, rec)
    if old_session != new_session:
        stop_session_safe(colab, old_session)
    return {"driver": 2, "old_session": old_session, "new_session": new_session}


def reaper_events_path(repo: Path) -> Path:
    return var_dir(repo) / "reaper-events.jsonl"


def append_reaper_event(repo: Path, event: dict) -> bool:
    """Spool a core-side lifecycle event for the companion server to
    drain into the UI outbox. The outbox has exactly one writer (the
    server, which owns seq allocation), so reap() never writes it
    directly. Best-effort: a failed spool write never breaks reap —
    the reap result itself still carries the outcome."""
    try:
        p = reaper_events_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        return True
    except OSError:
        return False


def grant_ledger_path(repo: Path) -> Path:
    return var_dir(repo) / "grant-ledger.jsonl"


def append_grant_ledger(repo: Path, event: dict) -> bool:
    """Append one envelope lifecycle event to var/grant-ledger.jsonl
    (two-tier envelope plan): grant started, lease attributed,
    renewed, released, grant ended. Best-effort like the reaper
    spool — a failed ledger write never breaks the lifecycle action
    it records."""
    try:
        p = grant_ledger_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
        return True
    except OSError:
        return False


def load_grant_ledger(repo: Path) -> list[dict]:
    out = []
    try:
        lines = grant_ledger_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _capture_session_host(repo: Path, rec: dict, origin: str, spool_on_first: bool) -> bool:
    """Record the session's current tunnel host on the lease record
    (initiation-time approval plan, .plans/2026-11-03-initiation-time-
    approval.md). A host is born at exactly three moments — a fresh
    acquire, a renewal swap, an adoption — and network approvals are
    granted per exact host per caller scope, so each birth must
    surface at initiation instead of being discovered by a background
    caller's retries. When the host differs from the one on the
    record, spool an approval-needed event through the reaper-events
    spool (the companion drains it; the notify hook announces it).

    A record with no host yet baselines silently unless
    spool_on_first: a fresh acquire's host is a birth, while an
    existing lease met by the adopted path was approved in its own
    time and must not mint a false batch. Returns True when the host
    changed — the caller saves the record."""
    host = session_host(rec.get("session") or "")
    if not host:
        return False
    previous = rec.get("host")
    if previous == host:
        return False
    rec["host"] = host
    if previous is not None or spool_on_first:
        event = {"kind": "approval-needed", "shape": rec.get("shape"), "session": rec.get("session"), "host": host, "previous_host": previous, "origin": origin}
        if rec.get("caller"):
            # The approval announcement names whose work the host
            # serves (caller-attribution plan) — present only when
            # the record carries a caller, so unattributed births
            # announce as such by absence.
            event["caller"] = rec["caller"]
        append_reaper_event(repo, event)
    return True


def _note_envelope_lease_started(repo: Path, rec: dict) -> None:
    """Record an envelope attribution in the ledger, and spool the
    envelope-lease-started event the companion drains into the UI
    outbox (the drain is kind-generic; the panel renders this kind).
    The record's caller rides both events when it carries one."""
    ledger = {"kind": "lease-attributed", "envelope_id": rec.get("envelope_id"), "shape": rec.get("shape"), "via": rec.get("via"), "session": rec.get("session"), "deadline": rec.get("deadline")}
    spool = {"kind": "envelope-lease-started", "shape": rec.get("shape"), "envelope_id": rec.get("envelope_id"), "via": rec.get("via")}
    if rec.get("caller"):
        ledger["caller"] = rec["caller"]
        spool["caller"] = rec["caller"]
    append_grant_ledger(repo, ledger)
    append_reaper_event(repo, spool)


def _note_envelope_lease_released(repo: Path, rec: dict, reason: str) -> None:
    if not rec.get("envelope_id"):
        return
    ledger = {"kind": "lease-released", "envelope_id": rec.get("envelope_id"), "shape": rec.get("shape"), "via": rec.get("via"), "reason": reason}
    spool = {"kind": "envelope-lease-released", "shape": rec.get("shape"), "envelope_id": rec.get("envelope_id"), "reason": reason}
    if rec.get("caller"):
        ledger["caller"] = rec["caller"]
        spool["caller"] = rec["caller"]
    append_grant_ledger(repo, ledger)
    append_reaper_event(repo, spool)


def _attempt_renewal(colab, repo: Path, cfg: dict, rec: dict, state: str, now_dt: datetime) -> dict | None:
    """One renewal attempt for a window lease. Driver 1 (handover
    adoption) is preferred when the kit is installed; driver 2 is the
    fallback — immediately when the session is gone or no kit is
    installed, and at the session cap when a kit is installed but no
    handover has landed. While the parent is alive and below its cap,
    a missing handover means the in-VM kit may still be minting: wait
    for the next cycle rather than race it into a second child.
    Never raises; None means retry next cycle."""
    try:
        result = None
        if state == "alive" and rec.get("kit") == "installed":
            result = _adopt_handover(colab, repo, cfg, rec, now_dt)
            if result is None and now_dt - parse_dt(rec["acquired_at"]) < timedelta(hours=SESSION_CAP_HOURS):
                return None
        if result is None:
            result = _renew_fresh_session(colab, repo, cfg, rec, now_dt)
        if result is not None:
            append_reaper_event(repo, {"kind": "lease-renewed", "shape": rec.get("shape"), **result})
            if rec.get("envelope_id"):
                append_grant_ledger(repo, {"kind": "lease-renewed", "envelope_id": rec.get("envelope_id"), "shape": rec.get("shape"), **result})
        return result
    except Exception:  # noqa: BLE001
        return None


def _reap_record(colab, repo: Path, cfg: dict, rec: dict, grant: dict | None, now_dt: datetime) -> dict:
    """One record's reap: the pre-two-tier reap logic (hard deadline,
    idle shutdown, death-clearing, keepalive relaunch + kernel pulse)
    plus grant-expiry enforcement for standing leases — a standing
    lease whose grant has lapsed (or vanished) is released even before
    its own deadline."""
    diag: dict = {}
    state = session_state(colab, rec["session"], diag, attempts=_REAP_READ_ATTEMPTS, timeout=_REAP_READ_TIMEOUT_S)
    if state == "unknown":
        return {"action": "kept-unknown", "session": rec["session"], "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent":
        if rec.get("renew") and not rec.get("standing") and now_dt < parse_dt(rec["deadline"]):
            # Window lease whose session died mid-window: renew it
            # instead of clearing (lease-extension plan). A failed
            # renewal keeps the record — the next cycle retries, and
            # the window's end still releases normally below once it
            # passes. Standing records never take this branch.
            renewed = _attempt_renewal(colab, repo, cfg, rec, state, now_dt)
            if renewed is not None:
                return {"action": "renewed", "session": rec["session"], **renewed}
            return {"action": "renewal-failed", "session": rec["session"]}
        append_spend_segment(repo, rec, now_dt)
        _note_envelope_lease_released(repo, rec, "cleared-dead")
        delete_record(repo, rec["session"])
        return {"action": "cleared-dead", "session": rec["session"]}
    if rec.get("standing") and not grant_active(grant, now_dt):
        rel = release(colab, repo, reason="grant-expired", session=rec["session"], now_dt=now_dt)
        return {"action": "released-standing-expired", "stop": rel.get("stop"), "was": rel.get("was")}
    if now_dt >= parse_dt(rec["deadline"]):
        rel = release(colab, repo, reason="deadline", session=rec["session"], now_dt=now_dt)
        return {"action": "released-deadline", "stop": rel.get("stop"), "was": rel.get("was")}
    idle = cfg.get("idle_shutdown_minutes")
    if idle is not None and not rec.get("jobs"):
        if now_dt - parse_dt(rec["last_used_at"]) > timedelta(minutes=float(idle)):
            rel = release(colab, repo, reason="idle", session=rec["session"], now_dt=now_dt)
            return {"action": "released-idle", "stop": rel.get("stop"), "was": rel.get("was")}
    renewal_failed = False
    if rec.get("renew") and not rec.get("standing"):
        # Renewal threshold: the session's age passes the platform cap
        # minus the overlap margin while the window is still open.
        if now_dt - parse_dt(rec["acquired_at"]) >= timedelta(hours=SESSION_CAP_HOURS) - RENEWAL_MARGIN:
            renewed = _attempt_renewal(colab, repo, cfg, rec, state, now_dt)
            if renewed is not None:
                return {"action": "renewed", "session": rec["session"], **renewed}
            renewal_failed = True  # the session still lives; the kept path below maintains it
    out = {"action": "kept", "session": rec["session"], "deadline": rec["deadline"], "jobs": rec.get("jobs", [])}
    if renewal_failed:
        out["renewal"] = "failed"
    if cfg.get("keepalive", {}).get("enabled"):
        fresh, age = keepalive_fresh(colab, repo, rec["session"])
        if fresh:
            out["keepalive"] = "ok"
            out["heartbeat_age_s"] = round(age, 1)
        else:
            out["keepalive"] = ensure_keepalive(colab, repo, rec["session"]).get("keepalive")
        pulse_every = float(cfg["keepalive"].get("pulse_seconds", 600))
        last_pulse = rec.get("last_pulse_at")
        due = last_pulse is None or (now_dt - parse_dt(last_pulse)).total_seconds() >= pulse_every
        if due:
            result = kernel_pulse(colab, repo, rec["session"])
            rec["last_pulse_at"] = now_dt.isoformat()
            rec["last_pulse_result"] = result
            save_record(repo, rec)
            out["pulse"] = result
        else:
            out["pulse"] = "not-due"
    return out


_REAP_PRIORITY = ("released-deadline", "cleared-dead", "released-standing-expired", "released-idle", "renewed", "renewal-failed", "kept-unknown", "kept")


@contextlib.contextmanager
def _reap_lock(repo: Path):
    """One reap pass at a time. The hook fires reap on a fixed
    poll and kills its wait at a timeout, but the reap process
    itself used to run on — the next poll then started a second
    pass over the same records, and two passes renewing the same
    lease in parallel minted duplicate successor leases
    (2026-11-08). The lock is an flock on a var/ file: held only
    while a pass runs, released by the OS when the holder dies
    (a kill included), so it can never go stale."""
    path = var_dir(repo) / "reap.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def reap(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    with _reap_lock(repo) as held:
        if not held:
            return {"action": "already-running", "records": []}
        return _reap_pass(colab, repo, now_dt)


def _reap_pass(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty: reap every lease record. The top-level
    action/fields are the most consequential per-record result —
    or the sole record's, in a one-lease world — so the hook's
    parsing (kept/none/released-deadline/cleared-dead) keeps its
    meaning; every record's outcome is also listed under
    "records"."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    recs = all_records(repo)
    # Unattributed detection runs even with no records at all —
    # that is precisely the world where a stray matters.
    unattributed = detect_unattributed(colab, repo, now_dt)
    if not recs:
        return {"action": "none", "records": [], "unattributed": unattributed}
    grant = load_grant(repo)
    for rec in recs:
        # Host reconcile (initiation-time approval follow-up,
        # 2026-11-04): a birth capture is not the last word — the
        # record's first saves and the capture save are seconds
        # apart (keepalive + pulse run between them), and a
        # concurrent writer holding a pre-capture copy (a keepwarm
        # pulse, a reap save) restores a hostless record; an
        # adopted lease ran exactly that race and the reaper's
        # approval gate stayed blind to its host for hours while
        # the acquire output reported the capture fine.
        # Re-derive the host from the local binding every pass: a
        # fill is silent (the birth event already spooled), a real
        # change spools as origin "reconcile".
        if _capture_session_host(repo, rec, "reconcile", spool_on_first=False):
            save_record(repo, rec)
    results = [{"session": rec.get("session"), **_reap_record(colab, repo, cfg, rec, grant, now_dt)} for rec in recs]
    top = results[0] if len(results) == 1 else None
    if top is None:
        for action in _REAP_PRIORITY:
            top = next((r for r in results if r["action"] == action), None)
            if top is not None:
                break
    out = dict(top)
    out["records"] = results
    out["unattributed"] = unattributed
    return out


def status(colab, repo: Path, session: str | None = None) -> dict:
    rec = load_record(repo, session)
    if not rec:
        return {"action": "no-lease"}
    out = {"action": "active" if session_alive(colab, rec["session"]) else "recorded-but-dead", "record": rec}
    if out["action"] == "active" and load_config(repo).get("keepalive", {}).get("enabled"):
        age = heartbeat_age(colab, repo, rec["session"])
        out["keepalive_heartbeat_age_s"] = round(age, 1) if age is not None else None
    return out


def grant_path(repo: Path) -> Path:
    return var_dir(repo) / "grant.json"


def load_grant(repo: Path) -> dict | None:
    grant = load_json(grant_path(repo), None)
    return grant if isinstance(grant, dict) and grant.get("kind") == "two-tier" and grant.get("until") else None


def grant_active(grant: dict | None, now_dt: datetime) -> bool:
    return grant is not None and now_dt < parse_dt(grant["until"])


def grant_scope(cfg: dict, standing: str, burst: str) -> dict:
    """The scope a grant sanctions (two-tier envelope plan): the
    shapes it covers and a snapshot of the budget caps in force at
    grant time, in the shape state serves caps in. The snapshot is
    the authorization the operator's tap granted — later config
    edits move future grants and ordinary leases, never this one."""
    shapes = [standing] if standing == burst else [standing, burst]
    return {"caps": _served_caps(cfg), "standing_shape": standing, "burst_shape": burst, "shapes": shapes}


def grant_shapes(repo: Path, grant: dict) -> list:
    """The shapes inside a grant's scope: the scope's own shapes
    list, exactly as written. A scope that names no shapes
    sanctions none."""
    scope = grant.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("shapes"), list) and scope["shapes"]:
        return list(scope["shapes"])
    return []


def grant_caps(grant: dict) -> dict | None:
    """The grant's snapshot caps, or None for a pre-envelope grant
    (no snapshot was taken, so live config caps still price it)."""
    scope = grant.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("caps"), dict):
        return scope["caps"]
    return None


def active_grant(repo: Path, now_dt: datetime) -> dict | None:
    grant = load_grant(repo)
    return grant if grant_active(grant, now_dt) else None


def shape_in_grant(repo: Path, grant: dict | None, shape_id: str | None) -> bool:
    return grant is not None and shape_id is not None and shape_id in grant_shapes(repo, grant)


def write_grant(repo: Path, hours: float, burst_shape: str | None = None, now_dt: datetime | None = None) -> dict:
    """Record a two-tier grant: for the next `hours`, the standing duty
    keeps the standing tier up alongside the burst tier. The burst is
    the default shape unless --burst names another catalog shape. The
    grant is an authorization envelope: its `granted_at` is its id and
    its `scope` snapshots the shapes and budget caps the grant tap
    sanctioned."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    standing = standing_shape(repo)
    burst = burst_shape or default_shape(repo) or standing
    grant = {
        "kind": "two-tier",
        "granted_at": now_dt.isoformat(),
        "until": (now_dt + timedelta(hours=float(hours))).isoformat(),
        "standing_shape": standing,
        "burst_shape": burst,
        "scope": grant_scope(cfg, standing, burst),
    }
    p = grant_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=2))
    append_grant_ledger(repo, {"kind": "grant-started", "envelope_id": grant["granted_at"], "until": grant["until"], "scope": grant["scope"]})
    return {"action": "granted", "grant": grant}


def clear_grant(repo: Path) -> None:
    grant = load_grant(repo)
    if grant is not None:
        append_grant_ledger(repo, {"kind": "grant-ended", "envelope_id": grant.get("granted_at")})
    grant_path(repo).unlink(missing_ok=True)


def ungrant(colab, repo: Path) -> dict:
    """Remove the grant and release the standing lease it sanctioned."""
    clear_grant(repo)
    rec = standing_record(repo)
    if rec is None:
        return {"action": "ungranted", "standing": {"action": "no-lease"}}
    rel = release(colab, repo, reason="ungrant", session=rec["session"])
    return {"action": "ungranted", "standing": rel}


def standing_record(repo: Path) -> dict | None:
    """The standing duty's lease record: stamped standing:true at
    creation; identified by its caller for a record the duty wrote
    before caller stamps existed. The standing duty holds one
    lease; when flag and caller disagree, the flag wins."""
    recs = all_records(repo)
    for rec in recs:
        if rec.get("standing"):
            return rec
    caller = standing_caller(repo)
    for rec in recs:
        if rec.get("caller") == caller:
            return rec
    return None


def standing_duty(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty for the two-tier grant. While a grant is active, keep
    the standing lease live: acquire it when absent or dead, with a
    session deadline of min(grant end, now + standing
    .max_session_hours); a created standing lease is marked
    "standing": true on its record, plus "warm": "pending" when
    standing.warm_on_renew is set (the warm run itself is a later
    step). When the grant has expired, release the standing lease,
    clear the grant, and report standing-expired. With no grant on
    file the duty is inert — an orphaned standing record is the
    reaper's to enforce."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    standing_cfg = cfg.get("standing", {})
    grant = load_grant(repo)
    if grant is None:
        return {"action": "no-grant"}
    rec = standing_record(repo)
    if not grant_active(grant, now_dt):
        out: dict = {"action": "standing-expired", "standing": {"action": "no-lease"}}
        if rec is not None and rec.get("standing"):
            out["standing"] = release(colab, repo, reason="grant-expired", session=rec["session"])
        elif rec is not None:
            out["standing"] = {"action": "not-standing", "session": rec["session"]}
        clear_grant(repo)
        return out
    shape_id = standing_shape(repo)
    if rec is None:
        # A live lease at the standing shape held by another caller
        # already satisfies "up"; it is not adopted into standing
        # semantics (the pre-retirement single-slot behaviour, kept
        # for the standing shape only).
        others = [r for r in all_records(repo) if r.get("shape") == shape_id]
        other = max(others, key=lambda r: r.get("deadline") or "", default=None)
        if other is not None and now_dt < parse_dt(other["deadline"]) and session_alive(colab, other["session"]):
            return {"action": "satisfied-live", "session": other["session"], "standing": False}
    if rec is not None and rec.get("standing"):
        if now_dt < parse_dt(rec["deadline"]) and session_alive(colab, rec["session"]):
            return {"action": "standing-kept", "session": rec["session"], "deadline": rec["deadline"], "grant_until": grant["until"]}
    max_session_hours = float(standing_cfg.get("max_session_hours", 12))
    remaining_hours = (parse_dt(grant["until"]) - now_dt).total_seconds() / 3600.0
    result = acquire(colab, repo, shape_id, now_dt=now_dt, hours=min(remaining_hours, max_session_hours), max_hours_cap=max_session_hours, via=GATE_DEFAULT_INFRA_ACTOR, caller=standing_caller(repo), standing=True)
    action = result.get("action")
    if action == "created":
        new_rec = result["record"]
        new_rec["standing"] = True
        if standing_cfg.get("warm_on_renew"):
            new_rec["warm"] = "pending"
        save_record(repo, new_rec)
        return {"action": "standing-acquired", "record": new_rec, "warm": new_rec.get("warm"), "grant_until": grant["until"]}
    if action == "adopted":
        return {"action": "standing-kept", "session": result["record"]["session"], "deadline": result["record"]["deadline"], "grant_until": grant["until"]}
    if action == "refused-budget":
        return {"action": "standing-refused-budget", "budget": result.get("budget")}
    return {"action": "standing-failed", "error": result.get("error", action)}


def specs(repo: Path, colab=None) -> list[dict]:
    """The shape catalog for the UI's picker: every shape with the
    fields the UI renders (identity, label, descriptor, rate). The
    UI serves costs from this list only. The label is the display
    name from the catalog (a shape id is an internal name; the UI
    shows labels); a shape without one falls back to its id."""
    gate = session_gate_policy(repo)
    out = []
    for shape_id, entry in catalog_shapes(repo, colab).items():
        out.append({
            "id": shape_id,
            "label": entry.get("label") or shape_id,
            "gpu": entry.get("gpu"),
            "tpu": entry.get("tpu"),
            "high_mem": bool(entry.get("high_mem", False)),
            "rate_cu_per_hour": entry.get("rate_cu_per_hour"),
            # Session shape gate, served ready: whether an agent
            # actor may start this shape (the panel itself is a
            # bypass actor and can start any of them).
            "agent_startable": not gate["enabled"] or shape_class(entry) in gate["agent_classes"],
        })
    return out


def budget_snapshot(colab, repo: Path) -> dict:
    """Read-only budget numbers for `state`: the live balance and
    spend from the attributed accounting (segment journal + live
    records), never from balance deltas."""
    cfg = load_config(repo)
    try:
        balance = parse_balance(colab("usage", timeout=120).stdout)
    except Exception:  # noqa: BLE001
        balance = None
    day_spent = month_spent = standing_day_spent = None
    if balance is not None:
        spend = attributed_spend(repo)
        day_spent = spend["day_spent"]
        month_spent = spend["month_spent"]
        # The standing caller's day spend from the same derivation
        # the guard enforces against — the number served is the
        # number enforced.
        standing_day_spent = spend["callers"].get(standing_caller(repo), {}).get("day_spent", 0.0)
    return {
        "balance": balance,
        "day_spent": day_spent,
        "month_spent": month_spent,
        "standing_day_spent": standing_day_spent,
        "caps": _served_caps(cfg),
    }


def _served_caps(cfg: dict) -> dict:
    """The caps in the shape state serves: config's daily/monthly
    budgets plus the standing caller's daily sub-cap."""
    standing = cfg.get("standing")
    return {
        "daily": cfg.get("daily_cu_budget"),
        "monthly": cfg.get("monthly_cu_budget"),
        "standing_daily": standing.get("daily_cu_cap") if isinstance(standing, dict) else None,
    }


_UNSET = object()


def set_budget_caps(repo: Path, daily=_UNSET, monthly=_UNSET, standing_daily=_UNSET) -> dict:
    """Update the budget caps in config/lease.json — the operator's
    throttle, set from the panel (2026-11-01: the caps are the operator's to
    set, not a fixed system wall; he holds 700+ CU banked with no
    competing use for it). Each cap is a non-negative number or None
    (no cap — the budget guard's own semantics); an omitted argument
    leaves its cap untouched. Every other config key is preserved,
    and the write is atomic (tmp sibling + rename) in the file's own
    compact sorted style, so no reader sees a half-written config."""
    path = repo / "config" / "lease.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"action": "caps-invalid", "error": "config/lease.json is unreadable"}
    if not isinstance(cfg, dict):
        return {"action": "caps-invalid", "error": "config/lease.json is not a JSON object"}
    for name, value in (("daily", daily), ("monthly", monthly), ("standing_daily", standing_daily)):
        if value is _UNSET or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return {"action": "caps-invalid", "error": f"{name} cap must be a non-negative number or null"}
    standing = cfg.get("standing")
    if standing_daily is not _UNSET and standing is not None and not isinstance(standing, dict):
        return {"action": "caps-invalid", "error": "config standing section is not an object"}
    previous = _served_caps(cfg)

    def clean(v):
        return int(v) if isinstance(v, float) and v.is_integer() else v

    if daily is not _UNSET:
        cfg["daily_cu_budget"] = None if daily is None else clean(daily)
    if monthly is not _UNSET:
        cfg["monthly_cu_budget"] = None if monthly is None else clean(monthly)
    if standing_daily is not _UNSET:
        if not isinstance(standing, dict):
            standing = cfg["standing"] = {}
        standing["daily_cu_cap"] = None if standing_daily is None else clean(standing_daily)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, path)
    return {"action": "caps-updated", "caps": _served_caps(cfg), "previous": previous}


def auth_required(repo: Path) -> dict | None:
    """The wrapper guard's stop-and-surface marker
    (var/auth-required.json): present while the stored Colab
    credential is dead and a human re-authorization is the only way
    forward. Written and cleared by bin/colab-run's guard."""
    data = load_json(var_dir(repo) / "auth-required.json", None)
    return data if isinstance(data, dict) else None


def _envelope_block(colab, repo: Path, grant: dict | None, budget: dict) -> dict | None:
    """The envelope view state serves with an active grant (two-tier
    envelope plan): the grant, its scope, the live records attributed
    to it (envelope_id = the grant's granted_at), and spend versus
    the snapshot caps.

    Spend: per-lease figures come from the attributed accounting
    (segment journal + live records) — the lease's own burn in the
    period, shared between envelope and non-envelope work on that
    lease rather than metered per envelope. The totals are the
    budget snapshot's own numbers, the same ones the guard enforces
    against."""
    if grant is None:
        return None
    scope = grant.get("scope")
    if not isinstance(scope, dict):
        # Pre-envelope grant: synthesize the view from the grant's
        # own fields; its caps are the live ones (no snapshot was
        # taken), which grant_caps reports as None.
        scope = {"caps": _served_caps(load_config(repo)), "standing_shape": standing_shape(repo), "shapes": grant_shapes(repo, grant)}
    caps = grant_caps(grant) or scope["caps"]
    attributed = [rec for rec in all_records(repo) if rec.get("envelope_id") == grant.get("granted_at")]
    balance = budget.get("balance")
    spend = attributed_spend(repo) if balance is not None else None
    session_spend = []
    for rec in attributed:
        rate = rec.get("rate_cu_per_hour")
        if rate is None and rec.get("shape"):
            rate = shape_rate(repo, rec["shape"])
        if spend is None or rate is None:
            day = month = None
        else:
            entry = spend["sessions"].get(rec["session"], {})
            day = entry.get("day_spent", 0.0)
            month = entry.get("month_spent", 0.0)
        session_spend.append({"session": rec["session"], "day_spent": day, "month_spent": month})
    return {
        "id": grant.get("granted_at"),
        "grant": grant,
        "scope": scope,
        "caps": caps,
        "records": attributed,
        "spend": {"balance": balance, "day_spent": budget.get("day_spent"), "month_spent": budget.get("month_spent"), "standing_day_spent": budget.get("standing_day_spent")},
        "session_spend": session_spend,
    }


# --- The standing bridge -------------------------------------------------------
#
# Local why: bridge children hold no lease record, so their stop
# segments fold into the attributed spend journal here — without
# the fold-in their burn would be invisible to the guard.
# Mechanics: docs/lease-workflow.md.


def bridge_limits(cfg: dict) -> dict:
    raw = cfg.get("bridge", {}) if isinstance(cfg.get("bridge"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_children": int(raw.get("max_children", 3)),
        "child_idle_minutes": float(raw.get("child_idle_minutes", 30)),
        "child_max_hours": float(raw.get("child_max_hours", 4)),
        "command_timeout_seconds": float(raw.get("command_timeout_seconds", 300)),
    }


def bridge_command(verb: str, cid: str | None = None, **fields) -> dict:
    """Build a signed bridge command (protocol v1): the sha256 covers
    the canonical JSON of every other field, so a partially planted
    file never validates in-VM. Shared by the client and the tests;
    the agent re-derives the same hash."""
    import hashlib
    import uuid

    cmd = {"v": BRIDGE_PROTOCOL, "id": cid or uuid.uuid4().hex, "verb": verb, **fields}
    canon = json.dumps(cmd, sort_keys=True, separators=(",", ":"))
    cmd["sha256"] = hashlib.sha256(canon.encode()).hexdigest()
    return cmd


def _bridge_arm_path(repo: Path) -> Path:
    return var_dir(repo) / "bridge-arm.json"


def _bridge_arm(repo: Path, rec: dict) -> dict:
    """The arm record ties a bridge generation to one standing session
    lifetime: a recycled or renewed standing lease (new session or new
    acquired_at) starts a new generation; an agent death inside the
    same session re-installs under the same generation, so the
    agent resumes its log segments and registry."""
    import uuid

    arm = load_json(_bridge_arm_path(repo), None)
    if not isinstance(arm, dict) or arm.get("session") != rec.get("session") or arm.get("acquired_at") != rec.get("acquired_at"):
        arm = {"session": rec.get("session"), "acquired_at": rec.get("acquired_at"), "generation": uuid.uuid4().hex, "last_install_ts": 0}
        _bridge_arm_path(repo).parent.mkdir(parents=True, exist_ok=True)
        _bridge_arm_path(repo).write_text(json.dumps(arm, indent=1))
    return arm


def _bridge_arm_stamp(repo: Path, arm: dict, now_dt: datetime) -> None:
    arm["last_install_ts"] = now_dt.timestamp()
    _bridge_arm_path(repo).write_text(json.dumps(arm, indent=1))


def bridge_status_read(colab, repo: Path, session: str) -> dict | None:
    """The agent's status.json via one bounded download; None when
    unreadable (never installed, stalled channel, recycled VM) —
    unknown, never an error."""
    dest = var_dir(repo) / "bridge-status-dl.json"
    try:
        r = colab("download", "-s", session, f"{BRIDGE_REMOTE}/status.json", str(dest), timeout=BRIDGE_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return None
        data = json.loads(dest.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _ensure_remote_dirs(colab, repo: Path, session: str, dirs: list[str], tag: str) -> bool:
    """Create remote directories before uploading into them: the
    file channel 500s on an upload whose parent directory does not
    exist (bridge probe, 2026-11-01 — the same defect that failed
    the renewal kit's first installs at the upload stage). One
    bounded exec running os.makedirs in the session; False on any
    failure, and the caller reports its own stage."""
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    runner = var / f"mkdir-{tag}.py"
    runner.write_text("import os\n" + "".join(f"os.makedirs({d!r}, exist_ok=True)\n" for d in dirs) + "print('dirs ready')\n")
    try:
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(runner), timeout=KIT_LAUNCH_TIMEOUT_S)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def install_bridge_kit(colab, repo: Path, rec: dict, generation: str) -> dict:
    """Install the bridge beachhead into the standing lease's session
    (the renewal kit's pattern): the agent snippet plus its guard,
    the bridge config (session, generation, limits, the full shape
    catalog for mints), and the account token, staged under
    /content/bridge-kit; the snippet's --setup places the token,
    builds the CLI venv, and spawns the agent. Bounded and
    non-raising: a failure is a result, and the duty retries after
    its cooldown."""
    session = rec["session"]
    token_src = Path.home() / ".config" / "colab-cli" / "token.json"
    if not token_src.exists():
        return {"bridge_install": "install-failed", "stage": "token-missing"}
    cfg = load_config(repo)
    shapes = {}
    for sid, entry in catalog_shapes(repo, colab).items():
        shapes[sid] = {"high_mem": bool(entry.get("high_mem")), "gpu": entry.get("gpu"), "tpu": entry.get("tpu"), "rate_cu_per_hour": entry.get("rate_cu_per_hour")}
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    # Session shape gate: the bridge agent mints children in-VM,
    # beyond the core's reach, so the install stamps the shapes
    # an agent mint may create under the current policy; the agent
    # refuses any other shape at mint time.
    gate = session_gate_policy(repo)
    agent_allowed = sorted(sid for sid, entry in shapes.items() if not gate["enabled"] or shape_class(entry) in gate["agent_classes"])
    cfg_local = var / "bridge-config.json"
    # Caller attribution: the bridge mints children in-VM under
    # this install's authority, so the config stamps the standing
    # record's caller (the machinery caller when the record
    # predates attribution); child reports inherit it.
    cfg_local.write_text(json.dumps({"session": session, "generation": generation, "limits": bridge_limits(cfg), "shapes": shapes, "agent_allowed_shapes": agent_allowed, "caller": rec.get("caller") or "lease"}, indent=2))
    if not _ensure_remote_dirs(colab, repo, session, [BRIDGE_KIT_REMOTE, f"{BRIDGE_REMOTE}/queue", f"{BRIDGE_REMOTE}/log", f"{BRIDGE_REMOTE}/results"], "bridge"):
        return {"bridge_install": "install-failed", "stage": "mkdir"}
    uploads = [
        (str(repo / "snippets" / "bridge_agent.py"), f"{BRIDGE_KIT_REMOTE}/bridge_agent.py"),
        (str(repo / "snippets" / "_remote_guard.py"), f"{BRIDGE_KIT_REMOTE}/_remote_guard.py"),
        (str(token_src), f"{BRIDGE_KIT_REMOTE}/token.json"),
        (str(cfg_local), f"{BRIDGE_KIT_REMOTE}/bridge.json"),
    ]
    for src, dest in uploads:
        try:
            r = colab("upload", "-s", session, src, dest, timeout=KIT_UPLOAD_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return {"bridge_install": "install-failed", "stage": "upload"}
        if r.returncode != 0:
            return {"bridge_install": "install-failed", "stage": "upload"}
    launcher = var / "launch-bridge-kit.py"
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        "env['COLAB_RELEASE_TAG'] = 'bridge-kit'\n"
        f"subprocess.Popen(['nohup', 'python3', '{BRIDGE_KIT_REMOTE}/bridge_agent.py', '--setup', '--config', '{BRIDGE_KIT_REMOTE}/bridge.json'], stdout=open('{BRIDGE_KIT_REMOTE}/setup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('bridge kit setup launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KIT_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"bridge_install": "install-failed", "stage": "launch"}
    if r.returncode != 0:
        return {"bridge_install": "install-failed", "stage": "launch"}
    return {"bridge_install": "installed"}


def _bridge_fold_stop(repo: Path, line: dict, folded: set) -> bool:
    """Fold one completed bridge-child segment into the attributed
    spend journal, once per child stop line (the ferry's folded set
    is the guard). The stop line carries its shape + minted_at,
    which is everything the journal's segment writer needs; a line
    naming no shape folds nothing."""
    if line.get("kind") != "stop" or not line.get("minted_at") or not line.get("name"):
        return False
    shape_id = line.get("shape")
    if not shape_id:
        return False
    key = f"{line.get('gen')}:{line.get('seq')}"
    if key in folded:
        return False
    folded.add(key)
    rec = {"session": line["name"], "shape": shape_id, "rate_cu_per_hour": shape_rate(repo, shape_id), "acquired_at": line["minted_at"]}
    if line.get("caller"):
        rec["caller"] = line["caller"]
    try:
        end = parse_dt(line["ts"])
    except Exception:  # noqa: BLE001
        return False
    append_spend_segment(repo, rec, end)
    return True


def _bridge_ferry(colab, repo: Path, session: str, status: dict, now_dt: datetime) -> dict:
    """One ferry pass: mirror new log lines (by generation, segment,
    byte offset — only complete lines advance the offset), fold
    child stop lines into the spend journal, drop outbox commands
    whose terminal line reached the mirror, and plant the rest.
    The outbox is the source of truth: a command leaves it only on
    acknowledgement, and a generation change re-plants everything
    unacknowledged. Bounded and non-raising throughout."""
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    outbox = var / "bridge-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    mirror_path = var / "bridge-mirror.jsonl"
    fstate_path = var / "bridge-ferry.json"
    fstate = load_json(fstate_path, None)
    if not isinstance(fstate, dict):
        fstate = {}
    gen = status.get("gen")
    if fstate.get("gen") != gen:
        fstate = {"gen": gen, "segment": "bridge-000001.jsonl", "offset": 0, "planted": {}, "folded": []}
        with mirror_path.open("a") as fh:
            fh.write(json.dumps({"kind": "generation", "gen": gen, "ts": now_dt.isoformat()}) + "\n")
    folded = set(fstate.get("folded", []))
    planted = dict(fstate.get("planted", {}))

    def seg_no(name: str) -> int:
        try:
            return int(str(name)[7:13])
        except (TypeError, ValueError):
            return 1

    mirrored = 0
    acked: set[str] = set()
    cur = seg_no(fstate.get("segment"))
    last = seg_no(status.get("log_segment") or fstate.get("segment"))
    dest = var / "bridge-seg-dl.jsonl"
    while cur <= last:
        name = f"bridge-{cur:06d}.jsonl"
        try:
            r = colab("download", "-s", session, f"{BRIDGE_REMOTE}/log/{name}", str(dest), timeout=30)
        except Exception:  # noqa: BLE001
            break
        if r.returncode != 0 or not dest.exists():
            break
        data = dest.read_bytes()
        start = int(fstate.get("offset", 0)) if name == fstate.get("segment") else 0
        chunk = data[start:]
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            complete = chunk[: cut + 1].decode(errors="replace")
            with mirror_path.open("a") as fh:
                fh.write(complete)
            for raw in complete.splitlines():
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mirrored += 1
                if isinstance(line, dict):
                    if line.get("id"):
                        acked.add(line["id"])
                    _bridge_fold_stop(repo, line, folded)
            fstate["offset"] = start + cut + 1
        fstate["segment"] = name
        fstate["folded"] = sorted(folded)[-500:]
        fstate_path.write_text(json.dumps(fstate, indent=1))
        cur += 1

    dropped = 0
    for path in sorted(outbox.glob("*.json")):
        if path.stem in acked:
            path.unlink(missing_ok=True)
            planted.pop(path.stem, None)
            dropped += 1
    planted_now = 0
    for path in sorted(outbox.glob("*.json")):
        if planted.get(path.stem) == gen:
            continue
        try:
            r = colab("upload", "-s", session, str(path), f"{BRIDGE_REMOTE}/queue/{path.name}", timeout=BRIDGE_READ_TIMEOUT_S * 3)
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0:
            planted[path.stem] = gen
            planted_now += 1
    fstate["planted"] = planted
    fstate_path.write_text(json.dumps(fstate, indent=1))
    return {"ferry": "ok", "mirrored": mirrored, "acked": len(acked), "outbox_dropped": dropped, "planted": planted_now}


def bridge_duty(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty (rides the standing pass): while a grant is active,
    keep the bridge armed in the standing lease — install or re-arm when the
    agent's heartbeat is missing or stale (cooldown-bounded), and
    run one ferry pass when it is live. Never raises: the bridge is
    an auxiliary surface and must not take the standing duty down."""
    now_dt = now_dt or datetime.now(timezone.utc)
    try:
        cfg = load_config(repo)
        if not bridge_limits(cfg)["enabled"]:
            return {"bridge": "disabled"}
        grant = active_grant(repo, now_dt)
        if grant is None:
            return {"bridge": "no-grant"}
        rec = standing_record(repo)
        if not rec:
            return {"bridge": "no-standing"}
        session = rec["session"]
        arm = _bridge_arm(repo, rec)
        status = bridge_status_read(colab, repo, session)
        age = None
        if status is not None and status.get("gen") == arm["generation"] and status.get("ts"):
            try:
                age = max(0.0, now_dt.timestamp() - parse_dt(status["ts"]).timestamp())
            except Exception:  # noqa: BLE001
                age = None
        out: dict = {"bridge": "armed" if (age is not None and age <= BRIDGE_HEARTBEAT_STALE_S) else "down", "generation": arm["generation"][:8]}
        if age is not None:
            out["heartbeat_age_s"] = round(age)
        if out["bridge"] == "armed":
            out.update(_bridge_ferry(colab, repo, session, status, now_dt))
            if status.get("auth_down"):
                out["bridge"] = "auth-down"
            return out
        if now_dt.timestamp() - float(arm.get("last_install_ts", 0)) >= BRIDGE_INSTALL_COOLDOWN_S:
            out.update(install_bridge_kit(colab, repo, rec, arm["generation"]))
            _bridge_arm_stamp(repo, arm, now_dt)
        else:
            out["bridge_install"] = "cooldown"
        return out
    except Exception as exc:  # noqa: BLE001
        return {"bridge": "error", "error": type(exc).__name__}


def _bridge_block(colab, repo: Path) -> dict | None:
    """The served bridge state for the panel/artifact: armed, the
    heartbeat's age, live children, and the queue depth — all read
    from the agent's own status file, nothing derived client-side.
    None when no grant is active (the bridge has no home then)."""
    now_dt = datetime.now(timezone.utc)
    grant = active_grant(repo, now_dt)
    if grant is None:
        return None
    cfg = load_config(repo)
    rec = standing_record(repo)
    if not rec:
        return {"armed": False, "reason": "no-standing"}
    status = bridge_status_read(colab, repo, rec["session"])
    if status is None:
        return {"armed": False, "reason": "no-status"}
    age = None
    try:
        age = round(max(0.0, now_dt.timestamp() - parse_dt(status["ts"]).timestamp()))
    except Exception:  # noqa: BLE001
        pass
    return {
        "armed": age is not None and age <= BRIDGE_HEARTBEAT_STALE_S,
        "generation": str(status.get("gen", ""))[:8],
        "heartbeat_age_s": age,
        "auth_down": bool(status.get("auth_down")),
        "children": status.get("children", []),
        "queue": status.get("queue", 0),
        "processed": status.get("processed", 0),
    }


# --- Session access list & unattributed sessions -------------------------------
#
# Local why: unattributed is a detection state, never a claim
# value. Mechanics: docs/lease-workflow.md.


def session_endpoint(session: str) -> str | None:
    """The raw binding endpoint/url for a session (the value
    session_host parses the host out of), or None when the binding
    or the entry is absent."""
    binding = load_json(local_sessions_path(), {})
    if not isinstance(binding, dict):
        return None
    entry = binding.get(session)
    if not isinstance(entry, dict):
        return None
    for key in ("url", "endpoint"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


def _endpoint_shape(endpoint: str | None) -> str | None:
    """The shape class an endpoint's host names (m-hm- high-RAM,
    m-s- standard — the same evidence the post-start assertion
    reads), or None when the endpoint names neither."""
    if not endpoint:
        return None
    host = urlparse(endpoint).hostname or endpoint
    m = re.match(r"m-(hm|s)-", host)
    if not m:
        return None
    return "high_ram" if m.group(1) == "hm" else "standard"


def live_sessions(colab, repo: Path) -> list[dict] | None:
    """The server's live sessions: [{session, endpoint,
    shape_class}], endpoint/shape filled from the listing line when
    it carries them, else from the local binding. None when the
    listing is unreadable — a failed call, an empty answer, an auth
    prompt: the session_state rule, that no caller may act on a
    list it could not read."""
    try:
        out = colab("sessions", timeout=120).stdout
    except Exception:  # noqa: BLE001
        return None
    if not out or not out.strip() or "Enter the authorization code" in out:
        return None
    found: dict[str, dict] = {}
    text = out.strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        for name in parsed:
            if isinstance(name, str) and name:
                found[name] = {"session": name, "endpoint": None, "shape_class": None}
    if not found:
        for line in text.splitlines():
            m = re.match(r"\[([^\]]+)\]\s*(.*)$", line.strip())
            if not m or not m.group(1).strip():
                continue
            if m.group(1).strip() == "colab":
                # The CLI prefixes its own chatter with [colab]
                # (e.g. "[colab] No active sessions.") — that is
                # not a session name, and reading it as one invents
                # a phantom session on an empty account.
                continue
            entry = {"session": m.group(1).strip(), "endpoint": None, "shape_class": None}
            rest = m.group(2)
            for tok in rest.split():
                if tok.startswith("http") or tok.startswith("m-"):
                    entry["endpoint"] = tok
                    break
            if "High-RAM" in rest:
                entry["shape_class"] = "high_ram"
            elif "Standard" in rest:
                entry["shape_class"] = "standard"
            found[entry["session"]] = entry
    if not found:
        # Substring fallback over the names this workspace knows
        # (records, binding, claims) — the way session_state reads
        # a listing whose format carries no structure at all.
        known = {rec.get("session") for rec in all_records(repo)}
        binding = load_json(local_sessions_path(), {})
        if isinstance(binding, dict):
            known |= set(binding.keys())
        known |= set(load_caller_claims(repo).keys())
        for name in sorted(n for n in known if n):
            if name in text:
                found[name] = {"session": name, "endpoint": None, "shape_class": None}
    for entry in found.values():
        if not entry["endpoint"]:
            entry["endpoint"] = session_endpoint(entry["session"])
        if not entry["shape_class"]:
            entry["shape_class"] = _endpoint_shape(entry["endpoint"])
    return list(found.values())


def unattributed_path(repo: Path) -> Path:
    return var_dir(repo) / "unattributed.json"


def load_unattributed(repo: Path) -> dict:
    raw = load_json(unattributed_path(repo), {})
    return raw if isinstance(raw, dict) else {}


def save_unattributed(repo: Path, stamps: dict) -> None:
    p = unattributed_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stamps, indent=2, sort_keys=True))


def clear_unattributed(repo: Path, session: str) -> None:
    stamps = load_unattributed(repo)
    if session in stamps:
        del stamps[session]
        save_unattributed(repo, stamps)


def access_list(colab, repo: Path) -> list[dict]:
    """Every live session with its attribution, served ready for
    the panel: record-backed sessions carry their record's caller
    and acquired_at and their shape's declared shape class;
    claim-only sessions carry the latest claim and its time; the
    rest are unattributed (caller None, first_seen from the
    detection stamp). An unreadable live list serves an empty
    list — no access section rather than a fabricated one."""
    live = live_sessions(colab, repo)
    if live is None:
        return []
    records = {rec.get("session"): rec for rec in all_records(repo)}
    claims = load_caller_claims(repo)
    stamps = load_unattributed(repo)
    shapes = catalog_shapes(repo)
    out = []
    for entry in live:
        name = entry["session"]
        rec = records.get(name)
        if rec is not None:
            shape = shapes.get(rec.get("shape")) or {}
            out.append({"session": name, "endpoint": entry["endpoint"], "shape_class": entry["shape_class"] or shape_class(shape), "caller": rec.get("caller"), "first_seen": rec.get("acquired_at")})
            continue
        claim = claims.get(name)
        if claim is not None:
            out.append({"session": name, "endpoint": entry["endpoint"], "shape_class": entry["shape_class"], "caller": claim.get("caller"), "first_seen": claim.get("ts")})
            continue
        stamp = stamps.get(name) or {}
        out.append({"session": name, "endpoint": entry["endpoint"] or stamp.get("endpoint"), "shape_class": entry["shape_class"] or stamp.get("shape_class"), "caller": None, "first_seen": stamp.get("first_seen"), "unattributed": True})
    return out


def release_session(colab, repo: Path, session: str) -> dict:
    """Stop one session by name (the panel's one-tap release for an
    unattributed session) and clear its detection stamp. The stop
    goes through stop_session_safe — the server-verified stop —
    never a raw stop. A session with a lease record is refused
    here: records release per session through release(), which
    owns the record lifecycle and the spend fold."""
    if any(rec.get("session") == session for rec in all_records(repo)):
        return {"action": "session-has-record", "session": session}
    verdict = stop_session_safe(colab, session)
    if verdict == "stop-failed":
        return {"action": "session-stop-failed", "session": session, "stop": verdict}
    clear_unattributed(repo, session)
    return {"action": "session-released", "session": session, "stop": verdict}


def detect_unattributed(colab, repo: Path, now_dt: datetime) -> dict:
    """Unattributed detection (Step 6), run by reap() each pass: a
    live session with no lease record and no caller claim is a
    stray — stamped with its first-seen time and spooled once as
    unattributed-session (the spool drains into the outbox; the
    kind is not in the UI log's closed vocabulary, so it surfaces
    in the access list, not the log). A stamped session that stops
    being a live stray — it died, or it gained a record or a claim
    — is cleared with an unattributed-cleared event. An unreadable
    live list detects nothing: detection never acts on a list it
    could not read."""
    live = live_sessions(colab, repo)
    if live is None:
        return {"detected": [], "cleared": []}
    known = {rec.get("session") for rec in all_records(repo)} | set(load_caller_claims(repo).keys())
    stamps = load_unattributed(repo)
    live_names = {e["session"] for e in live}
    detected: list[str] = []
    cleared: list[str] = []
    for entry in live:
        name = entry["session"]
        if name in known or name in stamps:
            continue
        stamps[name] = {"first_seen": now_dt.isoformat(), "endpoint": entry["endpoint"], "shape_class": entry["shape_class"]}
        append_reaper_event(repo, {"kind": "unattributed-session", "session": name, "endpoint": entry["endpoint"], "shape_class": entry["shape_class"], "first_seen": stamps[name]["first_seen"]})
        detected.append(name)
    for name in list(stamps):
        if name not in live_names or name in known:
            del stamps[name]
            append_reaper_event(repo, {"kind": "unattributed-cleared", "session": name})
            cleared.append(name)
    if detected or cleared:
        save_unattributed(repo, stamps)
    return {"detected": detected, "cleared": cleared}


# --- Orphaned sessions: evidence and adoption ----------------------------------
#
# Local why: a live session with no lease record is invisible to
# budget attribution and deadline enforcement; its history lives
# only in the journals and kit configs the lifecycle left behind.
# Mechanics: docs/lease-workflow.md.


def _jsonl_events(path: Path) -> list[dict]:
    """Every JSON object in a .jsonl journal, in file order. A
    missing file reads as no events; a torn line is skipped —
    journals are evidence, never a reason to fail a read."""
    events: list[dict] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return events
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _orphan_contribution(source: str, **fields) -> dict:
    contribution = {"source": source, "deadline": None, "acquired_at": None, "shape": None, "spec": None, "caller": None, "at": None}
    contribution.update(fields)
    return contribution


def _dt_or_none(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_dt(value)
    except ValueError:
        return None


def orphan_evidence(repo: Path, session: str) -> dict:
    """Every local source's contribution to one unrecorded
    session's history: {session, sources, kit_installed,
    handover_pending}, each source a {source, deadline,
    acquired_at, shape, spec, caller, at} contribution (None
    where the source carries nothing). Reads are local-only and
    tolerant: a missing or unparseable source contributes
    nothing, never an exception (the load_json pattern).
    Deadline evidence exists in exactly two places — the
    renewal kit's config (a verbatim copy of the record at
    install) and the panel outbox's lease-extended chain —
    and nowhere else: claims, liveness state, and run records
    carry caller/shape/first-seen evidence only."""
    var = var_dir(repo)
    sources: list[dict] = []
    kit = load_json(var / f"renewal-config-{session}.json", None)
    kit_installed = isinstance(kit, dict)
    if kit_installed:
        spec = {"gpu": kit.get("gpu"), "tpu": kit.get("tpu"), "high_mem": bool(kit.get("high_mem"))}
        sources.append(_orphan_contribution("renewal-config", deadline=kit.get("deadline"), acquired_at=kit.get("acquired_at"), shape=kit.get("shape"), spec=spec))
    for event in _jsonl_events(var / "ui-outbox.jsonl"):
        detail = event.get("detail")
        if event.get("kind") != "lease-extended" or not isinstance(detail, dict) or detail.get("session") != session:
            continue
        sources.append(_orphan_contribution("ui-outbox", deadline=detail.get("new_deadline"), at=event.get("ts")))
    for event in _jsonl_events(reaper_events_path(repo)):
        if session not in (event.get("session"), event.get("new_session")):
            continue
        if event.get("caller"):
            sources.append(_orphan_contribution("reaper-events", caller=event.get("caller"), at=event.get("ts")))
    for claim in _jsonl_events(caller_claims_path(repo)):
        if claim.get("session") == session and claim.get("caller"):
            sources.append(_orphan_contribution("caller-claims", caller=claim.get("caller"), at=claim.get("ts")))
            break
    liveness = load_json(var / f"liveness-{session}.json", None)
    if isinstance(liveness, dict):
        sources.append(_orphan_contribution("liveness", shape=liveness.get("shape"), caller=liveness.get("caller"), at=liveness.get("first_seen")))
    runs_dir = repo / "results" / "runs"
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.json")):
            run = load_json(path, None)
            if not isinstance(run, dict) or run.get("session") != session:
                continue
            contribution = _orphan_contribution("run-record", caller=run.get("caller"), at=run.get("started_at"))
            contribution["run_id"] = run.get("run_id") or path.stem
            contribution["state"] = run.get("state")
            sources.append(contribution)
    handover = load_json(var / "lease-handover.json", None)
    handover_pending = isinstance(handover, dict) and handover.get("child_session") == session
    return {"session": session, "sources": sources, "kit_installed": kit_installed, "handover_pending": handover_pending}


def derive_orphan(repo: Path, live_entry: dict) -> dict:
    """One orphan census entry, composed from the session's
    evidence by the priority ruling: the renewal-config's
    deadline/acquired_at/shape first (a verbatim copy of the
    record at kit install), any later lease-extended outbox
    event applied over it, and nothing else carrying a
    deadline. The record's creator is the renewal-origin
    caller in the reaper events, never the earliest claim — a
    claim is a ride, and a rider may predate nothing but their
    own attach. Every conflicting value is served with its
    source; the operator can always override at adoption.
    Shape from evidence first, else the live entry's class
    cross-checked against the catalog: candidates are the
    entries consistent with the class evidence (a high-RAM
    endpoint names high memory, not an accelerator, so its
    candidates span the high-memory CPU and accelerator
    shapes); more than one candidate is ambiguity, never a
    guess."""
    session = live_entry.get("session")
    evidence = orphan_evidence(repo, session)
    by_source: dict[str, list[dict]] = {}
    for contribution in evidence["sources"]:
        by_source.setdefault(contribution["source"], []).append(contribution)

    derived_deadline = None
    deadline_source = None
    deadline_stale_possible = False
    acquired_at = None
    shape = None
    shape_spec = None
    shape_evidenced = False
    kit_cfg = (by_source.get("renewal-config") or [None])[0]
    if kit_cfg is not None:
        acquired_at = kit_cfg.get("acquired_at")
        if kit_cfg.get("deadline"):
            derived_deadline = kit_cfg["deadline"]
            deadline_source = "renewal-config"
        if kit_cfg.get("spec"):
            shape_spec = kit_cfg["spec"]
            shape = shape_id_for(shape_spec.get("gpu"), shape_spec.get("tpu"), shape_spec.get("high_mem"))
            shape_evidenced = True
        elif kit_cfg.get("shape"):
            shape = kit_cfg["shape"]
            shape_evidenced = True
    extensions = by_source.get("ui-outbox") or []
    if extensions:
        last = extensions[-1]
        if _dt_or_none(last.get("deadline")) is not None:
            derived_deadline = last["deadline"]
            deadline_source = "ui-outbox"
        else:
            # An extension the config cannot reflect (extend does
            # not rewrite it once the kit is installed) whose
            # outbox event carries no usable new deadline: the
            # derived value may be stale, and says so.
            deadline_stale_possible = True

    conflicts: list[dict] = []
    deadline_values = []
    if kit_cfg is not None and kit_cfg.get("deadline"):
        deadline_values.append({"source": "renewal-config", "value": kit_cfg["deadline"]})
    for contribution in extensions:
        if contribution.get("deadline"):
            deadline_values.append({"source": "ui-outbox", "value": contribution["deadline"]})
    if len({v["value"] for v in deadline_values}) > 1:
        conflicts.append({"field": "deadline", "values": deadline_values})

    creator_caller = None
    for contribution in by_source.get("reaper-events") or []:
        if contribution.get("caller"):
            creator_caller = contribution["caller"]
            break
    first_claim = None
    claim = (by_source.get("caller-claims") or [None])[0]
    if claim is not None:
        first_claim = claim.get("caller")
    if creator_caller and first_claim and creator_caller != first_claim:
        conflicts.append({"field": "caller", "values": [{"source": "reaper-events", "value": creator_caller}, {"source": "caller-claims", "value": first_claim}]})

    shape_candidates: list[str] = []
    if not shape_evidenced:
        cls = live_entry.get("shape_class")
        entries = catalog_shapes(repo)
        if cls == "high_ram":
            shape_candidates = [sid for sid, entry in entries.items() if entry.get("high_mem")]
        elif cls == "standard":
            shape_candidates = [sid for sid, entry in entries.items() if not entry.get("high_mem")]
        if len(shape_candidates) == 1:
            shape = shape_candidates[0]

    jobs_in_flight = [c["run_id"] for c in by_source.get("run-record") or [] if c.get("state") in ("starting", "running", "polling") and c.get("run_id")]
    seen = [c["at"] for c in evidence["sources"] if c.get("at")]
    stamp = load_unattributed(repo).get(session) or {}
    if stamp.get("first_seen"):
        seen.append(stamp["first_seen"])
    return {
        "session": session,
        "endpoint": live_entry.get("endpoint"),
        "shape_class": live_entry.get("shape_class"),
        "shape": shape,
        "shape_spec": shape_spec,
        "shape_evidenced": shape_evidenced,
        "shape_candidates": shape_candidates,
        "creator_caller": creator_caller,
        "first_claim": first_claim,
        "derived_deadline": derived_deadline,
        "deadline_source": deadline_source,
        "deadline_stale_possible": deadline_stale_possible,
        "conflicts": conflicts,
        "acquired_at": acquired_at,
        "jobs_in_flight": jobs_in_flight,
        "first_seen": min(seen) if seen else None,
        "kit_installed": evidence["kit_installed"],
        "handover_pending": evidence["handover_pending"],
    }


def state_records(colab, repo: Path) -> dict:
    """Portion: every lease record's status. The per-record reads
    are independent and I/O-bound (session probes, heartbeat
    reads), so they run threaded — the portion's wall time is its
    slowest record, not their sum."""
    recs = all_records(repo)

    def entry(rec):
        e = status(colab, repo, session=rec["session"])
        e["session"] = rec["session"]
        e["warm"] = rec.get("warm")
        e["caller"] = rec.get("caller")
        return e

    if len(recs) <= 1:
        records = [entry(rec) for rec in recs]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(recs))) as pool:
            records = list(pool.map(entry, recs))
    # The access list rides this portion: the panel's records
    # section is where live-but-unrecorded sessions surface.
    return {"records": records, "access": access_list(colab, repo)}


def state_specs(colab, repo: Path) -> dict:
    """Portion: the spec catalog (CLI shapes read from the day
    cache; a portion read never triggers a fresh catalog parse
    beyond what specs() itself already does)."""
    _shapes, catalog_error = _cli_shapes(repo, colab)
    out = {"specs": specs(repo, colab), "session_gate": session_gate_policy(repo)}
    if catalog_error:
        out["catalog_error"] = catalog_error
    return out


def state_budget(colab, repo: Path) -> dict:
    """Portion: the budget snapshot, including the balance read."""
    return {"budget": budget_snapshot(colab, repo)}


def state_grant(colab, repo: Path, budget: dict | None = None) -> dict:
    """Portion: the two-tier grant and its envelope block. The
    envelope prices on a budget snapshot; composed reads pass
    theirs in so the balance is read once."""
    grant = load_grant(repo)
    if budget is None:
        budget = budget_snapshot(colab, repo)
    return {"grant": grant, "envelope": _envelope_block(colab, repo, grant, budget)}


def state_meta(colab, repo: Path) -> dict:
    """Portion: the bridge block and any outstanding auth need."""
    out = {"bridge": _bridge_block(colab, repo)}
    auth = auth_required(repo)
    if auth:
        out["auth_required"] = auth
    return out


def state_extend_guard(repo: Path) -> dict:
    """Portion: the auto-extension guard's resolved policy and
    its latest verdict per lease, from the guard's own state
    file. Before the guard's first pass the policy resolves
    from config and the verdicts are empty."""
    policy, _findings = extend_guard.load_policy(load_config(repo).get("extend_guard"))
    verdicts = extend_guard.guard_state(repo).get("verdicts", {})
    return {"extend_guard": {"policy": policy, "verdicts": verdicts}}


def state_orphans(colab, repo: Path) -> dict:
    """Portion: the orphan census — live sessions with no lease
    record, each entry derive_orphan's composition (the derived
    deadline and its evidence), ready for the panel to offer an
    adoption. Bridge-prefixed sessions are the bridge's, never
    the lease system's, and stay out; a handover child stays in,
    flagged, because its refusal reason is exactly what the
    operator needs to see. An unreadable live list serves an
    empty census flagged unreadable — no orphans rather than
    invented ones."""
    live = live_sessions(colab, repo)
    if live is None:
        return {"orphans": [], "unreadable": True}
    recorded = {rec.get("session") for rec in all_records(repo)}
    out = []
    for entry in live:
        name = entry.get("session")
        if not name or name in recorded or name.startswith("bridge-"):
            continue
        out.append(derive_orphan(repo, entry))
    return {"orphans": out}


def state(colab, repo: Path) -> dict:
    """The UI's composed read: every lease record's status, the
    two-tier grant, the spec catalog, and the budget snapshot,
    assembled from the portion reads above. All derivation lives
    here in the core; the UI only renders."""
    budget = budget_snapshot(colab, repo)
    out = {}
    out.update(state_records(colab, repo))
    out.update(state_specs(colab, repo))
    out["budget"] = budget
    out.update(state_grant(colab, repo, budget))
    out.update(state_meta(colab, repo))
    out.update(state_orphans(colab, repo))
    out.update(state_extend_guard(repo))
    return out


def request_path(repo: Path) -> Path:
    return var_dir(repo) / "lease-request.json"


def write_request(repo: Path, reason: str = "", shape: str | None = None, now_dt: datetime | None = None, caller: str | None = None, via: str | None = None, not_before: datetime | str | None = None) -> dict:
    """Record a demand for a lease (var/lease-request.json). Any driver
    may write it (bin/colab-lease request); the auto-acquire duty is the
    only consumer. `caller` is the requester: auto_acquire copies it
    onto the lease record it creates, so a lease started by demand is
    attributable to the thread that asked (caller-attribution plan —
    the demand's old `via` tag generalized; `via` remains accepted as
    the same claim's legacy spelling). `not_before` makes the demand a
    timed start: the duty leaves it untouched until its time comes
    (two-tier envelope plan — a timed start is data the hook consumes,
    never an agent-fired command)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    if caller is None:
        caller = via
    # Session shape gate, early refusal: a demand whose requester
    # is identified (a caller claim) and whose shape is catalogued
    # is checked now, so a refused demand never sits in the file for
    # the duty to retry. An unattributed demand (no caller) defers
    # to the consumption check in auto_acquire, which re-checks
    # before acquiring. The check is shape-only (creation=False):
    # a demand is an ask, never a creation.
    if caller is not None and shape is not None and shape in catalog_shapes(repo):
        refusal = enforce_session_admission(repo, shape, seam="request", caller=caller, creation=False)
        if refusal is not None:
            return refusal
    req = {"requested_at": now_dt.isoformat(), "reason": reason, "shape": shape}
    if caller:
        req["caller"] = caller
    if not_before is not None:
        req["not_before"] = not_before.isoformat() if isinstance(not_before, datetime) else parse_dt(str(not_before)).isoformat()
    p = request_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(req, indent=2))
    return {"action": "requested", "request": req}


def load_request(repo: Path) -> dict | None:
    req = load_json(request_path(repo), None)
    return req if isinstance(req, dict) and req.get("requested_at") else None


def clear_request(repo: Path) -> None:
    request_path(repo).unlink(missing_ok=True)


def auto_enabled(cfg: dict) -> bool:
    """The config seam is config/lease.json's auto_acquire.enabled; the
    COLAB_RUNNER_AUTO_ACQUIRE env var overrides it (the test harness
    uses the override so it never edits the tracked config)."""
    env = os.environ.get("COLAB_RUNNER_AUTO_ACQUIRE")
    if env is not None:
        return env == "1"
    return bool(cfg.get("auto_acquire", {}).get("enabled"))


def auto_acquire(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty, inert unless auto-acquire is enabled: when a driver
    has left a demand request and no live lease satisfies it, acquire
    one through acquire() — the same budget guard, keepalive launch,
    and kernel pulse as a manual acquire. The request is consumed when
    a lease is live or newly created; it is kept when acquisition was
    refused or failed, so a later poll retries. Makes no colab calls at
    all while disabled or when there is no demand.

    A demand is satisfied by a live lease at its shape held by its
    own caller (an unattributed demand, by any live lease at the
    shape): leases are per-caller, so another caller's lease at the
    same shape never silently satisfies this caller's ask."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not auto_enabled(cfg):
        return {"action": "auto-disabled"}
    req = load_request(repo)
    if req is None:
        return {"action": "no-demand"}
    not_before = req.get("not_before")
    if not_before:
        try:
            due_at = parse_dt(str(not_before))
        except ValueError:
            due_at = None  # an unparseable stamp never wedges the demand
        if due_at is not None and now_dt < due_at:
            # A timed start that has not come due: the request is
            # neither consumed nor failed — it simply waits for a
            # later poll. No colab calls happen on this path.
            return {"action": "not-due", "not_before": not_before, "shape": req.get("shape")}
    shapes = catalog_shapes(repo, colab)
    shape_id = req.get("shape") or default_shape(repo)
    if shape_id not in shapes:
        return {"action": "auto-failed", "error": f"unknown shape {shape_id!r}"}
    demand_caller = req.get("caller") or req.get("via")
    candidates = [r for r in all_records(repo) if r.get("shape") == shape_id and (demand_caller is None or r.get("caller") == demand_caller)]
    rec = max(candidates, key=lambda r: r.get("deadline") or "", default=None)
    if rec:
        state = session_state(colab, rec["session"])
        if state == "alive":
            clear_request(repo)
            return {"action": "satisfied-live", "session": rec["session"]}
        if state == "unknown":
            return {"action": "skipped-unknown", "session": rec["session"]}
        append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
        delete_record(repo, rec["session"])  # confirmed dead: clear before acquiring
    # The duty creates on the demand: the demand's caller becomes
    # the lease's caller (a pre-attribution demand's via tag reads
    # as the same claim), and the duty marker carries the creation
    # past the agent creation denial — the shape check inside
    # acquire still applies in full.
    result = acquire(colab, repo, shape_id, now_dt=now_dt, caller=demand_caller, duty=True)
    action = result.get("action")
    if action == "created":
        clear_request(repo)
        return {"action": "auto-acquired", "session": result["record"]["session"], "deadline": result["record"]["deadline"], "reason": req.get("reason", ""), "via": req.get("via"), "caller": demand_caller}
    if action == "adopted":
        clear_request(repo)
        return {"action": "satisfied-live", "session": result["record"]["session"]}
    if action == "shape-refused":
        # A shape refusal is permanent for this demand (unlike a
        # budget refusal, which a later poll may outgrow): clear
        # the demand so the duty never retries it forever. The
        # refusal itself is journaled at the acquire seam; this
        # entry records the demand's disposition.
        clear_request(repo)
        append_gate_journal(repo, {"kind": "demand-shape-refused", "seam": "auto", "shape": shape_id, "via": req.get("via"), "caller": demand_caller, "shape_class": result.get("shape_class")})
        return {"action": "skipped-shape", "shape": shape_id, "shape_class": result.get("shape_class")}
    if action == "refused-budget":
        return {"action": "skipped-budget", "budget": result.get("budget")}
    if action == "acquire-unknown":
        return {"action": "skipped-unknown", "session": result.get("session")}
    return {"action": "auto-failed", "error": result.get("error", action)}


