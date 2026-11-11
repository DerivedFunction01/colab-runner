"""Shared plumbing for probe drivers. Imported, not executed.

Every driver refuses to run before the operator's one-time OAuth login exists:
these probes burn compute units and must never trigger a login themselves.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The CLI funnel — resolution, the NO_PROXY environment, and the
# guarded invocation — is sync/cli.py's, the one implementation
# every consumer loads (consolidation Lane B step 5; this module
# used to carry its own unguarded copies). The names stay bound
# here because the founding probes import them from this module.
_cli_spec = importlib.util.spec_from_file_location("cli", REPO / "sync" / "cli.py")
cli = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(cli)

TOKEN = cli.TOKEN
colab_bin = cli.colab_bin
colab_env = cli.colab_env


def require_login(probe: str) -> None:
    if not TOKEN.exists():
        print(
            f"{probe}: STAGED — no colab login on this machine yet.\n"
            "One-time setup (the operator):\n"
            "  bash ops/bootstrap_local.sh\n"
            "  colab --auth oauth2 new -s <session> --high-mem   # open the printed URL, paste the code back\n"
            f"Then run: python3 probes/founding/{probe}.py",
            file=sys.stderr,
        )
        raise SystemExit(2)


def colab(*args: str, timeout: int = 18000, check: bool = False) -> subprocess.CompletedProcess:
    # The invocation stays this module's own (the gate wrapper
    # plus a direct subprocess.run through the module's
    # subprocess — the seam the driver tests patch); only the
    # resolution (colab_bin / colab_env, bound from sync/cli.py
    # above) is single-sourced. Delegating the call itself to
    # cli.colab would add the funnel's stored-token guard and
    # change the check=True failure shape — a behaviour change,
    # not a migration.
    _sync_gate(args)
    return subprocess.run(
        [colab_bin(), "--auth", "oauth2", *args],
        capture_output=True, text=True, timeout=timeout, check=check, env=colab_env(),
    )


# --- Synchronous claim gate (sync-channel design, option 3) -------------------
# The probes' session-targeted work calls (exec / install / upload /
# download) are the long synchronous consumers this module exists
# to run, so they pass the lease core's sync_rights verdict first —
# the same gate bin/colab-sh applies, with the same refusal shape
# (holder, evidence, expiry, exit 2). Short reads (list / status /
# usage), lifecycle (new / stop), and everything without a session
# target are not gated.
#
# Exemption, by construction rather than by flag: lease-internal
# and runner-internal calls never pass through this module — they
# dial through sync/cli.py's funnel and bin/colab-run — so the
# machinery's own calls are never gated and need no bypass.

_GATED_VERBS = ("exec", "install", "upload", "download")


def _gate_session(args) -> str | None:
    for i, a in enumerate(args):
        if a in ("-s", "--session") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--session="):
            return a.split("=", 1)[1]
    return None


def _sync_gate(args) -> None:
    if not args or args[0] not in _GATED_VERBS:
        return
    session = _gate_session(args)
    if not session:
        return
    import os

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from sync import lease

    env = os.environ.get(lease.CALLER_ENV)
    err = lease.caller_claim_error(REPO, env=env)
    if err:
        print(f"probes: {err}", file=sys.stderr)
        raise SystemExit(2)
    caller = lease.resolve_caller(REPO, env=env)
    verdict = lease.sync_rights(REPO, session, caller)
    if verdict.get("allowed"):
        return
    parts = []
    for item in verdict.get("evidence") or []:
        kind = item.get("kind")
        if kind == "claim":
            parts.append(f"claim claimed at {item.get('claimed_at')}")
        elif kind == "run":
            parts.append(f"run {item.get('run_id')} ({item.get('state')})")
        elif kind == "job":
            parts.append(f"job {item.get('run_id')} registered on the lease record")
        elif kind == "attach":
            parts.append(f"attach via {item.get('seam')} at {item.get('ts')}")
    print(
        f"probes: synchronous {args[0]} on session {session!r} refused — "
        f"blocked by {verdict.get('blocked_by')}: {verdict.get('holder')!r} "
        f"(evidence: {'; '.join(parts) or 'no detail'}; claim expiry: {verdict.get('expires_at')}). "
        "Run it detached (a colab-run job, or bin/colab-sh --detach) instead.",
        file=sys.stderr,
    )
    raise SystemExit(2)


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
# sync/lease/sessions.py's UNASSIGN_FALLBACK_SCRIPT.
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
