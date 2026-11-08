import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, gate as _mod_gate, sessions, records, spend, keepalive, grants

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


def _shape_spec_for(repo: Path, shape_id: str | None, rec: dict | None = None) -> dict:
    """A shape's descriptor fields ({gpu, tpu, high_mem}): the
    catalog entry when the catalog knows the shape, else the
    descriptor the lease record itself carries (stamped at
    creation/translation, so a lease's own shape survives a cold
    catalog cache), else plain CPU."""
    entry = catalog.catalog_shapes(repo).get(shape_id) if shape_id else None
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
    var = foundation.var_dir(repo)
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


def _swap_session(colab, repo: Path, rec: dict, new_session: str, acquired_at: str, now_dt: datetime) -> None:
    """The record half of a renewal, shared by both drivers: only the
    session and the session's acquired_at change — deadline, jobs,
    and shape are the window's and stay put. The keepalive is
    relaunched on the new session and the kernel pulsed, as at
    acquire."""
    spend.append_spend_segment(repo, rec, now_dt)  # the old session's segment closes here
    old_session = rec["session"]
    rec["session"] = new_session
    rec["acquired_at"] = acquired_at
    rec["last_used_at"] = now_dt.isoformat()
    records.save_record(repo, rec)
    if old_session != new_session:
        # The record's file is keyed by session: the old session's
        # file still holds the pre-swap record under the old name
        # and must not survive as a second, stale lease.
        records.delete_record(repo, old_session)
    if keepalive._capture_session_host(repo, rec, "renewal", spool_on_first=True):
        records.save_record(repo, rec)
    keepalive.ensure_keepalive(colab, repo, new_session)
    rec["last_pulse_at"] = now_dt.isoformat()
    rec["last_pulse_result"] = keepalive.kernel_pulse(colab, repo, new_session)
    records.save_record(repo, rec)


def _adopt_handover(colab, repo: Path, cfg: dict, rec: dict, now_dt: datetime) -> dict | None:
    """Renewal driver 1 (nested): pull the handover file the in-VM
    renewal kit wrote on the parent session, merge the child's
    binding into the local sessions file programmatically (contents
    never printed), swap the record onto the child, and stop the
    parent once the child answers. Returns the renewal detail, or
    None when there is no usable handover (yet)."""
    dest = foundation.var_dir(repo) / "lease-handover.json"
    try:
        r = colab("download", "-s", rec["session"], "/content/lease-handover.json", str(dest), timeout=HANDOVER_READ_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 or not dest.exists():
        return None
    data = foundation.load_json(dest, None)
    if not isinstance(data, dict):
        return None
    child = data.get("child_session")
    binding = data.get("binding")
    if not child or not isinstance(binding, dict) or child not in binding:
        return None
    sessions_file = sessions.local_sessions_path()
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
    if sessions.session_state(colab, child) != "alive":
        return None  # binding merged; the child does not answer yet — retry next cycle
    old_session = rec["session"]
    rec["kit"] = "installed"  # the kit installs itself in the child
    _swap_session(colab, repo, rec, child, data.get("minted_at") or now_dt.isoformat(), now_dt)
    sessions.stop_session_safe(colab, old_session)
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
    shape = catalog.catalog_shapes(repo).get(shape_id)
    if shape is None:
        shape = {"id": shape_id, "rate_cu_per_hour": rec.get("rate_cu_per_hour"), **_shape_spec_for(repo, shape_id, rec)}
    # Session shape gate: a renewal re-creates the session, so the
    # record's shape is re-checked (renewal_admission — no actor
    # bypass). A refusal recreates nothing: returning None keeps
    # the record for the next cycle, whose reap surfaces
    # renewal-failed until the window's end releases it — the shape
    # is surfaced, never silently perpetuated.
    gate = _mod_gate.renewal_admission(repo, shape_id, shape)
    if not gate["allowed"]:
        _mod_gate.append_gate_journal(repo, {"kind": "shape-refused", "seam": "renewal", "shape": shape_id, "shape_class": gate["shape_class"], "agent_classes": gate["agent_classes"]})
        return None
    # The renewal child derives from the lease's own name: the
    # generated name with any earlier renewal suffixes stripped, so
    # a lease's sessions read as one family for its whole window.
    base = rec["session"]
    while re.search(r"-r\d+$", base):
        base = re.sub(r"-r\d+$", "", base)
    new_session = f"{base}-r{int(now_dt.timestamp())}"
    try:
        r = colab(*(["new", "-s", new_session] + catalog._session_shape_args(shape)), timeout=600)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
        return None
    old_session = rec["session"]
    _swap_session(colab, repo, rec, new_session, now_dt.isoformat(), now_dt)
    kit = install_renewal_kit(colab, repo, rec, now_dt)
    if kit.get("kit") == "installed":
        rec["kit"] = "installed"
        records.save_record(repo, rec)
    if old_session != new_session:
        sessions.stop_session_safe(colab, old_session)
    return {"driver": 2, "old_session": old_session, "new_session": new_session}


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
            if result is None and now_dt - foundation.parse_dt(rec["acquired_at"]) < timedelta(hours=SESSION_CAP_HOURS):
                return None
        if result is None:
            result = _renew_fresh_session(colab, repo, cfg, rec, now_dt)
        if result is not None:
            foundation.append_reaper_event(repo, {"kind": "lease-renewed", "shape": rec.get("shape"), **result})
            if rec.get("envelope_id"):
                grants.append_grant_ledger(repo, {"kind": "lease-renewed", "envelope_id": rec.get("envelope_id"), "shape": rec.get("shape"), **result})
        return result
    except Exception:  # noqa: BLE001
        return None


def _ensure_remote_dirs(colab, repo: Path, session: str, dirs: list[str], tag: str) -> bool:
    """Create remote directories before uploading into them: the
    file channel 500s on an upload whose parent directory does not
    exist (bridge probe, 2026-11-01 — the same defect that failed
    the renewal kit's first installs at the upload stage). One
    bounded exec running os.makedirs in the session; False on any
    failure, and the caller reports its own stage."""
    var = foundation.var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    runner = var / f"mkdir-{tag}.py"
    runner.write_text("import os\n" + "".join(f"os.makedirs({d!r}, exist_ok=True)\n" for d in dirs) + "print('dirs ready')\n")
    try:
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(runner), timeout=KIT_LAUNCH_TIMEOUT_S)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
