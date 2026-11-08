import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, gate as _mod_gate, spend, grants, renewal

BRIDGE_KIT_REMOTE = "/content/bridge-kit"
BRIDGE_REMOTE = "/content/bridge"
BRIDGE_PROTOCOL = 2
BRIDGE_READ_TIMEOUT_S = 10
BRIDGE_INSTALL_COOLDOWN_S = 600
BRIDGE_HEARTBEAT_STALE_S = 45


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
    return foundation.var_dir(repo) / "bridge-arm.json"


def _bridge_arm(repo: Path, rec: dict) -> dict:
    """The arm record ties a bridge generation to one standing session
    lifetime: a recycled or renewed standing lease (new session or new
    acquired_at) starts a new generation; an agent death inside the
    same session re-installs under the same generation, so the
    agent resumes its log segments and registry."""
    import uuid

    arm = foundation.load_json(_bridge_arm_path(repo), None)
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
    dest = foundation.var_dir(repo) / "bridge-status-dl.json"
    try:
        r = colab("download", "-s", session, f"{BRIDGE_REMOTE}/status.json", str(dest), timeout=BRIDGE_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return None
        data = json.loads(dest.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


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
    cfg = foundation.load_config(repo)
    shapes = {}
    for sid, entry in catalog.catalog_shapes(repo, colab).items():
        shapes[sid] = {"high_mem": bool(entry.get("high_mem")), "gpu": entry.get("gpu"), "tpu": entry.get("tpu"), "rate_cu_per_hour": entry.get("rate_cu_per_hour")}
    var = foundation.var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    # Session shape gate: the bridge agent mints children in-VM,
    # beyond the core's reach, so the install stamps the shapes
    # an agent mint may create under the current policy; the agent
    # refuses any other shape at mint time.
    gate = _mod_gate.session_gate_policy(repo)
    agent_allowed = sorted(sid for sid, entry in shapes.items() if not gate["enabled"] or _mod_gate.shape_class(entry) in gate["agent_classes"])
    cfg_local = var / "bridge-config.json"
    # Caller attribution: the bridge mints children in-VM under
    # this install's authority, so the config stamps the standing
    # record's caller (the machinery caller when the record
    # predates attribution); child reports inherit it.
    cfg_local.write_text(json.dumps({"session": session, "generation": generation, "limits": bridge_limits(cfg), "shapes": shapes, "agent_allowed_shapes": agent_allowed, "caller": rec.get("caller") or "lease"}, indent=2))
    if not renewal._ensure_remote_dirs(colab, repo, session, [BRIDGE_KIT_REMOTE, f"{BRIDGE_REMOTE}/queue", f"{BRIDGE_REMOTE}/log", f"{BRIDGE_REMOTE}/results"], "bridge"):
        return {"bridge_install": "install-failed", "stage": "mkdir"}
    uploads = [
        (str(repo / "snippets" / "bridge_agent.py"), f"{BRIDGE_KIT_REMOTE}/bridge_agent.py"),
        (str(repo / "snippets" / "_remote_guard.py"), f"{BRIDGE_KIT_REMOTE}/_remote_guard.py"),
        (str(token_src), f"{BRIDGE_KIT_REMOTE}/token.json"),
        (str(cfg_local), f"{BRIDGE_KIT_REMOTE}/bridge.json"),
    ]
    for src, dest in uploads:
        try:
            r = colab("upload", "-s", session, src, dest, timeout=renewal.KIT_UPLOAD_TIMEOUT_S)
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
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=renewal.KIT_LAUNCH_TIMEOUT_S)
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
    rec = {"session": line["name"], "shape": shape_id, "rate_cu_per_hour": catalog.shape_rate(repo, shape_id), "acquired_at": line["minted_at"]}
    if line.get("caller"):
        rec["caller"] = line["caller"]
    try:
        end = foundation.parse_dt(line["ts"])
    except Exception:  # noqa: BLE001
        return False
    spend.append_spend_segment(repo, rec, end)
    return True


def _bridge_ferry(colab, repo: Path, session: str, status: dict, now_dt: datetime) -> dict:
    """One ferry pass: mirror new log lines (by generation, segment,
    byte offset — only complete lines advance the offset), fold
    child stop lines into the spend journal, drop outbox commands
    whose terminal line reached the mirror, and plant the rest.
    The outbox is the source of truth: a command leaves it only on
    acknowledgement, and a generation change re-plants everything
    unacknowledged. Bounded and non-raising throughout."""
    var = foundation.var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    outbox = var / "bridge-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    mirror_path = var / "bridge-mirror.jsonl"
    fstate_path = var / "bridge-ferry.json"
    fstate = foundation.load_json(fstate_path, None)
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
        cfg = foundation.load_config(repo)
        if not bridge_limits(cfg)["enabled"]:
            return {"bridge": "disabled"}
        grant = grants.active_grant(repo, now_dt)
        if grant is None:
            return {"bridge": "no-grant"}
        rec = grants.standing_record(repo)
        if not rec:
            return {"bridge": "no-standing"}
        session = rec["session"]
        arm = _bridge_arm(repo, rec)
        status = bridge_status_read(colab, repo, session)
        age = None
        if status is not None and status.get("gen") == arm["generation"] and status.get("ts"):
            try:
                age = max(0.0, now_dt.timestamp() - foundation.parse_dt(status["ts"]).timestamp())
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
