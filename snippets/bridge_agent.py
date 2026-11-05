#!/usr/bin/env python3
"""Bridge agent: the in-VM half of the standing bridge
(.plans/2026-11-01-lobby-bridge.md). Installed into the standing
lease by the lease core (sync/lease.py's install_bridge_kit)
alongside _remote_guard.py, the bridge config, and the account
token. Two modes:

  --setup   one-shot (the renewal kit's pattern): place the staged
            token at its canonical path (atomic, mode 600), build
            the CLI venv in the kit directory, spawn the agent
            detached, exit. Readiness is ready.json, written by the
            agent itself; a setup failure lands in setup-error.json.
  (agent)   the bridge loop: poll the queue directory for command
            files, execute them (OS commands in this session, or
            relayed into bridge-minted child sessions through the
            in-VM CLI), and append every outcome as a JSON line to
            the segmented bridge log. Heartbeats rewrite status.json
            on the same cadence the lease core reads it by.

Protocol (v1): a command file queue/<id>.json is
{"v": 2, "id": ..., "verb": "run"|"mint"|"stop"|"ping", ...,
"sha256": <hash>} where the hash is SHA-256 over the canonical
JSON of every other field — a partially planted file never
executes. An id executes at most once per bridge generation
(processed-id registry); a re-plant of a completed id is answered
with a duplicate line, not a re-run. Children live in the bridge-
prefix namespace, are registered at mint, pulsed on an interval,
and reaped on idle or at a hard age cap. Stops use the measured
form `colab stop -s <name>` (the --yes flag and the positional
form both fail rc 2 — bridge probe, 2026-11-01).

Standard library only. Refuses to run off a Colab VM via the
_remote_guard pattern. The token and the sessions file are never
printed or logged; the log carries command outputs (bounded
tails), timings, and lifecycle events only.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL = 2
KIT = Path("/content/bridge-kit")
ROOT = Path("/content/bridge")
QUEUE = ROOT / "queue"
LOGDIR = ROOT / "log"
RESULTS = ROOT / "results"
REGISTRY_FILE = ROOT / "registry.json"
STATUS_FILE = ROOT / "status.json"
READY_FILE = ROOT / "ready.json"
STOP_FILE = ROOT / "stop"
TOKEN_CANONICAL = Path.home() / ".config" / "colab-cli" / "token.json"
OUT_TAIL = 800
FULL_OUT_CAP = 200_000
SEGMENT_BYTES = 512_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# --- Protocol (pure; unit-tested off-VM) ---------------------------------------


def canonical(fields: dict) -> str:
    return json.dumps({k: v for k, v in fields.items() if k != "sha256"}, sort_keys=True, separators=(",", ":"))


def command_hash(fields: dict) -> str:
    return hashlib.sha256(canonical(fields).encode()).hexdigest()


def validate_command(obj) -> tuple[dict | None, str | None]:
    """A command object, or (None, reason). Whole-file or nothing:
    shape, protocol version, and the content hash must all hold."""
    if not isinstance(obj, dict):
        return None, "not-an-object"
    if obj.get("v") != PROTOCOL:
        return None, "protocol-mismatch"
    if not isinstance(obj.get("id"), str) or not obj["id"]:
        return None, "missing-id"
    if obj.get("verb") not in ("run", "mint", "stop", "ping"):
        return None, "unknown-verb"
    if obj.get("sha256") != command_hash(obj):
        return None, "hash-mismatch"
    if obj["verb"] == "run":
        if not isinstance(obj.get("cmd"), str) or not obj["cmd"]:
            return None, "missing-cmd"
        if not isinstance(obj.get("target"), str) or not obj["target"]:
            return None, "missing-target"
    if obj["verb"] == "mint" and not isinstance(obj.get("shape"), str):
        return None, "missing-shape"
    if obj["verb"] == "stop" and not isinstance(obj.get("name"), str):
        return None, "missing-name"
    return obj, None


def segment_name(n: int) -> str:
    return f"bridge-{n:06d}.jsonl"


# --- Segmented log --------------------------------------------------------------


class BridgeLog:
    """Append-only JSON lines across size-rotated segment files.
    A single lock serializes writers (the agent loop and its worker
    threads); every line carries the generation and a per-generation
    sequence number so the ferry can mirror by (segment, offset)
    and dedupe by id."""

    def __init__(self, logdir: Path, generation: str):
        self.dir = logdir
        self.generation = generation
        self.lock = threading.Lock()
        self.segment = 1
        self.seq = 0
        self.path = logdir / segment_name(1)
        # A re-arm within one generation (agent restart, same
        # /content) resumes the last segment and sequence.
        existing = sorted(logdir.glob("bridge-*.jsonl"))
        if existing:
            self.path = existing[-1]
            try:
                self.segment = int(self.path.name[7:13])
            except ValueError:
                self.segment = len(existing)
            try:
                for line in self.path.read_text().splitlines():
                    try:
                        self.seq = max(self.seq, int(json.loads(line).get("seq", 0)))
                    except (json.JSONDecodeError, ValueError):
                        continue
            except OSError:
                pass

    def write(self, obj: dict) -> dict:
        with self.lock:
            self.seq += 1
            line = {"v": PROTOCOL, "gen": self.generation, "seq": self.seq, "ts": now_iso(), **obj}
            try:
                if self.path.exists() and self.path.stat().st_size > SEGMENT_BYTES:
                    self.segment += 1
                    self.path = self.dir / segment_name(self.segment)
                with self.path.open("a") as fh:
                    fh.write(json.dumps(line) + "\n")
            except OSError:
                pass
            return line


# --- Child + processed-id registry ----------------------------------------------


class Registry:
    """Bridge-owned children and processed command ids, persisted
    atomically (tmp sibling + rename) after every change."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.children: dict[str, dict] = {}
        self.processed: dict[str, int] = {}
        self.inflight: set[str] = set()
        self.attempts: dict[str, int] = {}
        try:
            data = json.loads(path.read_text())
            if isinstance(data.get("children"), dict):
                self.children = data["children"]
            if isinstance(data.get("processed"), dict):
                self.processed = data["processed"]
            if isinstance(data.get("inflight"), list):
                self.inflight = set(data["inflight"])
            if isinstance(data.get("attempts"), dict):
                self.attempts = data["attempts"]
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        with self.lock:
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps({"children": self.children, "processed": self.processed, "inflight": sorted(self.inflight), "attempts": self.attempts}, indent=1))
                os.replace(tmp, self.path)
            except OSError:
                pass

    def note_attempt(self, cid: str) -> int:
        """Count one execution take of a command. A command may be
        taken at most twice in a generation (its first run plus one
        re-queue after an agent death); a command whose own
        execution kills the agent is quarantined at the third take
        instead of looping every re-arm."""
        with self.lock:
            self.attempts[cid] = int(self.attempts.get(cid, 0)) + 1
            n = self.attempts[cid]
            self.save()
            return n

    def mark_inflight(self, cid: str) -> None:
        self.inflight.add(cid)
        self.save()

    def clear_inflight(self, cid: str) -> None:
        self.inflight.discard(cid)
        self.save()

    def mark_processed(self, cid: str, seq: int) -> None:
        with self.lock:
            self.processed[cid] = seq
            if len(self.processed) > 500:
                keep = sorted(self.processed.items(), key=lambda kv: kv[1])[-500:]
                self.processed = dict(keep)
            self.save()

    def add_child(self, name: str, info: dict) -> None:
        with self.lock:
            self.children[name] = info
            self.save()

    def touch_child(self, name: str) -> None:
        with self.lock:
            if name in self.children:
                self.children[name]["last_used_at"] = now_iso()
                self.save()

    def remove_child(self, name: str) -> dict | None:
        with self.lock:
            info = self.children.pop(name, None)
            self.save()
            return info


