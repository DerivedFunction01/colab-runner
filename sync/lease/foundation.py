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
