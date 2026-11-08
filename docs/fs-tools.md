# fs tools — file edits on a Colab session

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
  with `--offset` when `truncated` is true.

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