# --- The in-VM CLI ----------------------------------------------------------------


def cli_bin() -> Path:
    return KIT / "venv" / "bin" / "colab"


def run_cli(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(cli_bin()), "--auth", "oauth2", *args], capture_output=True, text=True, timeout=timeout)


def auth_failed(res: subprocess.CompletedProcess) -> bool:
    text = ((res.stdout or "") + (res.stderr or "")).lower()
    return res.returncode != 0 and ("auth" in text or "login" in text or "credential" in text)


# --- Setup (the renewal kit's pattern) --------------------------------------------


def place_token() -> None:
    staged = KIT / "token.json"
    if staged.exists():
        TOKEN_CANONICAL.parent.mkdir(parents=True, exist_ok=True)
        tmp = TOKEN_CANONICAL.with_name(TOKEN_CANONICAL.name + ".tmp")
        shutil.copyfile(staged, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKEN_CANONICAL)
        staged.unlink()


def build_venv() -> bool:
    """The measured build steps (broker probe): venv --without-pip,
    seed pip, install the official CLI with --ignore-installed."""
    venv = KIT / "venv"
    vpy = venv / "bin" / "python"
    if cli_bin().exists():
        return True
    try:
        if not vpy.exists():
            subprocess.run(["python3", "-m", "venv", "--without-pip", str(venv)], capture_output=True, timeout=600)
            if not vpy.exists():
                return False
        if subprocess.run([str(vpy), "-m", "pip", "--version"], capture_output=True, timeout=120).returncode != 0:
            subprocess.run([str(vpy), "-m", "ensurepip", "--upgrade"], capture_output=True, timeout=600)
        if subprocess.run([str(vpy), "-m", "pip", "--version"], capture_output=True, timeout=120).returncode != 0:
            get_pip = KIT / "get-pip.py"
            urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", get_pip)
            subprocess.run([str(vpy), str(get_pip)], capture_output=True, timeout=600)
        r = subprocess.run([str(vpy), "-m", "pip", "install", "--quiet", "--ignore-installed", "google-colab-cli"], capture_output=True, timeout=900)
        return r.returncode == 0 and cli_bin().exists()
    except Exception:  # noqa: BLE001
        return False


