#!/usr/bin/env python3
"""Chunked file transfer over the colab CLI's file channel.

Measured constraints (2026-10-29, live):
- `colab upload` lands files at the VM root unless the remote path is
  absolute — always use /content-absolute remote paths.
- Single-file uploads succeed at 50 MB and fail immediately at 75 MB
  (SSLEOFError from the runtime proxy, proxy-independent): the contents
  API has a size ceiling between those values. Transfers chunk at 32 MB
  and reassemble on the far side; integrity is by SHA-256 end to end.
- Tunnel throughput is ~1.6 MB/s each way.

Reliability (2026-10-31): the P2 1 GB upload died at part 12 of 32 on a
contents-API drop and the whole transfer was lost. Parts are now
retried individually (PART_ATTEMPTS attempts, linear backoff) and both
directions resume: before moving a part, the far side's copy is
verified by per-part SHA-256 and skipped when it already matches, so
re-running a failed transfer moves only the missing or corrupt parts.
The end-to-end SHA-256 still gates the assembled file.

bin/colab-xfer is the verb over this library: it drives the two
functions with a progress callback and keeps a pollable transfer
record per move.
"""

import hashlib
import importlib.util as _ilu
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CHUNK = 32 * 1024 * 1024
PART_ATTEMPTS = 3
PART_BACKOFF_S = 5
REPO = Path(__file__).resolve().parent.parent

# The CLI funnel — resolution ($COLAB_BIN, else the uv tool venv,
# else PATH, else ~/.local/bin/colab), the NO_PROXY environment,
# and the guarded invocation — is sync/cli.py's, the one
# implementation every consumer loads (consolidation Lane B
# step 5; this module used to carry its own unguarded copies).
# The names stay bound on this module because its callers and
# tests address them here — and because this module's internals
# call the bare name, so a test's fake binds exactly here.
_cli_spec = _ilu.spec_from_file_location("cli", Path(__file__).resolve().parent / "cli.py")
cli = _ilu.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(cli)

colab = cli.colab
colab_bin = cli.colab_bin
colab_env = cli.colab_env

# Launch-health journal (written by the runner's launch path,
# sync/lease/health.py owns the shape): the trailing window and
# failure cluster that mark a session degraded.
HEALTH_WINDOW_MINUTES = 30
HEALTH_FAILURE_CLUSTER = 2


