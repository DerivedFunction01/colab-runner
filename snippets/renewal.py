#!/usr/bin/env python3
"""Renewal kit: the in-VM half of window-lease renewal (driver 1).

Installed into a renew-flagged lease's session by the lease core
(sync/lease.py's install_renewal_kit) alongside _remote_guard.py, the
window config (renewal.json), and the account token. Two modes:

  --setup   one-shot: place the staged token at its canonical path
            (mode 600), build the CLI venv — create --without-pip
            (the VM image's venv ensurepip defect), seed pip after,
            install the official CLI with --ignore-installed (the
            env_build.sh lessons) — then spawn the daemon detached
            and exit. Readiness is the ready.json marker; a failure
            lands in setup-error.json instead, and the reaper's
            driver 2 covers a kit that never comes up.
  (daemon)  sleep until the renewal threshold (session start + cap -
            margin), re-checking the window end as it goes; then mint
            the child session with the same shape via the in-VM CLI,
            install the kit in the child, and write the handover file
            /content/lease-handover.json (the child's binding entry +
            the window record) for the reaper to pull. Single-shot:
            after a handover — or a failure — the daemon exits.

Standard library only. Refuses to run off a Colab VM via the
_remote_guard pattern (the guard ships beside this file in the kit
dir; the same check is inlined as a fallback so a missing sibling can
never make the snippet runnable locally by accident). The token and
the sessions file are never printed or logged — the handover carries
the child's binding entry as data for the reaper's programmatic
merge, and the logs name sessions and stages only.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KIT = Path("/content/renewal-kit")
TOKEN_CANONICAL = Path.home() / ".config" / "colab-cli" / "token.json"
SESSIONS_FILE = Path.home() / ".config" / "colab-cli" / "sessions.json"
HANDOVER = Path("/content/lease-handover.json")
LOG = KIT / "renewal.log"
PID = KIT / "renewal.pid"


def log(msg: str) -> None:
    try:
        KIT.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass


def require_colab_vm() -> None:
    """The _remote_guard pattern: refuse off a Colab VM. Prefers the
    real guard module (shipped beside this file); falls back to the
    same check inlined when the sibling is missing."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _remote_guard import require_colab

        require_colab("renewal")
        return
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - sibling missing: same check, inlined
        pass
    if "--force-local" in sys.argv:
        return
    if not (any(k.startswith("COLAB_") for k in os.environ) or Path("/content").exists()):
        print("renewal: REFUSED — no Colab runtime marker (no COLAB_* env, no /content)", file=sys.stderr)
        raise SystemExit(2)


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cli_bin() -> Path:
    return KIT / "venv" / "bin" / "colab"


def cli(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return run([str(cli_bin()), "--auth", "oauth2", *args], timeout=timeout)


def place_token() -> None:
    staged = KIT / "token.json"
    if staged.exists():
        TOKEN_CANONICAL.parent.mkdir(parents=True, exist_ok=True)
        # Atomic placement (owner ruling, 2026-11-01): stage to a .tmp
        # sibling and rename over the live file — the token is never
        # written in place under its live name, so an interrupted
        # placement cannot destroy or expose a half-written credential.
        tmp = TOKEN_CANONICAL.with_name(TOKEN_CANONICAL.name + ".tmp")
        shutil.copyfile(staged, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKEN_CANONICAL)
        staged.unlink()


def build_venv() -> bool:
    """The CLI venv, per the measured image defects: create
    --without-pip, seed pip via ensurepip with get-pip.py as the
    fallback, then install with --ignore-installed so the venv owns
    its copy even with system site-packages inherited."""
    venv = KIT / "venv"
    vpy = venv / "bin" / "python"
    if cli_bin().exists():
        return True
    try:
        if not vpy.exists():
            run(["python3", "-m", "venv", "--without-pip", str(venv)], timeout=600)
            if not vpy.exists():
                return False
        if run([str(vpy), "-m", "pip", "--version"], timeout=120).returncode != 0:
            run([str(vpy), "-m", "ensurepip", "--upgrade"], timeout=600)
        if run([str(vpy), "-m", "pip", "--version"], timeout=120).returncode != 0:
            get_pip = KIT / "get-pip.py"
            urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", get_pip)
            run([str(vpy), str(get_pip)], timeout=600)
        r = run([str(vpy), "-m", "pip", "install", "--quiet", "--ignore-installed", "google-colab-cli"], timeout=900)
        return r.returncode == 0 and cli_bin().exists()
    except Exception as exc:  # noqa: BLE001
        log(f"venv build failed: {type(exc).__name__}")
        return False


def daemon_running() -> bool:
    try:
        os.kill(int(PID.read_text().strip()), 0)
        return True
    except Exception:  # noqa: BLE001 - no pidfile or stale pid: not running
        return False


def setup(config_path: Path) -> int:
    KIT.mkdir(parents=True, exist_ok=True)
    try:
        place_token()
        if not build_venv():
            (KIT / "setup-error.json").write_text(json.dumps({"stage": "venv", "ts": datetime.now(timezone.utc).isoformat()}))
            log("setup failed at the venv build")
            return 1
        if not daemon_running():
            env = dict(os.environ)
            env["COLAB_RELEASE_TAG"] = "renewal-kit"
            with (KIT / "daemon.out").open("a") as out:
                proc = subprocess.Popen(
                    [str(KIT / "venv" / "bin" / "python"), str(KIT / "renewal.py"), "--config", str(config_path)],
                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env,
                )
            PID.write_text(str(proc.pid))
        (KIT / "ready.json").write_text(json.dumps({"ok": True, "ts": datetime.now(timezone.utc).isoformat()}))
        log("setup complete; daemon running")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"setup failed: {type(exc).__name__}")
        return 1