def agent_running() -> bool:
    pid_file = KIT / "agent.pid"
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def setup(config_path: Path) -> int:
    KIT.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    # Arming supersedes a stale stop file (the probe era left one in
    # this directory): a stop only means something to the agent
    # generation that was running when it was written.
    STOP_FILE.unlink(missing_ok=True)
    (KIT / "setup-error.json").unlink(missing_ok=True)
    try:
        place_token()
        if not build_venv():
            (KIT / "setup-error.json").write_text(json.dumps({"stage": "venv", "ts": now_iso()}))
            return 1
        if not agent_running():
            env = dict(os.environ)
            env["COLAB_RELEASE_TAG"] = "bridge-agent"
            with (KIT / "agent.out").open("a") as out:
                proc = subprocess.Popen(
                    [str(KIT / "venv" / "bin" / "python"), str(KIT / "bridge_agent.py"), "--config", str(config_path)],
                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env,
                )
            (KIT / "agent.pid").write_text(str(proc.pid))
        return 0
    except Exception as exc:  # noqa: BLE001
        (KIT / "setup-error.json").write_text(json.dumps({"stage": type(exc).__name__, "ts": now_iso()}))
        return 1


# --- The agent --------------------------------------------------------------------


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = cfg.get("session", "")
        self.generation = cfg.get("generation", "")
        limits = cfg.get("limits", {})
        self.max_children = int(limits.get("max_children", 3))
        self.child_idle_s = float(limits.get("child_idle_minutes", 30)) * 60
        self.child_max_s = float(limits.get("child_max_hours", 4)) * 3600
        self.default_timeout = float(limits.get("command_timeout_seconds", 300))
        self.shapes = cfg.get("shapes", {})
        # Caller attribution: the caller this bridge installation
        # acts for, stamped into the config at kit install from the
        # standing record; child entries and stop lines inherit it, so
        # bridge-minted spend attributes like any lease record.
        # Absent in pre-attribution configs.
        self.caller = cfg.get("caller")
        # Session shape gate: the shapes an agent-driven mint may
        # create, stamped into this config at kit install. A config
        # without the stamp (a bridge armed before the gate) derives
        # the same set from the snapshot's own shapes, restating the
        # core's classifier: gpu/tpu are their own classes, high_mem
        # is high-RAM, the rest standard — and only high-RAM is
        # agent-allowed.
        stamped = cfg.get("agent_allowed_shapes")
        if isinstance(stamped, list):
            self.mint_allowed = {str(n) for n in stamped}
        else:
            self.mint_allowed = {name for name, p in self.shapes.items() if not p.get("gpu") and not p.get("tpu") and p.get("high_mem")}
        for d in (QUEUE, LOGDIR, RESULTS):
            d.mkdir(parents=True, exist_ok=True)
        self.log = BridgeLog(LOGDIR, self.generation)
        self.registry = Registry(REGISTRY_FILE)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.inflight_lock = threading.Lock()
        self.target_locks: dict[str, threading.Lock] = {}
        self.auth_down = False
        self._recover_inflight()

    # -- intake recovery: a command taken but never completed is re-queued --
    def _recover_inflight(self) -> None:
        inflight = sorted(self.registry.inflight)
        if not inflight:
            return
        terminal = set()
        for seg in sorted(LOGDIR.glob("bridge-*.jsonl")):
            try:
                for line in seg.read_text().splitlines():
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("kind") in ("result", "mint", "stop", "pong", "error") and obj.get("id"):
                        terminal.add(obj["id"])
            except OSError:
                continue
        for cid in inflight:
            taken = QUEUE / f"{cid}.json.taken"
            if cid in terminal:
                self.registry.processed.setdefault(cid, 0)
                taken.unlink(missing_ok=True)
            elif taken.exists():
                taken.rename(QUEUE / f"{cid}.json")
            self.registry.inflight.discard(cid)
        self.registry.save()

    # -- CLI helpers --
    def _cli(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess | None:
        try:
            return run_cli(*args, timeout=timeout)
        except Exception:  # noqa: BLE001
            return None

    def _note_auth(self, res) -> bool:
        if res is not None and auth_failed(res):
            if not self.auth_down:
                self.auth_down = True
                self.log.write({"kind": "auth-failed"})
            return True
        return False

    def _sessions(self) -> set[str] | None:
        res = self._cli("sessions", timeout=60)
        if res is None or res.returncode != 0:
            return None
        names = set()
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("[") and "]" in line:
                names.add(line[1 : line.index("]")])
        return names

    # -- verbs --
    def _result_line(self, cmd: dict, verdict: dict) -> None:
        line = self.log.write({"kind": "result", "id": cmd["id"], "target": cmd.get("target"), **verdict})
        self.registry.mark_processed(cmd["id"], line["seq"])

    def _error_line(self, cmd: dict, error: str, **extra) -> None:
        line = self.log.write({"kind": "error", "id": cmd["id"], "verb": cmd.get("verb"), "error": error, **extra})
        self.registry.mark_processed(cmd["id"], line["seq"])

    def do_run(self, cmd: dict) -> None:
        target = cmd["target"]
        timeout = min(float(cmd.get("timeout_s") or self.default_timeout), 3600.0)
        if target != "self" and target not in self.registry.children:
            self._error_line(cmd, "unknown-target")
            return
        lock = None
        if cmd.get("serial"):
            lock = self.target_locks.setdefault(target, threading.Lock())
            lock.acquire()
        try:
            if target == "self":
                verdict = self._run_self(cmd, timeout)
            else:
                verdict = self._run_child(cmd, target, timeout)
        finally:
            if lock is not None:
                lock.release()
        if verdict.pop("auth", False):
            self._error_line(cmd, "auth-failed")
            return
        self._result_line(cmd, verdict)
        if target != "self":
            self.registry.touch_child(target)

    def _store_full(self, cid: str, out: str, err: str) -> str | None:
        if len(out) + len(err) <= OUT_TAIL * 2:
            return None
        try:
            p = RESULTS / f"{cid}.txt"
            p.write_text((out + "\n--- stderr ---\n" + err)[:FULL_OUT_CAP])
            return str(p)
        except OSError:
            return None

    def _run_self(self, cmd: dict, timeout: float) -> dict:
        t0 = time.monotonic()
        try:
            r = subprocess.run(["bash", "-lc", cmd["cmd"]], capture_output=True, text=True, timeout=timeout)
            out, err = r.stdout or "", r.stderr or ""
            verdict = {"rc": r.returncode, "out": out[-OUT_TAIL:], "err": err[-200:], "seconds": round(time.monotonic() - t0, 2)}
        except subprocess.TimeoutExpired:
            verdict = {"rc": None, "error": "timeout", "out": "", "err": "", "seconds": round(time.monotonic() - t0, 2)}
        except Exception as exc:  # noqa: BLE001
            verdict = {"rc": None, "error": type(exc).__name__, "out": "", "err": "", "seconds": round(time.monotonic() - t0, 2)}
        stored = self._store_full(cmd["id"], verdict.get("out", ""), verdict.get("err", ""))
        if stored:
            verdict["result_file"] = stored
        return verdict

    def _run_child(self, cmd: dict, child: str, timeout: float) -> dict:
        runner = RESULTS / f"{cmd['id']}.runner.py"
        runner.write_text(
            "import json, subprocess\n"
            f"r = subprocess.run(['bash', '-lc', {cmd['cmd']!r}], capture_output=True, text=True, timeout={max(1, int(timeout))})\n"
            "print(json.dumps({'rc': r.returncode, 'out': r.stdout[-800:], 'err': r.stderr[-200:]}))\n"
        )
        t0 = time.monotonic()
        res = self._cli("exec", "-s", child, "--timeout", str(int(timeout) + 60), "-f", str(runner), timeout=int(timeout) + 180)
        seconds = round(time.monotonic() - t0, 2)
        if res is None:
            return {"rc": None, "error": "relay-failed", "seconds": seconds}
        if self._note_auth(res):
            return {"auth": True}
        for line in reversed((res.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    verdict = json.loads(line)
                    verdict["seconds"] = seconds
                    return verdict
                except json.JSONDecodeError:
                    continue
        # No verdict: the child may be gone. Confirm before believing it.
        sessions = self._sessions()
        if sessions is not None and child not in sessions:
            info = self.registry.remove_child(child)
            self.log.write({"kind": "child-gone", "name": child, "shape": (info or {}).get("shape")})
            return {"rc": None, "error": "child-gone", "seconds": seconds}
        return {"rc": None, "error": "no-verdict", "relay_rc": res.returncode, "seconds": seconds}

    def do_mint(self, cmd: dict) -> None:
        shape = self.shapes.get(cmd["shape"])
        if shape is None:
            self._error_line(cmd, "unknown-shape")
            return
        if cmd["shape"] not in self.mint_allowed:
            self._error_line(cmd, "shape-refused", shape=cmd["shape"])
            return
        if self.auth_down:
            self._error_line(cmd, "auth-failed")
            return
        if len(self.registry.children) >= self.max_children:
            self._error_line(cmd, "bridge-cap", cap=self.max_children)
            return
        name = cmd.get("name") or f"bridge-{cmd['shape']}-{cmd['id'][:6]}"
        if not name.startswith("bridge-") or not all(c.isalnum() or c in "-_" for c in name):
            self._error_line(cmd, "bad-name")
            return
        if name in self.registry.children:
            self._error_line(cmd, "name-taken")
            return
        args = ["new", "-s", name]
        if shape.get("high_mem"):
            args.append("--high-mem")
        if shape.get("gpu"):
            args += ["--gpu", str(shape["gpu"])]
        if shape.get("tpu"):
            args += ["--tpu", str(shape["tpu"])]
        t0 = time.monotonic()
        res = self._cli(*args, timeout=600)
        if res is None or (res.returncode != 0 and "already" not in ((res.stdout or "") + (res.stderr or ""))):
            if self._note_auth(res):
                self._error_line(cmd, "auth-failed")
            else:
                self._error_line(cmd, "mint-failed")
            return
        info = {"shape": cmd["shape"], "rate": shape.get("rate_cu_per_hour"), "minted_at": now_iso(), "last_used_at": now_iso(), "caller": self.caller}
        self.registry.add_child(name, info)
        line = self.log.write({"kind": "mint", "id": cmd["id"], "name": name, "shape": cmd["shape"], "rate": info["rate"], "seconds": round(time.monotonic() - t0, 1)})
        self.registry.mark_processed(cmd["id"], line["seq"])

    def _stop_child(self, name: str, reason: str, cmd_id: str | None = None) -> bool:
        res = self._cli("stop", "-s", name, timeout=300)
        if res is None or res.returncode != 0:
            if self._note_auth(res):
                return False
            sessions = self._sessions()
            if sessions is None or name in sessions:
                return False
        info = self.registry.remove_child(name) or {}
        line = {"kind": "stop", "name": name, "shape": info.get("shape"), "rate": info.get("rate"), "minted_at": info.get("minted_at"), "reason": reason, "caller": info.get("caller")}
        if cmd_id:
            line["id"] = cmd_id
        written = self.log.write(line)
        if cmd_id:
            self.registry.mark_processed(cmd_id, written["seq"])
        return True

    def do_stop(self, cmd: dict) -> None:
        name = cmd["name"]
        if name not in self.registry.children:
            self._error_line(cmd, "not-bridge-owned")
            return
        if not self._stop_child(name, cmd.get("reason") or "requested", cmd_id=cmd["id"]):
            self._error_line(cmd, "stop-failed")

    def do_ping(self, cmd: dict) -> None:
        line = self.log.write({"kind": "pong", "id": cmd["id"]})
        self.registry.mark_processed(cmd["id"], line["seq"])

    # -- intake --
    def _intake(self) -> None:
        for path in sorted(QUEUE.glob("*.json")):
            cid = path.name[: -len(".json")]
            try:
                raw = path.read_text()
                obj = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                # A partial plant gets one grace window, then quarantine.
                try:
                    if time.time() - path.stat().st_mtime > 10:
                        path.rename(path.with_suffix(".bad"))
                        self.log.write({"kind": "bad-command", "file": path.name, "error": "unparseable"})
                except OSError:
                    pass
                continue
            cmd, reason = validate_command(obj)
            if cmd is None:
                try:
                    if reason == "hash-mismatch" and time.time() - path.stat().st_mtime <= 10:
                        continue
                    path.rename(path.with_suffix(".bad"))
                except OSError:
                    pass
                self.log.write({"kind": "bad-command", "file": path.name, "error": reason})
                continue
            if cmd["id"] in self.registry.processed:
                line = self.log.write({"kind": "duplicate", "id": cmd["id"], "orig_seq": self.registry.processed[cmd["id"]]})
                path.rename(path.with_suffix(".done"))
                continue
            with self.inflight_lock:
                if cmd["id"] in self.registry.inflight:
                    continue
                self.registry.mark_inflight(cmd["id"])
            try:
                path.rename(QUEUE / f"{cmd['id']}.json.taken")
            except OSError:
                with self.inflight_lock:
                    self.registry.clear_inflight(cmd["id"])
                continue
            self.executor.submit(self._execute, cmd)

    def _execute(self, cmd: dict) -> None:
        try:
            if self.registry.note_attempt(cmd["id"]) > 2:
                line = self.log.write({"kind": "bad-command", "id": cmd["id"], "error": "attempt-limit"})
                self.registry.mark_processed(cmd["id"], line["seq"])
                return
            {"run": self.do_run, "mint": self.do_mint, "stop": self.do_stop, "ping": self.do_ping}[cmd["verb"]](cmd)
        except Exception as exc:  # noqa: BLE001 - a verb never kills the agent
            try:
                self._error_line(cmd, f"agent-error:{type(exc).__name__}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self.inflight_lock:
                self.registry.clear_inflight(cmd["id"])
            (QUEUE / f"{cmd['id']}.json.taken").unlink(missing_ok=True)

    # -- periodic duties --
    def _pulse_children(self) -> None:
        pulse = RESULTS / "pulse.py"
        pulse.write_text("print('bridge-pulse')\n")
        for name in list(self.registry.children):
            res = self._cli("exec", "-s", name, "--timeout", "60", "-f", str(pulse), timeout=120)
            info = self.registry.children.get(name)
            if info is None:
                continue
            if res is not None and res.returncode == 0 and "bridge-pulse" in (res.stdout or ""):
                info["misses"] = 0
            else:
                info["misses"] = int(info.get("misses", 0)) + 1
                if info["misses"] >= 2:
                    sessions = self._sessions()
                    if sessions is not None and name not in sessions:
                        gone = self.registry.remove_child(name)
                        self.log.write({"kind": "child-gone", "name": name, "shape": (gone or {}).get("shape")})

    def _reap_children(self) -> None:
        now = now_ts()
        for name, info in list(self.registry.children.items()):
            try:
                age = now - datetime.fromisoformat(info["minted_at"]).timestamp()
                idle = now - datetime.fromisoformat(info["last_used_at"]).timestamp()
            except (KeyError, ValueError):
                continue
            if age > self.child_max_s:
                self._stop_child(name, "cap")
            elif idle > self.child_idle_s:
                self._stop_child(name, "idle")

    def _write_status(self, queue_depth: int) -> None:
        now = now_ts()
        children = []
        for name, info in self.registry.children.items():
            try:
                age = round(now - datetime.fromisoformat(info["minted_at"]).timestamp())
                idle = round(now - datetime.fromisoformat(info["last_used_at"]).timestamp())
            except (KeyError, ValueError):
                age = idle = None
            children.append({"name": name, "shape": info.get("shape"), "age_s": age, "idle_s": idle, "caller": info.get("caller")})
        status = {"v": PROTOCOL, "gen": self.generation, "ts": now_iso(), "session": self.session, "auth_down": self.auth_down, "children": children, "processed": len(self.registry.processed), "queue": queue_depth, "log_segment": self.log.path.name}
        try:
            tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
            tmp.write_text(json.dumps(status, indent=1))
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass

    def run(self) -> int:
        READY_FILE.write_text(json.dumps({"v": PROTOCOL, "gen": self.generation, "ts": now_iso(), "session": self.session, "pid": os.getpid()}))
        self.log.write({"kind": "agent-up", "session": self.session})
        last_beat = last_status = last_pulse = 0.0
        while not STOP_FILE.exists():
            self._intake()
            self._reap_children()
            now = time.monotonic()
            if now - last_pulse >= 60:
                self._pulse_children()
                last_pulse = now
            if now - last_status >= 10:
                self._write_status(len(list(QUEUE.glob("*.json"))))
                last_status = now
            if now - last_beat >= 30:
                self.log.write({"kind": "heartbeat", "processed": len(self.registry.processed), "children": len(self.registry.children)})
                last_beat = now
            time.sleep(2)
        self.log.write({"kind": "agent-down"})
        return 0


def require_colab_vm() -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _remote_guard import require_colab

        require_colab("bridge_agent")
        return
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    if not (any(k.startswith("COLAB_") for k in os.environ) or Path("/content").exists()):
        print("bridge_agent: REFUSED — no Colab runtime marker", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    require_colab_vm()
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--config", default=str(KIT / "bridge.json"))
    args = parser.parse_args()
    if args.setup:
        return setup(Path(args.config))
    cfg = json.loads(Path(args.config).read_text())
    return Agent(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
