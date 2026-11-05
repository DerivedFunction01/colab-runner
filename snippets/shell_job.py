#!/usr/bin/env python3
"""Run a shell script as a job — the payload is bash, not Python.

The shell program arrives as a heredoc on stdin (or via --script FILE):

    python3 shell_job.py --out DIR <<'EOF'
    ... shell commands ...
    EOF

It is written verbatim to <out>/job.sh and executed with bash as a
real process. This Python file is only the launcher/harness: it gives
shell work the runner's heartbeat contract — <out>/heartbeat.log
streams the script's output and ends with a line containing
"complete"; <out>/checkpoint.json carries completed_units (0 while
running, 1 at the end) plus the shell exit code. The launcher's own
exit code is the shell script's exit code.

Launch it detached (nohup, start_new_session) for long work, the same
pattern as every other job snippet; run it in the foreground for
short work. VM-side only in practice — it runs wherever it is started.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--script", help="shell script file (default: read stdin)")
    ap.add_argument("--cwd", default="/content", help="working directory for the script")
    args, payload_args = ap.parse_known_args()

    if args.script:
        script_path = Path(args.script)
        if not script_path.is_file() and script_path.is_absolute() and Path.cwd() != Path("/content"):
            # The runner addresses session files as /content/<name>;
            # under the fake CLI the session root is the working dir
            # instead (the same dual-world seam as the runner's
            # relative out dir), so fall back to the bare name there.
            # The discriminator is where this process runs, not
            # whether a /content happens to exist on the local
            # filesystem: on a real VM the cwd is /content, the
            # fallback never engages, and a genuinely missing file
            # refuses exactly as before.
            alt = Path(script_path.name)
            if alt.is_file():
                script_path = alt
        if not script_path.is_file():
            print(f"shell_job: --script file not found: {args.script} "
                  "(upload it to the VM first, e.g. /content/env_build.sh)", file=sys.stderr)
            return 2
        script = script_path.read_text()
    else:
        if sys.stdin.isatty():
            print("shell_job: no --script and stdin is a TTY (pipe a heredoc)", file=sys.stderr)
            return 2
        script = sys.stdin.read()
    if not script.strip():
        print("shell_job: empty shell script", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    job = out / "job.sh"
    job.write_text(script)
    ckpt = out / "checkpoint.json"
    ckpt.write_text(json.dumps({"completed_units": 0, "state": "running"}))

    cwd = Path(args.cwd)
    if Path.cwd() != Path("/content") and (cwd == Path("/content") or not cwd.is_dir()):
        # Same dual-world seam as the script path above, keyed the
        # same way: under the fake CLI the session root is the
        # working dir, not /content — a stray local /content
        # directory must not lure the job into running there while
        # its files live in the session root.
        cwd = Path.cwd()

    with open(out / "heartbeat.log", "a") as log:
        log.write(f"start shell_job cwd={cwd} t={time.time():.1f}\n")
        log.flush()
        env = dict(os.environ)
        env["SHELL_JOB_OUT"] = str(out)
        r = subprocess.run(["bash", str(job), *payload_args], cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=env)
        log.write(f"complete exit_code={r.returncode} t={time.time():.1f}\n")
        log.flush()
    ckpt.write_text(json.dumps({"completed_units": 1, "exit_code": r.returncode}))
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
