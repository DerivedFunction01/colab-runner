"""Records-vs-channel reconciliation evidence.

The local record set and the channel's session list are two
readings of one fleet, and when they disagree — a session
live on the channel with no lease record, or a live lease
record whose session the channel reads absent — the
disagreement itself is the diagnostic. Before this module it
surfaced only as a stderr note on whichever call happened to
notice (colab-sh's orphan advisory, the panel's census), and
the ember renewal swap (2026-11-11) was reconstructed after
the fact from exactly such fragments. A disagreement now
lands as a journal line (var/reconcile-journal.jsonl) with
the evidence: the session, both readings, the producing
surface, and the timestamp.

The journal is evidence, not state: appends are best-effort
(a failed append never breaks the read that noticed), and a
standing disagreement is journaled at most once per window
per reading-pair — the state reads that produce these lines
run on the panel's poll cadence, and a journal that repeats
the same line every poll buries the transitions it exists to
record. A changed reading re-journals at once.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import foundation

JOURNAL_WINDOW_S = 3600
# The dedup scan reads the journal's tail, not the whole
# file: a (session, kind) pair's last line inside the window
# is what the scan is for, and the sync-claims journal set
# the tail-read precedent.
_TAIL_BYTES = 256 * 1024


def reconcile_journal_path(repo: Path) -> Path:
    return foundation.var_dir(repo) / "reconcile-journal.jsonl"


def _entries(repo: Path) -> list[dict]:
    path = reconcile_journal_path(repo)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def journal_disagreement(repo: Path, session: str, kind: str, local_reading: str, channel_reading: str, source: str, now_dt: datetime | None = None) -> bool:
    """Journal one records-vs-channel disagreement.

    kind is "live-no-record" (the channel lists the session;
    no lease record exists) or "record-no-session" (a lease
    record stands; the channel reads the session absent).
    local_reading / channel_reading name the two readings in
    the producer's own vocabulary, source the producing
    surface ("state", "colab-sh"). Returns True when a line
    was appended: inside the window, the same (session, kind,
    readings) is not journaled twice."""
    now_dt = now_dt or datetime.now(timezone.utc)
    for entry in reversed(_entries(repo)):
        if entry.get("session") != session or entry.get("kind") != kind:
            continue
        if entry.get("local") == local_reading and entry.get("channel") == channel_reading:
            try:
                age = (now_dt - foundation.parse_dt(entry["ts"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = None
            if age is not None and 0 <= age < JOURNAL_WINDOW_S:
                return False
        break
    entry = {
        "ts": now_dt.isoformat(),
        "session": session,
        "kind": kind,
        "local": local_reading,
        "channel": channel_reading,
        "source": source,
    }
    try:
        path = reconcile_journal_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        return False
    return True
