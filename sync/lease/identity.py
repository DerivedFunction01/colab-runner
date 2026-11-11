"""Runtime identity per live lease: which machine actually
sits behind a session name, stamped and re-checked.

A session's runtime can be replaced under its lease name — a
platform recycle, a renewal-adjacent swap — and before this
module nothing recorded which runtime a lease was talking
to: the cozy-pilot-61 recycle (2026-11-11) erased a running
job's entire /content footprint and the keepalive's silence
read as staleness and relaunched onto the new runtime, a
silent self-heal masking the loss; the ember renewal swap the
same day reproduced the signature from the other direction.
Identity is the countermeasure: a boot identity captured at
acquire/adopt, re-probed on the reap cycle, and a confirmed
change with work in flight surfaces as a runtime-replaced
event instead of healing silently.

The stamp lives in a side file (var/identity-<session>.json),
never on the lease record — the provisioning stamp's pattern,
for the same reason: lease records are whole-record
last-writer-wins saves, and an identity writer must not race
them. The served state merges the stamp at read time.

Identity is a boot identity: the runtime's
/proc/sys/kernel/random/boot_id where readable, else a
boot epoch (the probe's wall clock minus its uptime) — a
replacement runtime booted later, so its epoch postdates the
stamped one well beyond the tolerance. An unreadable probe is
unknown, never a verdict: no comparison runs on evidence that
could not be read, and nothing re-stamps on a guess.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import foundation, records, sessions

# The probe rides one kernel exec, bounded like the kernel
# pulse's: a wedged channel degrades the reading to unknown,
# never wedges the acquire or reap pass carrying it.
PROBE_TIMEOUT_S = 75
# A boot-epoch replacement verdict needs the new boot to
# postdate the stamped one by more than this: each reading's
# epoch is a local wall clock minus a remote uptime, with
# seconds of error between them — and a runtime that merely
# aged moves its epoch not at all.
BOOT_EPOCH_TOLERANCE_S = 120

_PROBE_SOURCE = (
    "import json\n"
    "from pathlib import Path\n"
    "boot_id = None\n"
    "try:\n"
    "    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None\n"
    "except OSError:\n"
    "    pass\n"
    "uptime_s = None\n"
    "try:\n"
    "    uptime_s = float(Path('/proc/uptime').read_text().split()[0])\n"
    "except (OSError, ValueError, IndexError):\n"
    "    pass\n"
    "print(json.dumps({'boot_id': boot_id, 'uptime_s': uptime_s}))\n"
)


def identity_stamp_path(repo: Path, session: str) -> Path:
    return foundation.var_dir(repo) / f"identity-{session}.json"


def read_stamp(repo: Path, session: str) -> dict:
    """The session's identity stamp, {} when none exists."""
    try:
        return json.loads(identity_stamp_path(repo, session).read_text())
    except (OSError, ValueError):
        return {}


def _write_stamp(repo: Path, session: str, stamp: dict) -> None:
    path = identity_stamp_path(repo, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamp))
    tmp.replace(path)


def _now_iso(now_dt: datetime | None) -> str:
    return (now_dt or datetime.now(timezone.utc)).isoformat()


def _boot_identity(ident: dict) -> bool:
    """Whether an identity reading carries a boot identity at
    all — a host-only reading can be stamped as contact
    evidence but can never ground a replacement verdict."""
    return bool(ident.get("boot_id")) or ident.get("boot_epoch") is not None


