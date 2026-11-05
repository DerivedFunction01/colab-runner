#!/usr/bin/env python3
"""Bridge probe: the in-VM half of a standing-as-bridge probe
(2026-11-01). Runs detached inside the standing lease session and
tests the bridge topology end to end:

  this side plants command files in a queue directory (file
  channel) -> the watcher executes them — OS commands in the
  standing lease itself, or relayed into a child session it minted
  (in-VM CLI exec) -> every step appends a JSON line to
  bridge.log, which this side downloads and reads.

Command file (queue/<id>.json): {"id": ..., "target": "self" or a
session name, "cmd": "shell string"}. Processed files are renamed
to <id>.json.done. The watcher heartbeats into the same log,
records dispatch latency (command start minus the file's mtime),
and exits on the stop file or after --seconds.

The beachhead venv and canonical token come from the broker probe
(/content/broker-probe); the child mint reuses them. Nothing here
prints the token or any sessions-file contents; the log carries
command outputs (bounded tails) and timings only.

Standard library only. Refuses to run off a Colab VM.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/content/bridge")
QUEUE = ROOT / "queue"
LOG = ROOT / "bridge.log"
STOP = ROOT / "stop"
CLI = Path("/content/broker-probe/venv/bin/colab")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(obj: dict) -> None:
    obj = {"ts": now(), **obj}
    with LOG.open("a") as fh:
        fh.write(json.dumps(obj) + "\n")


def cli(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(CLI), "--auth", "oauth2", *args], capture_output=True, text=True, timeout=timeout)


def run_self(cmd: str) -> dict:
    t0 = time.monotonic()
    try:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=180)
        return {"rc": r.returncode, "out": r.stdout[-800:], "err": r.stderr[-200:], "seconds": round(time.monotonic() - t0, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"rc": None, "error": type(exc).__name__, "seconds": round(time.monotonic() - t0, 2)}


def run_child(child: str, cmd: str) -> dict:
    """Relay: write a runner that executes the command inside the
    child and prints a JSON verdict; run it through the in-VM CLI's
    exec into the child and parse the verdict back out."""
    runner = ROOT / "child_cmd.py"
    runner.write_text(
        "import json, subprocess\n"
        f"r = subprocess.run(['bash', '-lc', {cmd!r}], capture_output=True, text=True, timeout=170)\n"
        "print(json.dumps({'rc': r.returncode, 'out': r.stdout[-800:], 'err': r.stderr[-200:]}))\n"
    )
    t0 = time.monotonic()
    try:
        r = cli("exec", "-s", child, "--timeout", "180", "-f", str(runner), timeout=300)
    except Exception as exc:  # noqa: BLE001
        return {"rc": None, "error": type(exc).__name__, "seconds": round(time.monotonic() - t0, 2)}
    seconds = round(time.monotonic() - t0, 2)
    for line in reversed((r.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                verdict = json.loads(line)
                verdict["seconds"] = seconds
                verdict["relay_rc"] = r.returncode
                return verdict
            except json.JSONDecodeError:
                continue
    return {"rc": None, "error": "no-verdict", "relay_rc": r.returncode, "stdout_tail": (r.stdout or "")[-200:], "seconds": seconds}


def main() -> int:
    if not (any(k.startswith("COLAB_") for k in __import__("os").environ) or Path("/content").exists()):
        print("bridge_probe: REFUSED — no Colab runtime marker", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--mint", default="")
    parser.add_argument("--high-mem", action="store_true")
    parser.add_argument("--seconds", type=int, default=420)
    args = parser.parse_args()
    QUEUE.mkdir(parents=True, exist_ok=True)
    log({"kind": "watcher-up", "cli_ready": CLI.exists()})
    if args.mint:
        t0 = time.monotonic()
        cli_args = ["new", "-s", args.mint] + (["--high-mem"] if args.high_mem else [])
        try:
            r = cli(*cli_args, timeout=600)
            log({"kind": "mint", "name": args.mint, "rc": r.returncode, "seconds": round(time.monotonic() - t0, 1)})
        except Exception as exc:  # noqa: BLE001
            log({"kind": "mint", "name": args.mint, "error": type(exc).__name__})
    deadline = time.monotonic() + args.seconds
    last_beat = 0.0
    processed = 0
    while time.monotonic() < deadline and not STOP.exists():
        for path in sorted(QUEUE.glob("*.json")):
            try:
                spec = json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                path.rename(path.with_suffix(".bad"))
                continue
            started = time.monotonic()
            dispatch = round(started - (path.stat().st_mtime - time.time() + time.monotonic()), 2) if False else None
            # Dispatch latency in wall terms: file mtime vs now.
            dispatch_wall = round(time.time() - path.stat().st_mtime, 2)
            target = spec.get("target", "self")
            if target == "self":
                verdict = run_self(spec.get("cmd", ""))
            else:
                verdict = run_child(target, spec.get("cmd", ""))
            log({"kind": "result", "id": spec.get("id"), "target": target, "dispatch_seconds": dispatch_wall, **verdict})
            path.rename(path.with_suffix(".json.done"))
            processed += 1
        if time.monotonic() - last_beat >= 15:
            log({"kind": "heartbeat", "processed": processed})
            last_beat = time.monotonic()
        time.sleep(2)
    log({"kind": "watcher-down", "processed": processed})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
