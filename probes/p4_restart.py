#!/usr/bin/env python3
"""P4: restart-survival verification.

Local leg (runs now, sandboxed): simulate a VM replacement — canonical
token/sessions deleted, PATH stripped — then prove both restore triggers
independently: (a) wrapper-triggered mirror restore, (b) the cron command
line alone. Conflict edge: a newer canonical file is never clobbered.
With --full, also prove bootstrap reinstalls the CLI into a bare sandbox
HOME from nothing.

Colab leg (staged, needs login): stop a session mid-job, start a fresh one,
resume under the same run-id, compare final hashes to an uninterrupted run.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _driver import record, REPO

MIRROR = REPO / "ops" / "mirror_transient.py"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def mirror(op: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, COLAB_RUNNER_HOME=str(home))
    return subprocess.run([sys.executable, str(MIRROR), op, "--json"], capture_output=True, text=True, env=env)


def main() -> int:
    full = "--full" in sys.argv
    legs: dict = {}
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        token = home / ".config" / "colab-cli" / "token.json"
        sessions = home / ".config" / "colab-cli" / "sessions.json"
        token.parent.mkdir(parents=True)
        token.write_text('{"refresh": "survival-v1"}')
        sessions.write_text('{"workload": {"id": "fake"}}')
        mirror("capture", home)
        mirror_token = REPO / "state" / "transient" / "colab-cli" / "token.json"
        mirror_sessions = REPO / "state" / "transient" / "colab-cli" / "sessions.json"
        saved = {p: (p.read_bytes() if p.exists() else None) for p in (mirror_token, mirror_sessions)}
        try:
            want = sha(token)
            # (a) wrapper trigger: canonical deleted -> mirror restore.
            token.unlink()
            sessions.unlink()
            mirror("restore", home)
            legs["wrapper_trigger_restore"] = token.exists() and sha(token) == want and oct(token.stat().st_mode)[-3:] == "600"
            # (b) cron trigger: the crontab command line alone, wrappers untouched.
            token.unlink()
            r = subprocess.run([sys.executable, str(MIRROR), "restore"], capture_output=True, text=True, env=dict(os.environ, COLAB_RUNNER_HOME=str(home)))
            legs["cron_trigger_restore"] = r.returncode == 0 and token.exists() and sha(token) == want
            # Conflict edge: newer canonical survives a restore.
            token.write_text('{"refresh": "newer"}')
            os.utime(token, (token.stat().st_atime, token.stat().st_mtime + 7200))
            mirror("restore", home)
            legs["newer_canonical_not_clobbered"] = token.read_text() == '{"refresh": "newer"}'
            if full:
                # VM-replacement shape per plan Step 9: PATH stripped and the
                # colab tool removed, but HOME (and the uv installer in it)
                # persists. Bootstrap must recreate the tool from its pin.
                bare = Path(td) / "bare-home"
                (bare / ".local" / "bin").mkdir(parents=True)
                real_uv = Path.home() / ".local" / "bin" / "uv"
                (bare / ".local" / "bin" / "uv").symlink_to(real_uv)
                env = {"HOME": str(bare), "PATH": "/usr/bin:/bin"}
                b = subprocess.run(["bash", str(REPO / "ops" / "bootstrap_local.sh")], capture_output=True, text=True, env=env, timeout=600)
                legs["bootstrap_recreates_tool"] = "version=" in b.stdout and "floor 0.7.1" in b.stdout
                legs["bootstrap_stdout_tail"] = (b.stdout.strip().split("\n")[-6:] if b.stdout else b.stderr.strip().split("\n")[-6:])
        finally:
            for p, content in saved.items():
                if content is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(content)
    legs["colab_leg"] = "STAGED — needs the operator's login: start the smoke job, `colab stop` mid-run, fresh session, `bin/colab-run smoke --resume <run-id>`, final hashes must equal the uninterrupted control (tests/test_runner_fake.py proves the same flow against the fake CLI)."
    passed = all(v for k, v in legs.items() if isinstance(v, bool))
    record("p4-restart-local", {"legs": legs, "local_leg_passed": passed})
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
