"""Harness bootstrap payload (connector plan, Step 5): install
the connector client in this session and prove one attributed
read through the ferry.

The job's uploads land beside this script's working directory:
the bin/harness client and the consumer's token file. Install:
the client goes to harness-client/harness, the token to
harness-client/token at 0600 — and the uploaded token copy is
shredded (zero-overwritten, then unlinked) so the token exists
in exactly one place in the session. The ferry dirs are
created; the smoke call (chat_list, a bridged read) runs with
the token read from its installed home into the subprocess
environment only — it is never written to any config file or
output. The smoke verdict lands in the run's outputs as
smoke-result.json; the payload exits with the smoke's verdict,
so the run record states the bootstrap's truth.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_BOOTSTRAP_ROOT") or "/content")
CLIENT_DIR = ROOT / "harness-client"
FERRY_OUT = ROOT / "harness-ferry" / "out"
FERRY_IN = ROOT / "harness-ferry" / "in"
UPLOAD_CLIENT = ROOT / "harness"
UPLOAD_TOKEN = ROOT / "sweeper-venue.token"
SMOKE_WAIT_S = "600"
FERRY_WAIT_S = "300"


def _shred(path: Path) -> None:
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(b"\0" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def install() -> dict:
    if not UPLOAD_CLIENT.is_file():
        return {"ok": False, "stage": "install", "error": "client upload missing"}
    if not UPLOAD_TOKEN.is_file():
        return {"ok": False, "stage": "install", "error": "token upload missing"}
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)
    FERRY_OUT.mkdir(parents=True, exist_ok=True)
    FERRY_IN.mkdir(parents=True, exist_ok=True)
    client = CLIENT_DIR / "harness"
    client.write_bytes(UPLOAD_CLIENT.read_bytes())
    client.chmod(0o755)
    token_dest = CLIENT_DIR / "token"
    token_dest.write_bytes(UPLOAD_TOKEN.read_bytes())
    token_dest.chmod(0o600)
    _shred(UPLOAD_TOKEN)
    (CLIENT_DIR / "env.json").write_text(json.dumps({
        "transport": "ferry",
        "ferry_out": str(FERRY_OUT),
        "ferry_in": str(FERRY_IN),
        "client": str(client),
        "token_file": str(token_dest),
    }, indent=2) + "\n")
    return {"ok": True}


def smoke() -> dict:
    token = (CLIENT_DIR / "token").read_text().strip()
    env = dict(os.environ)
    env.update({
        "HARNESS_TRANSPORT": "ferry",
        "HARNESS_FERRY_OUT": str(FERRY_OUT),
        "HARNESS_FERRY_IN": str(FERRY_IN),
        "HARNESS_FERRY_WAIT": FERRY_WAIT_S,
        "HARNESS_TOKEN": token,
    })
    proc = subprocess.run(
        [sys.executable, str(CLIENT_DIR / "harness"), "call", "chat_list", "{}", "--wait", SMOKE_WAIT_S],
        capture_output=True, text=True, env=env, timeout=int(SMOKE_WAIT_S) + int(FERRY_WAIT_S) + 120,
    )
    result = None
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = None
    chats = None
    if isinstance(result, dict):
        payload = result.get("result") if isinstance(result.get("result"), dict) else result
        if isinstance(payload, dict) and isinstance(payload.get("chats"), list):
            chats = len(payload["chats"])
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "chats": chats,
        "result": result,
        "stderr_tail": (proc.stderr or "")[-300:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    installed = install()
    if not installed.get("ok"):
        verdict = {"ok": False, "stage": installed.get("stage"), "error": installed.get("error")}
        (out_dir / "smoke-result.json").write_text(json.dumps(verdict, indent=2) + "\n")
        print(f"bootstrap failed at install: {installed.get('error')}", flush=True)
        return 1
    verdict = smoke()
    (out_dir / "smoke-result.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(f"bootstrap complete: smoke {'ok' if verdict['ok'] else 'failed'} "
          f"(chats: {verdict.get('chats')})", flush=True)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
