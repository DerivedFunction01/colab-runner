#!/usr/bin/env python3
"""The job runner: sync in, execute detached, poll, pull results.

bin/colab-run is the thin entry point; its body lives in this
package, split by seam (consolidation Wave 2, Lane B5 — the code
moved verbatim, no behaviour change):

- foundation: repo roots, the CLI funnel binding, the lease /
  registry / transfer module handles, now(), mirror().
- records: run-record persistence and the terminal-event outbox
  (save/load, mark_terminal).
- reader: the per-run reader payload and the poll-failure
  classification (write_reader, read_reader, register_poll_failure).
- pull: output pulls and the terminal finalize (pull_outputs,
  finalize_run).
- sweep: the orphan / stale-record reconcile (orphan_sweep).
- launch: verified pre-launch uploads and the acquire-refusal
  orphan notes (upload_one, orphan_notes).
- verbs: the non-launch verbs (poll_run, wait_run) and the
  poller's lease close-out.
- main: argument parsing, dispatch, and the launch path itself.

The package re-exports the former bin/colab-run module's whole
surface under its existing names: consumers load bin/colab-run
by path (as "colab_run") and address colab / load_run_record /
TERMINAL_STATES / main on it, and the thin entry binds these
names from here.
"""

from . import foundation
from . import launch, pull, reader, records, sweep, verbs

from .foundation import REPO, RUNS, cli, colab, colab_bin, colab_env, TOKEN, lease, mirror, now, repo_registry, transfer
from .records import TERMINAL_STATES, emit_terminal_event, load_run_record, mark_terminal, outbox_path, save_record
from .reader import LAUNCH_ABSENCE_GRACE_S, POLL_FAILURE_BOUND, _absence_grace_s, _launch_age_s, read_reader, register_poll_failure, write_reader
from .pull import SINGLE_FILE_CEILING_BYTES, finalize_run, pull_outputs
from .sweep import STALE_RUNNING_S, STALE_STARTING_S, orphan_sweep
from .launch import UPLOAD_CHUNK_THRESHOLD_BYTES, orphan_notes, upload_one
from .verbs import close_out_lease, poll_run, wait_run
from .main import main

__all__ = [
    "REPO", "RUNS", "cli", "lease", "repo_registry", "transfer",
    "colab", "colab_bin", "colab_env", "TOKEN",
    "mirror", "now",
    "TERMINAL_STATES", "save_record", "load_run_record",
    "outbox_path", "emit_terminal_event", "mark_terminal",
    "POLL_FAILURE_BOUND", "LAUNCH_ABSENCE_GRACE_S",
    "_absence_grace_s", "_launch_age_s",
    "write_reader", "read_reader", "register_poll_failure",
    "SINGLE_FILE_CEILING_BYTES", "pull_outputs", "finalize_run",
    "STALE_RUNNING_S", "STALE_STARTING_S", "orphan_sweep",
    "UPLOAD_CHUNK_THRESHOLD_BYTES", "upload_one", "orphan_notes",
    "close_out_lease", "poll_run", "wait_run", "main",
]
