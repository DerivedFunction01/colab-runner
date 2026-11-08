import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

def var_dir(repo: Path) -> Path:
    return Path(os.environ.get("COLAB_RUNNER_VAR", repo / "var"))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


# --- Dial caches (E14) -------------------------------------------------------
# A state read should not re-dial the account for a value it
# read seconds ago. A cache file in var/ beside the liveness
# files: a {fetched_at, value} stamp; a missing, unreadable,
# or stale cache falls back to the dial; only a successful
# dial refreshes the stamp (a failed dial is never cached, so
# recovery is never masked). One home for the mechanism —
# state.py's portions and access.access_list share it, and
# the live-list cache file itself.

DIAL_CACHE_TTL_S = 30


def dial_cache_read(repo: Path, name: str, now_dt: datetime):
    """One dial's cached value, or None when the cache is
    missing, unreadable, or older than the TTL. A cached
    value is never itself None (failures are not cached), so
    None unambiguously means: dial."""
    data = load_json(var_dir(repo) / name, None)
    if not isinstance(data, dict) or "value" not in data:
        return None
    try:
        fetched = parse_dt(data.get("fetched_at"))
    except (TypeError, ValueError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = (now_dt - fetched).total_seconds()
    if age < 0 or age > DIAL_CACHE_TTL_S:
        return None
    return data["value"]


def dial_cache_write(repo: Path, name: str, value, now_dt: datetime) -> None:
    """Refresh one dial cache (staged sibling + rename, the
    record discipline). Best-effort by design: a failed cache
    write costs a re-dial on the next read, never a failed
    read."""
    p = var_dir(repo) / name
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
        try:
            tmp.write_text(json.dumps({"fetched_at": now_dt.isoformat(), "value": value}))
            os.replace(tmp, p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        pass


def load_config(repo: Path) -> dict:
    return load_json(repo / "config" / "lease.json", {"enabled": False})


# --- Session shape gate -------------------------------------------------------
# Local why: a bare panel tag is not a bypass — the bypass needs the
# server-carried env claim too — and the gate is admission only,
# never eviction. It is a journaled guardrail at the same trust
# level, not a security boundary. Mechanics:
# docs/lease-workflow.md.

ACTOR_ENV_CLAIM = "COLAB_LEASE_ACTOR"
PANEL_ACTOR = "panel"
GATE_DEFAULT_AGENT_CLASSES = ("high_ram",)
GATE_DEFAULT_BYPASS_ACTORS = (PANEL_ACTOR,)
GATE_DEFAULT_INFRA_ACTOR = "standing"


def parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def parse_balance(usage_text: str) -> float | None:
    m = re.search(r"balance:?\s*([0-9]+(?:\.[0-9]+)?)", usage_text, re.IGNORECASE)
    return float(m.group(1)) if m else None


# --- Attributed spend accounting ----------------------------------------------
# Local why: the balance carries no per-source breakdown, so spend
# is built only from this repo's own lease segments; a late-detected
# death overcounts by at most the detection lag, and an unconfirmed
# rate contributes nothing. Mechanics: docs/lease-workflow.md.


def spend_journal_path(repo: Path) -> Path:
    return var_dir(repo) / "lease-spend-journal.jsonl"


def reaper_events_path(repo: Path) -> Path:
    return var_dir(repo) / "reaper-events.jsonl"


def append_reaper_event(repo: Path, event: dict) -> bool:
    """Spool a core-side lifecycle event for the companion server to
    drain into the UI outbox. The outbox has exactly one writer (the
    server, which owns seq allocation), so reap() never writes it
    directly. Best-effort: a failed spool write never breaks reap —
    the reap result itself still carries the outcome."""
    try:
        p = reaper_events_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        return True
    except OSError:
        return False
