#!/usr/bin/env python3
"""The single Colab CLI funnel: resolution, environment, invocation.

Every automated call to the official google-colab-cli goes through
here. Resolution order (stated once, in AGENTS.md): $COLAB_BIN (the
test seam), else the uv tool venv
(~/.local/share/uv/tools/google-colab-cli/bin/colab), else PATH,
else ~/.local/bin/colab. The CLI is deliberately NOT installed on
PATH (no global shim): this funnel is the only sanctioned access,
and a bare `colab` in an agent shell is a contract breach, not a
shortcut. Every call passes --auth oauth2 explicitly and runs with
the NO_PROXY suffixes that keep Colab/Google traffic off this
machine's egress proxy.

The guarded colab() below is the auth guard of record (mechanics:
docs/auth.md). It moved here from bin/colab-run unchanged; the
runner and every other consumer load this module by path and call
the one implementation.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOKEN = Path.home() / ".config" / "colab-cli" / "token.json"


def colab_bin() -> str:
    env_bin = os.environ.get("COLAB_BIN")
    if env_bin:
        return env_bin
    venv_bin = Path.home() / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "colab"
    if venv_bin.exists():
        return str(venv_bin)
    return shutil.which("colab") or str(Path.home() / ".local" / "bin" / "colab")


def colab_env() -> dict:
    env = dict(os.environ)
    for key in ("NO_PROXY", "no_proxy"):
        suffix = "google.com,googleapis.com,googleusercontent.com,colab.dev"
        env[key] = f"{env[key]},{suffix}" if env.get(key) else suffix
    return env


AUTH_PROMPT = "Enter the authorization code"
AUTH_RC = 3
TOKEN_MIN_REFRESH_LEN = 20


def _var_dir() -> Path:
    # The lease core's var seam (sync/lease.py var_dir): the
    # COLAB_RUNNER_VAR override, else this repo's var/. Mirrored
    # here so the funnel carries no lease import; the auth-required
    # marker is the only var/ state the guard touches.
    return Path(os.environ.get("COLAB_RUNNER_VAR", REPO / "var"))


def _token_bytes() -> bytes | None:
    try:
        return TOKEN.read_bytes()
    except OSError:
        return None


def _token_valid(data: bytes | None) -> bool:
    """A stored credential is valid when it parses as JSON and carries
    a refresh_token of plausible length. The interactive flow's stub
    (a lone refresh_token key, 15 chars) fails this, as does any
    truncated or non-JSON write. Contents are never printed."""
    if not data:
        return False
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    rt = parsed.get("refresh_token")
    return isinstance(rt, str) and len(rt) >= TOKEN_MIN_REFRESH_LEN


def _atomic_write_token(data: bytes) -> None:
    """Write the token via a .tmp sibling + rename, so no reader ever
    sees a partial credential and an interrupted write cannot destroy
    the live file (owner ruling, 2026-11-01: token writes are atomic
    at every seam)."""
    tmp = TOKEN.with_name(TOKEN.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN)


def _note_auth_required(reason: str) -> None:
    try:
        var = _var_dir()
        var.mkdir(parents=True, exist_ok=True)
        noted_at = datetime.now(timezone.utc).isoformat()
        (var / "auth-required.json").write_text(json.dumps({"noted_at": noted_at, "reason": reason}, indent=2))
    except OSError:
        pass


def _clear_auth_required() -> None:
    try:
        (_var_dir() / "auth-required.json").unlink(missing_ok=True)
    except OSError:
        pass


def _auth_blocked(cmd: list[str], reason: str) -> subprocess.CompletedProcess:
    _note_auth_required(reason)
    return subprocess.CompletedProcess(args=cmd, returncode=AUTH_RC, stdout="", stderr=f"colab auth required: {reason}")


def colab(*args: str, timeout: int = 18000) -> subprocess.CompletedProcess:
    """The one funnel for every automated Colab CLI call, guarded so a
    dead credential can never again masquerade as data (2026-11-01: a
    failed token refresh dropped the CLI into its interactive login
    flow, which overwrote the stored token with a stub and printed the
    auth prompt where callers expected a session list / usage text —
    the prompt read as "session absent" and a live lease's record was
    deleted under it). The guard:
    - never invokes the CLI when the stored token is missing/invalid;
    - snapshots the token before the call and restores it atomically
      if the call leaves it invalid (the flow writes its stub BEFORE
      prompting, so this also covers a call that times out blocked on
      the prompt);
    - turns any call whose output carries the auth prompt into an
      auth-required result (rc 3, empty stdout), so downstream reads
      see "unknown", never junk data.
    Inert under COLAB_BIN: the test fakes are not credential flows."""
    cmd = [colab_bin(), "--auth", "oauth2", *args]
    if os.environ.get("COLAB_BIN"):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=colab_env())
    snapshot = _token_bytes()
    if not _token_valid(snapshot):
        return _auth_blocked(cmd, "stored token missing or invalid; re-authorization required (no CLI call made)")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=colab_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        if not _token_valid(_token_bytes()):
            _atomic_write_token(snapshot)
            _note_auth_required("CLI call timed out after overwriting the token; snapshot restored")
        raise
    if AUTH_PROMPT in (result.stdout + result.stderr):
        if not _token_valid(_token_bytes()):
            _atomic_write_token(snapshot)
        return _auth_blocked(cmd, "CLI entered its interactive login flow")
    if not _token_valid(_token_bytes()):
        _atomic_write_token(snapshot)
        return _auth_blocked(cmd, "CLI call overwrote the stored token; snapshot restored")
    if result.returncode == 0:
        _clear_auth_required()
    return result
