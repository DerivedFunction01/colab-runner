"""Shared plumbing for probe drivers. Imported, not executed.

Every driver refuses to run before the operator's one-time OAuth login exists:
these probes burn compute units and must never trigger a login themselves.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOKEN = Path.home() / ".config" / "colab-cli" / "token.json"


def colab_bin() -> str:
    import shutil

    return shutil.which("colab") or str(Path.home() / ".local" / "bin" / "colab")


def require_login(probe: str) -> None:
    if not TOKEN.exists():
        print(
            f"{probe}: STAGED — no colab login on this machine yet.\n"
            "One-time setup (the operator):\n"
            "  bash ops/bootstrap_local.sh\n"
            "  colab --auth oauth2 new -s <session> --high-mem   # open the printed URL, paste the code back\n"
            f"Then run: python3 probes/{probe}.py",
            file=sys.stderr,
        )
        raise SystemExit(2)


def colab_env() -> dict:
    import os

    env = dict(os.environ)
    for key in ("NO_PROXY", "no_proxy"):
        suffix = "google.com,googleapis.com,googleusercontent.com,colab.dev"
        env[key] = f"{env[key]},{suffix}" if env.get(key) else suffix
    return env


def colab(*args: str, timeout: int = 18000, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [colab_bin(), "--auth", "oauth2", *args],
        capture_output=True, text=True, timeout=timeout, check=check, env=colab_env(),
    )


def record(probe: str, data: dict) -> Path:
    data = {"probe": probe, "at": datetime.now(timezone.utc).isoformat(), **data}
    out = REPO / "probes" / "results" / f"{probe}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, default=str))
    print(json.dumps(data, indent=2, default=str))
    print(f"wrote {out}")
    return out


# The fallback's unassign replay, run under the CLI's own interpreter:
# the client's stock unassign POST carries no body and the tunnel
# endpoint answers 411 Length Required (diagnosed 2026-11-04), so the
# client's own two steps (GET for the XSRF token, then POST) are
# replayed with an explicit empty-JSON body attached. Same form as
# sync/lease.py's UNASSIGN_FALLBACK_SCRIPT.
_UNASSIGN_SCRIPT = """
import os
from urllib.parse import urljoin

from pydantic import BaseModel

from colab_cli.client import COLAB_XSRF_TOKEN_HEADER, TUN_ENDPOINT, GetUnassignRequest
from colab_cli.common import state

endpoint = os.environ["UNASSIGN_ENDPOINT"]
client = state.client
url = urljoin(client.colab_domain, f"{TUN_ENDPOINT}/unassign/{endpoint}")
resp = client._issue_request(url, schema=GetUnassignRequest)
headers = {COLAB_XSRF_TOKEN_HEADER["key"]: resp.token}
client._issue_request(url, method="POST", headers=headers, schema=BaseModel, data=b"{}")
"""


def stop_session(name: str) -> None:
    """Stop a session; never raise. `colab stop` hangs when the kernel is
    wedged (measured after the OOM probe: 120 s timeout, session leaked),
    because it first tries a graceful kernel shutdown. Fall back to a
    direct server-side unassign through the CLI's own library, which does
    not touch the kernel."""
    try:
        r = colab("stop", "-s", name, timeout=120)
        if r.returncode == 0:
            return
    except Exception:
        pass
    try:
        import json as _json

        sessions = _json.loads((Path.home() / ".config" / "colab-cli" / "sessions.json").read_text())
        endpoint = sessions[name]["endpoint"]
        venv_py = Path.home() / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "python"
        env = colab_env()
        env["UNASSIGN_ENDPOINT"] = endpoint
        subprocess.run(
            [str(venv_py), "-c", _UNASSIGN_SCRIPT],
            capture_output=True, text=True, timeout=120, env=env,
        )
    except Exception:
        pass


def upload(session: str, local, timeout: int = 600) -> subprocess.CompletedProcess:
    """colab upload requires BOTH local and remote paths, and a relative
    remote path lands at the VM root — always /content-absolute (measured)."""
    from pathlib import Path as _P

    return colab("upload", "-s", session, str(local), f"/content/{_P(local).name}", timeout=timeout)


def download(session: str, remote: str, local, timeout: int = 600) -> subprocess.CompletedProcess:
    if not remote.startswith("/"):
        remote = f"/content/{remote}"
    return colab("download", "-s", session, remote, str(local), timeout=timeout)