def install_in_child(child: str, cfg: dict, minted_at: datetime) -> bool:
    """Upload the kit (token, this snippet, the guard, the child's
    config) into the child session and launch its --setup there, so
    the next renewal generation is also driver 1."""
    child_cfg = dict(cfg)
    child_cfg["session"] = child
    child_cfg["acquired_at"] = minted_at.isoformat()
    child_cfg_path = KIT / "child-renewal.json"
    child_cfg_path.write_text(json.dumps(child_cfg, indent=2))
    uploads = [
        (TOKEN_CANONICAL, f"{KIT}/token.json"),
        (KIT / "renewal.py", f"{KIT}/renewal.py"),
        (KIT / "_remote_guard.py", f"{KIT}/_remote_guard.py"),
        (child_cfg_path, f"{KIT}/renewal.json"),
    ]
    for src, dest in uploads:
        try:
            r = cli("upload", "-s", child, str(src), dest, timeout=120)
        except Exception:  # noqa: BLE001
            return False
        if r.returncode != 0:
            return False
    launcher = KIT / "launch-child-setup.py"
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        "env['COLAB_RELEASE_TAG'] = 'renewal-kit'\n"
        f"subprocess.Popen(['nohup', 'python3', '{KIT}/renewal.py', '--setup', '--config', '{KIT}/renewal.json'], stdout=open('{KIT}/setup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('child renewal kit setup launched')\n"
    )
    try:
        r = cli("exec", "-s", child, "--timeout", "120", "-f", str(launcher), timeout=300)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


def daemon(config_path: Path) -> int:
    cfg = json.loads(config_path.read_text())
    start = datetime.fromisoformat(cfg["acquired_at"])
    deadline = datetime.fromisoformat(cfg["deadline"])
    cap = float(cfg.get("cap_seconds", 12 * 3600))
    margin = float(cfg.get("margin_seconds", 1800))
    wake = start + timedelta(seconds=cap - margin)
    log(f"daemon up: session {cfg.get('session')} wake {wake.isoformat()} deadline {deadline.isoformat()}")
    while True:
        now = datetime.now(timezone.utc)
        if now >= deadline:
            log("window closed before the threshold; exiting without minting")
            return 0
        if now >= wake:
            break
        time.sleep(min(60.0, max(1.0, (wake - now).total_seconds())))
    minted_at = datetime.now(timezone.utc)
    child = f"{cfg['session']}-r{int(minted_at.timestamp())}"
    args = ["new", "-s", child]
    if cfg.get("high_mem"):
        args.append("--high-mem")
    if cfg.get("gpu"):
        args += ["--gpu", str(cfg["gpu"])]
    try:
        r = cli(*args, timeout=600)
    except Exception as exc:  # noqa: BLE001
        log(f"child mint failed: {type(exc).__name__}")
        return 1
    if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
        log("child mint failed: CLI error")
        return 1
    if not install_in_child(child, cfg, minted_at):
        # The handover is still written: the child is adoptable, and
        # the reaper's later kit install (driver-2 path) or the next
        # extension covers the missing in-child kit.
        log("kit install in the child failed; writing the handover anyway")
    try:
        entry = json.loads(SESSIONS_FILE.read_text())[child]
    except Exception:  # noqa: BLE001
        log("handover failed: the child binding is not readable")
        return 1
    HANDOVER.write_text(json.dumps({
        "child_session": child,
        "binding": {child: entry},
        "parent_session": cfg.get("session"),
        "shape": cfg.get("shape"),
        "deadline": cfg.get("deadline"),
        "minted_at": minted_at.isoformat(),
    }, indent=2))
    log(f"handover written: child {child}")
    return 0


def main() -> int:
    require_colab_vm()
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--config", default=str(KIT / "renewal.json"))
    args, _unknown = parser.parse_known_args()
    if args.setup:
        return setup(Path(args.config))
    return daemon(Path(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
