# fs tools — file edits and bulk moves on a Colab session

`bin/colab-fs` writes, patches, deletes, and reads individual files
on a Colab session, with the same semantics as the agent file
tools. It rides the shell channel (`bin/colab-sh`): one stream
plants the payload, `snippets/fs_ops.py`, at an in-session temp
path and runs it with one JSON spec as its stdin. Text and content
travel base64 so the spec survives the channel byte-exact.

    colab-fs write  -s SESSION PATH (--from LOCALFILE | --stdin)
    colab-fs patch  -s SESSION PATH (--old TEXT --new TEXT | --old-file F --new-file F)
    colab-fs delete -s SESSION PATH [--missing-ok]
    colab-fs read   -s SESSION PATH [--offset N] [--max-lines N]

The session comes from `-s` or `$COLAB_SH_SESSION`. The tool
targets existing sessions only and never creates one — session
lifecycle belongs to the lease, and `bin/colab-sh` refuses a
session its own read does not call alive. Caller attribution rides
`$COLAB_CALLER` through the channel, as with every wrapper.

Bulk movement is a different tool: `bin/colab-xfer` (below) moves
one file in 32 MB parts with per-part hash resume. colab-fs is for
file-sized edits; anything near the single-file channel ceiling
belongs to the chunked verb, and the bounds below say so at the
point of refusal.

## The four ops

- **write** — create or overwrite PATH with the given bytes,
  creating missing parent directories. The write is atomic: a
  fresh sibling temp in the same directory, then a rename over
  the target.
- **patch** — replace exactly one occurrence of the old text with
  the new text. Zero occurrences is an error (`old_text not
  found`); more than one is an error reporting the count. The
  write-back is atomic, like write.
- **delete** — unlink PATH. A missing file is an error unless
  `--missing-ok` is given.
- **read** — no mutation. The file's bytes go to the driver's
  stdout; the verdict goes to its stderr. A read is paged:
  `--offset N` is the 1-based first line (default 1) and
  `--max-lines N` caps the lines returned (default
  `$COLAB_FS_READ_MAX_LINES` or 900; 0 means no cap), so a read
  never returns a monolith unasked. The verdict reports
  `total_lines`, `lines_returned`, and `truncated` — page forward
  with `--offset` when `truncated` is true. In-session, the
  payload streams the file in bounded chunks and splits lines as
  it goes (the line definition is `bytes.splitlines`' own,
  `\r\n` pairs included): the file's lines are never materialized,
  however large the file is — only the returned window is held.

## Bounds

Single-file channel transfers are reliable to ~50 MB and
unreliable above (the measured ceiling in AGENTS.md), so
colab-fs payloads are bounded by it:

- A **write** payload above the ceiling is refused by the driver
  before the channel is touched — nothing is written — and the
  refusal names `bin/colab-xfer`. The ceiling defaults to
  52428800 bytes and is tunable via `$COLAB_FS_MAX_WRITE_BYTES`.
- A **read** window is capped in bytes as well as lines: the
  spec carries `max_bytes` (default 52428800, tunable via
  `$COLAB_FS_MAX_READ_BYTES`; 0 lifts it deliberately) and the
  payload refuses a window past the cap with an error naming
  `bin/colab-xfer`. `--max-lines 0` lifts only the line cap,
  never this one.

## The verdict

Every op prints one JSON verdict line (stdout for write/patch/
delete, stderr for read):

    {"ok": bool, "op": ..., "path": ...,
     "before_sha256": hex|null, "after_sha256": hex|null,
     "bytes_before": int|null, "bytes_after": int|null,
     "content_b64": ... (read only), "error": ... (failures only)}

For a read, `before_*` describe the whole file and `after_*` the
returned slice; the verdict adds the paging fields named above.

Failures are verdicts with `ok: false`, never tracebacks. The
driver's exit code is the payload's: 0 when the verdict is ok,
1 when it is not, 2 for usage errors.

## Bulk moves: bin/colab-xfer

    colab-xfer up     -s SESSION LOCAL_PATH REMOTE_NAME [--caller TAG] [--id XFER_ID]
    colab-xfer down   -s SESSION REMOTE_NAME LOCAL_PATH [--caller TAG] [--id XFER_ID]
    colab-xfer status XFER_ID

The verb over `sync/transfer.py`'s chunked machinery, for the
moves that sit outside any job's declared outputs. REMOTE_NAME is
a bare file name at `/content` (a leading `/content/` is accepted
and stripped); the file moves in 32 MB parts, each retried
individually and verified by its own SHA-256, and the assembled
file is verified end to end. A part whose far-side (up) or local
(down) copy already matches its hash is not re-moved, so a killed
or failed move re-run later moves only what never landed
verified. The caller claim resolves as every wrapper's does and
is stamped on the record.

Every move keeps a transfer record at `results/xfers/<id>.json`
(the id is generated — `<direction>-<UTC timestamp>-<pid>` — or
given with `--id`), written as the parts land and finished with
the terminal state:

    {"xfer_id": ..., "direction": "up"|"down", "session": ...,
     "caller": ..., "local_path": ..., "remote_name": ...,
     "state": "starting"|"running"|"complete"|"failed",
     "started_at": ..., "finished_at": ...,
     "bytes": ..., "sha256": ..., "remote_sha256": ...,
     "parts": N, "parts_done": K, "resumed_parts": ...,
     "moved_parts": ..., "retried_parts": ...,
     "error": {"stage": ..., "attempts": ..., "stderr": ...}
              (failed only)}

The record makes a move pollable (`colab-xfer status <id>` prints
it without touching a session) and continuable: re-invoking the
move with the same `--id` updates the same record while the parts
resume by hash. The verb prints the final record and exits 0 on
complete, 1 on a failed move, 2 on usage errors; `status` exits
with the record's state (0 complete, 1 failed, 3 still in
flight, 2 no such record), and every move closes with a one-line
verdict on stderr — complete with the hash, or FAILED with the
stage — because the record's own verdict fields sit at its top
while its hash fields sit at its end, so a trimmed capture of
the record alone can read as a success-shaped null.
