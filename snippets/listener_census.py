#!/usr/bin/env python3
"""Listener census probe: runs in-session, prints one JSON
object naming every listening TCP socket's port, owning pid,
cwd, start time, and command name.

The census exists because a leaked server (a gate's dev serve
whose job ended, parented to init, cwd deleted) is invisible
to every run record — it outlives the run that started it and
holds its port against the next tenant. Reading /proc only,
stdlib only; the proc root is overridable via
LISTENER_CENSUS_PROC for fixture tests. A process that
vanishes mid-scan is skipped, never fatal: the census is a
snapshot, not a transaction.
"""

import json
import os
from pathlib import Path

PROC = Path(os.environ.get("LISTENER_CENSUS_PROC", "/proc"))
LISTEN_STATE = "0A"


def _boot_time(proc: Path) -> float:
    for line in (proc / "stat").read_text().splitlines():
        if line.startswith("btime"):
            return float(line.split()[1])
    return 0.0


def _listen_inodes(proc: Path) -> dict:
    """socket inode -> local port, for listening TCP sockets."""
    inodes = {}
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != LISTEN_STATE:
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
            inodes[fields[9]] = port
    return inodes


def census(proc: Path = PROC) -> dict:
    inodes = _listen_inodes(proc)
    if not inodes:
        return {"listeners": []}
    clk = os.sysconf("SC_CLK_TCK")
    boot = _boot_time(proc)
    return {"listeners": _join(proc, inodes, clk, boot)}


def _join(proc: Path, inodes: dict, clk: int, boot: float) -> list:
    out = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        ports = []
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inode = target[len("socket:[") : -1]
                if inode in inodes:
                    ports.append(inodes[inode])
        if not ports:
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        try:
            cmd = (entry / "comm").read_text().strip()
        except OSError:
            cmd = ""
        started = None
        try:
            stat = (entry / "stat").read_text()
            start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
            started = boot + start_ticks / clk
        except (OSError, ValueError, IndexError):
            pass
        for port in sorted(set(ports)):
            out.append({"port": port, "pid": pid, "cwd": cwd, "cmd": cmd, "started_epoch": started})
    out.sort(key=lambda e: (e["port"], e["pid"]))
    return out


if __name__ == "__main__":
    print(json.dumps(census()))