def probe_identity(colab, repo: Path, session: str) -> dict:
    """The session's current identity: {host, endpoint,
    boot_id, boot_epoch}, fields None where unreadable. The
    host/endpoint halves are the local binding's (no dial);
    the boot half is one bounded exec. Never raises."""
    ident = {"host": sessions.session_host(session), "endpoint": sessions.session_endpoint(session), "boot_id": None, "boot_epoch": None}
    try:
        payload = foundation.var_dir(repo) / "identity-probe.py"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text(_PROBE_SOURCE)
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(payload), timeout=PROBE_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return ident
    if getattr(r, "returncode", 1) != 0:
        return ident
    try:
        data = json.loads(r.stdout[r.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return ident
    if isinstance(data.get("boot_id"), str) and data["boot_id"]:
        ident["boot_id"] = data["boot_id"]
    uptime = data.get("uptime_s")
    if isinstance(uptime, (int, float)) and uptime >= 0:
        ident["boot_epoch"] = datetime.now(timezone.utc).timestamp() - float(uptime)
    return ident


def ensure_identity(colab, repo: Path, session: str, source: str = "acquire") -> dict:
    """Capture the session's identity at first registration
    (acquire's create, an adoption). A stamp that already
    carries a boot identity is left exactly as it is — capture
    is a birth act, and re-checking belongs to the reap cycle
    (check_identity). Returns {"identity": "captured" |
    "present" | "unreadable"} for the caller's result; never
    raises into the acquire it rides on."""
    if _boot_identity(read_stamp(repo, session)):
        return {"identity": "present"}
    ident = probe_identity(colab, repo, session)
    if not _boot_identity(ident):
        return {"identity": "unreadable"}
    _write_stamp(repo, session, {**ident, "session": session, "captured_at": _now_iso(None), "source": source})
    return {"identity": "captured"}


def _changed(stamp: dict, current: dict) -> bool | None:
    """The replacement verdict between a stamped identity and
    a fresh reading: True / False when a comparison ran, None
    when neither half could be compared (no verdict). boot_id
    is decisive where both readings carry it; the boot epoch
    is the fallback — a replacement's epoch postdates the
    stamped one by more than the tolerance, the same
    runtime's epoch does not move."""
    stamped_id, current_id = stamp.get("boot_id"), current.get("boot_id")
    if stamped_id and current_id:
        return stamped_id != current_id
    stamped_epoch, current_epoch = stamp.get("boot_epoch"), current.get("boot_epoch")
    if stamped_epoch is not None and current_epoch is not None:
        return (current_epoch - stamped_epoch) > BOOT_EPOCH_TOLERANCE_S
    return None


def _affected_work(repo: Path, rec: dict) -> tuple[list, list]:
    """The work a replacement puts at risk on this lease:
    (registered live jobs, non-terminal run records naming
    the session). The runs scan reaches runs whose
    registration was lost — their records are exactly the
    evidence a silent heal would bury."""
    jobs = [entry["id"] for entry in records.live_jobs(repo, rec)]
    runs = []
    runs_dir = repo / "results" / "runs"
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.json")):
            run = foundation.load_json(path, None)
            if not isinstance(run, dict) or run.get("session") != rec.get("session"):
                continue
            if run.get("state") in records.RUN_NONTERMINAL_STATES and run.get("run_id") not in jobs:
                runs.append(run.get("run_id"))
    return jobs, runs


def check_identity(colab, repo: Path, rec: dict, now_dt: datetime | None = None) -> dict:
    """One reap-cycle identity check for a live lease.

    - No usable stamp yet (the lease predates identity, or
      its capture read nothing): the fresh reading baselines
      silently — a birth, like a first host capture.
    - Unreadable probe, or no comparable half: "unknown", no
      verdict, no re-stamp.
    - Same runtime: "ok".
    - Replaced runtime: the stamp re-stamps with the new
      identity and the previous one preserved on it; when
      registered jobs or non-terminal run records name the
      session, a runtime-replaced event spools for the outbox
      (foundation.append_reaper_event — the lease core's
      drain pattern, the approval-needed host event's home)
      and the result carries the affected ids. A replacement
      with nothing in flight re-stamps quietly: nothing was
      lost, and there is nothing to surface."""
    session = rec["session"]
    stamp = read_stamp(repo, session)
    current = probe_identity(colab, repo, session)
    if not _boot_identity(current):
        return {"identity": "unknown"}
    if not _boot_identity(stamp):
        _write_stamp(repo, session, {**current, "session": session, "captured_at": _now_iso(now_dt), "source": "reconcile"})
        return {"identity": "baselined"}
    changed = _changed(stamp, current)
    if changed is None:
        return {"identity": "unknown"}
    if not changed:
        return {"identity": "ok"}
    jobs, runs = _affected_work(repo, rec)
    previous = {key: stamp.get(key) for key in ("boot_id", "boot_epoch", "host", "captured_at")}
    _write_stamp(repo, session, {**current, "session": session, "captured_at": _now_iso(now_dt), "source": "reconcile", "replaced_at": _now_iso(now_dt), "previous": previous})
    event = bool(jobs or runs)
    if event:
        foundation.append_reaper_event(repo, {
            "kind": "runtime-replaced",
            "session": session,
            "caller": rec.get("caller"),
            "at": _now_iso(now_dt),
            "previous": previous,
            "current": {key: current.get(key) for key in ("boot_id", "boot_epoch", "host")},
            "jobs": jobs,
            "runs": runs,
        })
    return {"identity": "replaced", "jobs": jobs, "runs": runs, "event": event}
