"""Session provisioning: the Chromium OS-library ensure.

A fresh Colab image carries no Chromium runtime libraries, so
every Playwright e2e leg on a new session fails at launch with
a missing shared library until someone provisions the session
by hand — and the provisioning dies with the session
(2026-11-11: 74/74 e2e legs dead at launch on a fresh image,
cured by `playwright install-deps chromium`). The ensure is
runner-owned and per-session-lifetime: at a session's first
registration (creation or first adoption), the acquire path
launches snippets/session_provision.py detached; the payload
checks the distro package set, installs what is missing, and
writes its verdict to /content/provision-verdict.json.

Verdicts live in a side file (var/provision-<session>.json),
never on the lease record itself: lease records are
whole-record last-writer-wins saves with known lost-update
races, and a detached poller stamping the record would race
the pulse's saves. The side file has exactly one writer at a
time (the launch stamps the attempt, the reconcile stamps the
verdict), and the served state merges it at read time — the
launch-health journal's pattern.

The ensure never blocks an acquire: the launch is bounded and
non-raising like the keepalive's, a stamped-ok session is
never re-provisioned, and a failed or stale attempt retries
on a later acquire (bounded by RETRY_AFTER_S).
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import foundation

PROVISION_UPLOAD_TIMEOUT_S = 30
PROVISION_LAUNCH_TIMEOUT_S = 90
VERDICT_READ_TIMEOUT_S = 30
# A failed attempt (or one whose verdict never landed) may be
# retried once this long has passed since the attempt stamp.
RETRY_AFTER_S = 1800
REMOTE_VERDICT = "/content/provision-verdict.json"


def _stamp_path(repo: Path, session: str) -> Path:
    return foundation.var_dir(repo) / f"provision-{session}.json"


def read_stamp(repo: Path, session: str) -> dict:
    """The session's provisioning stamp, {} when none exists."""
    try:
        return json.loads(_stamp_path(repo, session).read_text())
    except (OSError, ValueError):
        return {}


def _write_stamp(repo: Path, session: str, stamp: dict) -> None:
    path = _stamp_path(repo, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamp))
    tmp.replace(path)


def _parse_stamp(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def ensure_provisioning(colab, repo: Path, session: str) -> dict:
    """Launch the provisioning payload if this session needs it.

    Returns a small status dict for the acquire result:
    {"provisioning": "ok"} when a verdict already reads ok or
    installed, {"provisioning": "launched"} when this call
    fired the ensure, {"provisioning": "pending"} when an
    attempt is in flight inside its retry window, and
    {"provisioning": "launch-failed", ...} when the launch
    itself failed (the acquire proceeds regardless).
    """
    stamp = read_stamp(repo, session)
    if stamp.get("chromium") in ("ok", "installed"):
        return {"provisioning": stamp["chromium"]}
    now_ts = datetime.now(timezone.utc).timestamp()
    attempted = _parse_stamp(stamp.get("attempted_at"))
    if attempted is not None and now_ts - attempted < RETRY_AFTER_S:
        return {"provisioning": "pending"}
    try:
        colab("upload", "-s", session, str(repo / "snippets" / "session_provision.py"), "/content/session_provision.py", timeout=PROVISION_UPLOAD_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"provisioning": "launch-failed", "stage": "upload"}
    launcher = foundation.var_dir(repo) / "launch-provision.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "import subprocess\n"
        "subprocess.Popen(['nohup', 'python3', 'session_provision.py', '--out', 'provision-verdict.json'], stdout=open('provision.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True)\n"
        "print('provisioning launched')\n"
    )
    _write_stamp(repo, session, {"attempted_at": datetime.now(timezone.utc).isoformat(), "state": "launched"})
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=PROVISION_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"provisioning": "launch-failed", "stage": "launch"}
    if r.returncode != 0:
        return {"provisioning": "launch-failed", "stage": "launch"}
    return {"provisioning": "launched"}


def reconcile_provisioning(colab, repo: Path, session: str) -> dict:
    """Fold a landed verdict into the session's stamp.

    Called from read paths (the served state composes it): one
    bounded download when an attempt is outstanding and no
    verdict is stamped yet. Returns the current stamp.
    """
    stamp = read_stamp(repo, session)
    if not stamp or stamp.get("chromium") in ("ok", "installed"):
        return stamp
    dest = foundation.var_dir(repo) / f"provision-verdict-{session}.json"
    try:
        r = colab("download", "-s", session, REMOTE_VERDICT, str(dest), timeout=VERDICT_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return stamp
        verdict = json.loads(dest.read_text())
    except Exception:  # noqa: BLE001
        return stamp
    stamp = {**stamp, **verdict, "state": "verdict"}
    _write_stamp(repo, session, stamp)
    return stamp
