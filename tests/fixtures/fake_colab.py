#!/usr/bin/env python3
"""Fake `colab` CLI for offline tests of the runner and wrappers.

State lives under $FAKE_COLAB_STATE (default /tmp/fake-colab). Each session
gets a sandbox dir acting as the VM working directory: upload copies in,
exec runs the given file with python3 there, download copies out.
Implements: version, new, stop, sessions, status, usage, exec, run,
upload, download, install. Unknown flags are ignored.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ.get("FAKE_COLAB_STATE", "/tmp/fake-colab"))


def session_dir(name: str) -> Path:
    return STATE / "sessions" / name


def parse(argv: list[str]) -> tuple[str, dict, list[str]]:
    args = list(argv)
    if args[:1] == ["--auth"]:
        args = args[2:]
    cmd, opts, pos = args[0], {}, []
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("-s", "--timeout", "-f", "--gpu", "--tpu"):
            opts[a] = args[i + 1]
            i += 2
        elif a == "--":
            pos.extend(args[i + 1:])
            break
        elif a.startswith("-"):
            i += 1
        else:
            pos.append(a)
            i += 1
    return cmd, opts, pos


def main() -> int:
    cmd, opts, pos = parse(sys.argv[1:])
    name = opts.get("-s", "default")
    root = session_dir(name)
    if cmd == "version":
        print("google-colab-cli 0.7.5 (fake)")
    elif cmd == "new":
        root.mkdir(parents=True, exist_ok=True)
        (root / "ACTIVE").write_text("1")
        print(f"session {name} created (fake)")
    elif cmd == "stop":
        (root / "ACTIVE").unlink(missing_ok=True)
        print(f"session {name} stopped (fake)")
    elif cmd == "sessions":
        base = STATE / "sessions"
        active = [d.name for d in base.iterdir() if (d / "ACTIVE").exists()] if base.exists() else []
        print(json.dumps(active))
    elif cmd == "status":
        print(f"session {name}: {'active' if (root / 'ACTIVE').exists() else 'stopped'} (fake)")
    elif cmd == "usage":
        print("balance: 500 CU (fake)")
    elif cmd == "install":
        print(f"installed (fake): {' '.join(pos)}")
    elif cmd == "upload":
        src = Path(pos[0])
        root.mkdir(parents=True, exist_ok=True)
        remote = pos[1] if len(pos) > 1 else src.name
        dest = root / (remote[len("/content/"):] if remote.startswith("/content/") else remote)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"uploaded {src.name} (fake)")
    elif cmd == "download":
        remote = pos[0]
        if remote.startswith("/content/"):  # the sandbox root plays /content
            remote = remote[len("/content/"):]
        src, dest = root / remote, Path(pos[1])
        if not src.exists():
            print(f"fake download: no such remote file {pos[0]}", file=sys.stderr)
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"downloaded {pos[0]} (fake)")
    elif cmd in ("exec", "run"):
        f = opts.get("-f") or (pos[0] if pos else None)
        if not f:
            print("fake exec: no file", file=sys.stderr)
            return 1
        local_f = root / Path(f).name
        if not local_f.exists():
            shutil.copy2(f, local_f)
        extra = pos[1:] if cmd == "run" else []
        r = subprocess.run([sys.executable, str(local_f), *extra], cwd=root, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode
    else:
        print(f"fake colab: unimplemented {cmd}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