def _session_degraded(session: str) -> bool:
    """Whether the launch-health journal marks this session
    degraded right now, under the lease health module's rule
    (most recent outcome in the trailing window a failure, or a
    failure cluster since the last success). The journal is
    evidence, not state: a missing or unreadable journal reads
    as not degraded. Mirrored here rather than imported — the
    transfer layer sits below the lease package."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    try:
        lines = (REPO / "var" / "launch-health.jsonl").read_text().splitlines()
    except OSError:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=HEALTH_WINDOW_MINUTES)
    last_ok = None
    last_fail = None
    fails_since_ok = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
            if entry.get("session") != session:
                continue
            at = datetime.fromisoformat(entry["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if at < cutoff:
            continue
        if entry.get("ok"):
            last_ok = entry["ts"]
            fails_since_ok = 0
        else:
            last_fail = entry["ts"]
            fails_since_ok += 1
    return bool(
        (last_fail and (last_ok is None or last_fail > last_ok))
        or fails_since_ok >= HEALTH_FAILURE_CLUSTER
    )


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _exec_file(session: str, code: str, timeout: int = 600) -> subprocess.CompletedProcess:
    # Unique staging name per call: a fixed name made concurrent
    # transfers clobber each other's scripts (and made the fake
    # CLI serve the first script staged under that name for every
    # later call — the 2026-11-05 chunked-upload proof failure).
    f = REPO / "var" / f"transfer-exec-{os.getpid()}-{time.time_ns()}.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(code)
    return colab("exec", "-s", session, "--timeout", str(timeout), "-f", str(f), timeout=timeout + 120)


def _attempt(fn) -> tuple:
    """Run fn (returning a CompletedProcess) up to PART_ATTEMPTS times.

    A non-zero return code or a subprocess timeout counts as a failed
    attempt. Returns (process, attempts_used); process is None when
    every attempt timed out.
    """
    proc = None
    for i in range(1, PART_ATTEMPTS + 1):
        try:
            proc = fn()
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            return proc, i
        if i < PART_ATTEMPTS and PART_BACKOFF_S:
            time.sleep(PART_BACKOFF_S * i)
    return proc, PART_ATTEMPTS


def _xfer_dir(digest: str) -> str:
    """The per-transfer staging directory for one payload,
    derived from its whole-file SHA-256: .xfer-<digest[:12]>.
    Parts live under it, never flat beside the target — residue
    from a completed transfer must be unable to poison a
    consumer globbing the target's directory (2026-11-11: xfer
    .part-000 chunks beside a bundle were swallowed by a job's
    part-* reassembly glob and doubled it). The derivation is
    content-keyed so both directions and any re-run agree on
    the directory, which keeps resume working."""
    return f".xfer-{digest[:12]}"


def _cleanup_parts(session: str, target: str, staging_dir: str) -> None:
    """Remove a completed transfer's staging directory and any
    legacy flat parts for the same target. Best-effort: the
    transfer has already verified end to end when this runs,
    and a cleanup failure must not fail it — the residue it
    leaves is inert under the staging directory (a failed
    transfer keeps its parts: they are its resume state)."""
    try:
        _exec_file(session, (
            "# transfer:cleanup\n"
            "import glob, os, shutil\n"
            "BASE = '/content' if os.getcwd() == '/content' else os.getcwd()\n"
            f"TARGET = {json.dumps(target)}\n"
            f"DIR = {json.dumps(staging_dir)}\n"
            "shutil.rmtree(os.path.join(BASE, DIR), ignore_errors=True)\n"
            "for pat in (TARGET + '.part-*', TARGET + '.dl-*'):\n"
            "    for p in glob.glob(os.path.join(BASE, pat)):\n"
            "        os.unlink(p)\n"
            "print('{}')\n"
        ), timeout=240)
    except Exception:
        pass


def _remote_part_hashes(session: str, names: list, mkdir_dir: str = "") -> dict:
    """SHA-256 of each named part already in /content ({} on any failure).

    A failed probe is not fatal: the caller just uploads everything,
    and the end-to-end hash still gates the result. When
    mkdir_dir is given, the script creates that staging
    directory first — uploads into a directory that does not
    exist fail on the far side.
    """
    names_json = json.dumps(names)
    # The generated scripts address session files through BASE:
    # /content on a real VM (where the exec's cwd is /content),
    # the working dir under the fake CLI (whose session root is
    # the cwd) — the same dual-world seam the runner and shell_job
    # use. The upload/download commands keep their /content-
    # absolute remote paths; that is the CLI's own contract.
    # The probe is bounded tighter than the phase execs below
    # and its timeout is caught: it is a resume optimization,
    # and a stall here must read as "no resume information"
    # (upload everything), never as twelve silent minutes in
    # front of a transfer that has not started (2026-11-11:
    # xfer verify-phase stalls on a saturated kernel).
    try:
        r = _exec_file(session, (
            "# transfer:part-hashes\n"
            "import hashlib, json, os\n"
            "BASE = '/content' if os.getcwd() == '/content' else os.getcwd()\n"
            f"MKDIR = {json.dumps(mkdir_dir)}\n"
            "if MKDIR:\n"
            "    os.makedirs(os.path.join(BASE, MKDIR), exist_ok=True)\n"
            f"NAMES = {names_json}\n"
            "out = {}\n"
            "for name in NAMES:\n"
            "    p = os.path.join(BASE, name)\n"
            "    if os.path.exists(p):\n"
            "        h = hashlib.sha256()\n"
            "        with open(p, 'rb') as f:\n"
            "            while c := f.read(1024*1024):\n"
            "                h.update(c)\n"
            "        out[name] = h.hexdigest()\n"
            "print(json.dumps(out))\n"
        ), timeout=240)
    except subprocess.TimeoutExpired:
        return {}
    try:
        return json.loads(r.stdout[r.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return {}


def chunked_upload(session: str, local: Path, remote_name: str, progress=None) -> dict:
    """Upload local to /content/<remote_name> in 32 MB parts; verify hash.

    Parts already on the VM with a matching SHA-256 are skipped
    (resume); each remaining part is retried up to PART_ATTEMPTS times.
    When progress is given it is called with {"parts", "parts_done",
    "resumed_parts", "moved_parts"} once the part count is known and
    again after every part lands or is skipped — the transfer
    record bin/colab-xfer keeps is written from these calls.
    """
    local = Path(local)
    digest = sha256_file(local)
    staging_dir = _xfer_dir(digest)
    metas = []
    with open(local, "rb") as fh:
        i = 0
        while chunk := fh.read(CHUNK):
            part = REPO / "var" / "chunks" / f"{remote_name}.part-{i:03d}"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(chunk)
            metas.append({"name": f"{staging_dir}/{part.name}", "path": part, "sha256": sha256_bytes(chunk)})
            i += 1

    def emit(resumed: int, moved: int) -> None:
        if progress is not None:
            progress({"parts": len(metas), "parts_done": resumed + moved,
                      "resumed_parts": resumed, "moved_parts": moved})

    # The part-hash probe is resume detection only — an
    # optimization. On a session the launch journal already
    # reads degraded, the probe contends with the part uploads
    # for the same worst channel and its bounded stall is paid
    # serially before any byte moves (the 2026-11-11 file-cap
    # specimen: the verify leg timed out twice against
    # byte-identical parts). Skip it there; every part uploads
    # and the end-to-end hash still verifies.
    probe_skipped = _session_degraded(session)
    remote_hashes = {} if probe_skipped else _remote_part_hashes(session, [m["name"] for m in metas], mkdir_dir=staging_dir)
    resumed = uploaded = retried = 0
    emit(0, 0)
    for i, m in enumerate(metas):
        if remote_hashes.get(m["name"]) == m["sha256"]:
            resumed += 1
            emit(resumed, uploaded)
            continue
        r, used = _attempt(lambda m=m: colab("upload", "-s", session, str(m["path"]), f"/content/{m['name']}", timeout=3600))
        if r is None or r.returncode != 0:
            stderr = "" if r is None else (r.stdout + r.stderr)[-300:]
            return {"ok": False, "stage": f"upload part {i}", "attempts": used,
                    "resumed_parts": resumed, "uploaded_parts": uploaded, "stderr": stderr}
        uploaded += 1
        if used > 1:
            retried += 1
        emit(resumed, uploaded)
    names = json.dumps([m["name"] for m in metas])
    # The verify phase (reassemble + end-to-end hash) rides the
    # same attempt discipline as the parts: it is idempotent,
    # and until it did, a stall here was one silent 720 s bound
    # ending in an uncaught timeout or a stage-less failure —
    # the caller could not tell a wedged verify from a slow
    # transfer (2026-11-11).
    r, _used = _attempt(lambda: _exec_file(session, (
        "# transfer:reassemble\n"
        "import hashlib, json, os\n"
        "BASE = '/content' if os.getcwd() == '/content' else os.getcwd()\n"
        f"parts = {names}\n"
        f"out = open(os.path.join(BASE, '{remote_name}'), 'wb')\n"
        "h = hashlib.sha256()\n"
        "for name in parts:\n"
        "    with open(os.path.join(BASE, name), 'rb') as f:\n"
        "        while c := f.read(1024*1024):\n"
        "            out.write(c); h.update(c)\n"
        "out.close()\n"
        "print(json.dumps({'sha256': h.hexdigest()}))\n"
    )))
    got = None
    if r is not None:
        try:
            got = json.loads(r.stdout[r.stdout.index("{"):])["sha256"]
        except (ValueError, json.JSONDecodeError, KeyError):
            pass
    if got != digest:
        return {"ok": False, "stage": "verify (reassemble)", "sha256": digest, "remote_sha256": got,
                "parts": len(metas), "resumed_parts": resumed,
                "uploaded_parts": uploaded, "retried_parts": retried,
                "stderr": "" if r is None else (r.stderr or "")[-300:]}
    # Verified end to end: the parts have done their work.
    # Remove them (and any legacy flat residue for this target)
    # so the target's directory holds only the target.
    _cleanup_parts(session, remote_name, staging_dir)
    return {"ok": True, "sha256": digest, "remote_sha256": got,
            "parts": len(metas), "resumed_parts": resumed,
            "uploaded_parts": uploaded, "retried_parts": retried,
            "probe_skipped": probe_skipped}


def chunked_download(session: str, remote_name: str, dest: Path, progress=None) -> dict:
    """Download /content/<remote_name> in 32 MB parts; verify hash locally.

    The remote split reports a SHA-256 per part and for the whole file.
    A local part copy whose hash already matches is skipped (resume);
    each remaining part is retried up to PART_ATTEMPTS times, and a
    downloaded part whose hash mismatches counts as a failed attempt.
    When progress is given it is called with {"parts", "parts_done",
    "resumed_parts", "moved_parts"} once the remote split reports and
    again after every part lands or is skipped.
    """
    # The split rides the attempt discipline too (idempotent:
    # it rewrites the same part files): a stall names its phase
    # instead of raising through the caller.
    r, _used = _attempt(lambda: _exec_file(session, (
        "# transfer:split\n"
        "import hashlib, json, os\n"
        "BASE = '/content' if os.getcwd() == '/content' else os.getcwd()\n"
        f"REMOTE = {json.dumps(remote_name)}\n"
        "path = os.path.join(BASE, REMOTE)\n"
        "size = os.path.getsize(path)\n"
        f"CH = {CHUNK}\n"
        "whole = hashlib.sha256()\n"
        "with open(path, 'rb') as f:\n"
        "    while c := f.read(1024*1024):\n"
        "        whole.update(c)\n"
        "digest = whole.hexdigest()\n"
        "DIR = '.xfer-' + digest[:12]\n"
        "os.makedirs(os.path.join(BASE, DIR), exist_ok=True)\n"
        "parts = []\n"
        "with open(path, 'rb') as f:\n"
        "    i = 0\n"
        "    while c := f.read(CH):\n"
        "        name = DIR + '/' + REMOTE + '.dl-' + format(i, '03d')\n"
        "        dest = os.path.join(BASE, name)\n"
        "        os.makedirs(os.path.dirname(dest), exist_ok=True)\n"
        "        open(dest, 'wb').write(c)\n"
        "        parts.append({'name': name, 'size': len(c), 'sha256': hashlib.sha256(c).hexdigest()})\n"
        "        i += 1\n"
        "print(json.dumps({'size': size, 'sha256': digest, 'dir': DIR, 'parts': parts}))\n"
    )))
    if r is None:
        return {"ok": False, "stage": "remote split", "stdout": "", "stderr": "remote split exec timed out on every attempt"}
    try:
        info = json.loads(r.stdout[r.stdout.index("{"):])
        parts = info["parts"]
    except (ValueError, json.JSONDecodeError, KeyError, TypeError):
        return {"ok": False, "stage": "remote split", "stdout": r.stdout[-300:], "stderr": r.stderr[-300:]}
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resumed = downloaded = retried = 0

    def emit() -> None:
        if progress is not None:
            progress({"parts": len(parts), "parts_done": resumed + downloaded,
                      "resumed_parts": resumed, "moved_parts": downloaded})

    emit()
    local_parts = []
    for meta in parts:
        name, want_sha = meta["name"], meta["sha256"]
        tmp = REPO / "var" / "chunks" / Path(name).name
        tmp.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists() and tmp.stat().st_size == meta["size"] and sha256_file(tmp) == want_sha:
            resumed += 1
            local_parts.append(tmp)
            emit()
            continue

        def fetch(tmp=tmp, name=name, want_sha=want_sha):
            d = colab("download", "-s", session, f"/content/{name}", str(tmp), timeout=3600)
            if d.returncode == 0 and not (tmp.exists() and sha256_file(tmp) == want_sha):
                # Channel reported success but the bytes are wrong:
                # count it as a failed attempt so it is retried.
                return subprocess.CompletedProcess(d.args, 1, d.stdout, d.stderr)
            return d

        d, used = _attempt(fetch)
        if d is None or d.returncode != 0:
            stderr = "" if d is None else (d.stdout + d.stderr)[-300:]
            return {"ok": False, "stage": f"download {name}", "attempts": used,
                    "resumed_parts": resumed, "downloaded_parts": downloaded, "stderr": stderr}
        downloaded += 1
        if used > 1:
            retried += 1
        local_parts.append(tmp)
        emit()
    h = hashlib.sha256()
    with open(dest, "wb") as out:
        for tmp in local_parts:
            data = tmp.read_bytes()
            out.write(data)
            h.update(data)
    ok = h.hexdigest() == info["sha256"]
    if ok:
        # Verified end to end: remove the split's staging
        # directory (and any legacy flat residue for this
        # target). A failed assembly keeps its remote parts —
        # they are the resume state for the next attempt.
        _cleanup_parts(session, remote_name, info.get("dir") or _xfer_dir(info["sha256"]))
    return {"ok": ok, "bytes": dest.stat().st_size,
            "sha256": h.hexdigest(), "remote_sha256": info["sha256"],
            "parts": len(parts), "resumed_parts": resumed,
            "downloaded_parts": downloaded, "retried_parts": retried}


if __name__ == "__main__":
    print("library module; import chunked_upload / chunked_download", file=sys.stderr)
    raise SystemExit(2)
