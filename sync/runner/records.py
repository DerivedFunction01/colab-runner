#!/usr/bin/env python3
"""Run records: persistence, terminal states, the outbox event.

A run's record (results/runs/<run_id>.json) is the runner's
state of record; this leaf owns writing it, reading it back,
and the exactly-one run-terminal event a terminal transition
appends to the UI outbox.
"""

import json
import os
from pathlib import Path

from .foundation import REPO, RUNS, now


def save_record(rec: dict) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{rec['run_id']}.json").write_text(json.dumps(rec, indent=2))


TERMINAL_STATES = ("complete", "failed", "orphaned")


def load_run_record(run_id: str) -> dict | None:
    try:
        rec = json.loads((RUNS / f"{run_id}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def outbox_path() -> Path:
    # The UI outbox lives in the runner var dir (the panel's own
    # var_dir() convention: $COLAB_RUNNER_VAR, else the repo's var).
    return Path(os.environ.get("COLAB_RUNNER_VAR", REPO / "var")) / "ui-outbox.jsonl"


def emit_terminal_event(rec: dict) -> bool:
    """Append the run's one run-terminal event to the UI outbox.

    The lease core already appends its own kinds there (the
    lease-extended chain the adoption census reads is the
    precedent); a run's terminal transition is the runner's kind.
    Guarded on the record: terminal_event_state stamps the state
    the event announced, so a second terminal observation of the
    same run (a --poll after the foreground poller finished, a
    sweep after a finalize) never double-announces — while a
    resumed run, whose resume clears the stamp, announces its new
    terminal state afresh. Best-effort: a failed append stamps
    nothing, so a later transition may retry it. Returns True
    when it appended."""
    if rec.get("terminal_event_state") == rec.get("state"):
        return False
    path = outbox_path()
    seq = 0
    try:
        lines = path.read_text().splitlines()
        for line in reversed(lines):
            if line.strip():
                try:
                    seq = int(json.loads(line).get("seq", 0))
                except (ValueError, json.JSONDecodeError):
                    seq = len(lines)
                break
    except OSError:
        seq = 0
    event = {
        "seq": seq + 1,
        "ts": now(),
        "kind": "run-terminal",
        "detail": {"run_id": rec["run_id"], "job": rec.get("job"), "caller": rec.get("caller"), "state": rec["state"], "session": rec.get("session")},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        return False
    rec["terminal_event_state"] = rec["state"]
    rec["terminal_event_at"] = event["ts"]
    return True


def mark_terminal(rec: dict, state: str, **fields) -> None:
    """Move a launched run to a terminal state: record update,
    save, and its exactly-one run-terminal outbox event."""
    rec.update(state=state, **fields)
    save_record(rec)
    if emit_terminal_event(rec):
        save_record(rec)
