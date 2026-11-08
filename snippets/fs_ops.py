#!/usr/bin/env python3
"""File operations on a Colab session — the payload behind bin/colab-fs.

One JSON spec arrives on stdin:

    {"op": "write",  "path": P, "content_b64": B}
    {"op": "patch",  "path": P, "old_b64": B, "new_b64": B}
    {"op": "delete", "path": P, "missing_ok": false}
    {"op": "read",   "path": P, "offset": 1, "max_lines": 900,
     "max_bytes": N}

Text fields travel base64 so the spec survives the shell channel
byte-exact; patch also accepts plain "old_text"/"new_text" fields.
Semantics mirror the agent file tools: write creates or overwrites
(creating missing parents), patch replaces exactly one occurrence
of the old text (zero or several occurrences are errors), delete
unlinks (a missing file is an error unless missing_ok), read
mutates nothing. A read is paged: "offset" is the 1-based first
line (default 1) and "max_lines" caps the lines returned (default
DEFAULT_READ_MAX_LINES; 0 means no cap), so a read never returns a
monolith unasked. "max_bytes" caps the returned window's bytes
(default DEFAULT_READ_MAX_BYTES; 0 means no cap): a window over
the cap is an error verdict naming bin/colab-xfer, the bulk-move
verb — a read is a window onto a file, never a transfer. The read
streams the file in bounded chunks and splits lines as it goes:
the file's lines are never materialized in-session, however large
it is; only the returned window is held. Writes land atomically —
a fresh sibling temp in the same directory, then os.replace over
the target.

Exactly one JSON verdict line goes to stdout:

    {"ok": bool, "op": ..., "path": ...,
     "before_sha256": hex|null, "after_sha256": hex|null,
     "bytes_before": int|null, "bytes_after": int|null,
     "content_b64": ... (read only), "error": ... (failures only)}

For a read, before_* describe the whole file, after_* describe
the returned slice, and the verdict adds "total_lines",
"lines_returned", "offset", and "truncated" (true when lines
exist beyond the returned window).

Failures are verdicts with ok=false, never tracebacks. Exit code
is 0 when the verdict is ok, 1 otherwise. Standard library only;
runs wherever python3 runs, in-session in practice.
"""

import base64
import binascii
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

OPS = ("write", "patch", "delete", "read")

# A read returns at most this many lines unless the spec says
# otherwise (0 lifts the cap) — a monolith read is never the
# default. The driver mirrors this default and can override it
# per call or via its environment.
DEFAULT_READ_MAX_LINES = 900

# A read's window is also capped in bytes (0 lifts the cap): the
# measured single-file channel ceiling, above which bulk movement
# rides bin/colab-xfer's chunked route instead. The driver mirrors
# this default the same way.
DEFAULT_READ_MAX_BYTES = 50 * 1024 * 1024

# One streaming-read chunk in the read pass: the payload reads and
# hashes the file in slices of this size, never the whole file.
_READ_CHUNK = 64 * 1024

# The bytes bytes.splitlines treats as line boundaries.
_LINE_BOUNDARIES = frozenset(b"\n\r\v\f\x1c\x1d\x1e")


def _open_read(path: Path):
    """Open path for binary streaming reads (the read pass's seam)."""
    return path.open("rb")


def _iter_lines(chunks):
    """Yield the lines of a byte stream, split exactly as
    bytes.splitlines(keepends=True) splits — boundaries are \\n,
    \\r, \\r\\n (one boundary), \\v, \\f, and \\x1c-\\x1e — from an
    iterable of byte chunks. Only one partial line plus one chunk
    is ever held: the file's line list is never materialized."""
    buf = bytearray()
    pos = 0  # scan cursor: buf[:pos] is the current line's scanned prefix

    def scan(final: bool) -> bytes | None:
        nonlocal pos
        i = pos
        while i < len(buf) and buf[i] not in _LINE_BOUNDARIES:
            i += 1
        if i == len(buf):
            pos = i
            if final and buf:
                line = bytes(buf)
                buf.clear()
                pos = 0
                return line
            return None
        end = i + 1
        if buf[i] == 0x0D:  # \r: a following \n joins it into one boundary
            if i + 1 == len(buf) and not final:
                pos = i  # undecidable until the next chunk arrives
                return None
            if i + 1 < len(buf) and buf[i + 1] == 0x0A:
                end = i + 2
        line = bytes(buf[:end])
        del buf[:end]
        pos = 0
        return line

    for chunk in chunks:
        buf += chunk
        while True:
            line = scan(final=False)
            if line is None:
                break
            yield line
    while True:
        line = scan(final=True)
        if line is None:
            break
        yield line


def _run_read(spec: dict, path: Path, raw_path: str, verdict: dict) -> None:
    """Execute a read spec into verdict, streaming the file once.

    The pass hashes and counts the whole file (before_* describe
    it, as the verdict contract states) while collecting only the
    requested window's lines. A window past the byte cap fails the
    read after the pass — the verdict still describes the file,
    and the error names the bulk-move verb.
    """
    offset = spec.get("offset", 1)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
        raise ValueError("offset must be a positive integer (1-based)")
    max_lines = spec.get("max_lines", DEFAULT_READ_MAX_LINES)
    if max_lines is None:
        max_lines = DEFAULT_READ_MAX_LINES
    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 0:
        raise ValueError("max_lines must be a non-negative integer (0 means no cap)")
    max_bytes = spec.get("max_bytes", DEFAULT_READ_MAX_BYTES)
    if max_bytes is None:
        max_bytes = DEFAULT_READ_MAX_BYTES
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer (0 means no cap)")
    try:
        fh = _open_read(path)
    except FileNotFoundError:
        raise ValueError(f"no such file: {raw_path}") from None
    digest = hashlib.sha256()
    total_bytes = 0
    total_lines = 0
    lines_returned = 0
    window = bytearray()
    window_over_cap = False
    with fh:

        def chunks():
            nonlocal total_bytes
            while chunk := fh.read(_READ_CHUNK):
                digest.update(chunk)
                total_bytes += len(chunk)
                yield chunk

        for line in _iter_lines(chunks()):
            total_lines += 1
            if total_lines < offset:
                continue
            if max_lines and lines_returned >= max_lines:
                continue
            window += line
            lines_returned += 1
            if max_bytes and len(window) > max_bytes:
                window_over_cap = True
    verdict["before_sha256"] = digest.hexdigest()
    verdict["bytes_before"] = total_bytes
    if window_over_cap:
        raise ValueError(
            f"read window exceeds the {max_bytes}-byte cap; bulk movement rides bin/colab-xfer"
        )
    data = bytes(window)
    verdict["content_b64"] = base64.b64encode(data).decode("ascii")
    verdict["after_sha256"] = _sha(data)
    verdict["bytes_after"] = len(data)
    verdict["total_lines"] = total_lines
    verdict["lines_returned"] = lines_returned
    verdict["offset"] = offset
    verdict["truncated"] = (offset - 1 + lines_returned) < total_lines


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _before(path: Path) -> bytes | None:
    """The file's current bytes, or None when it does not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _decode_b64(spec: dict, field: str) -> bytes:
    raw = spec.get(field)
    if raw is None:
        raise ValueError(f"spec is missing {field!r}")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} is not valid base64") from exc


def _text_field(spec: dict, name: str) -> str:
    """A patch text field: base64 on the wire, plain text accepted."""
    if f"{name}_b64" in spec:
        return _decode_b64(spec, f"{name}_b64").decode("utf-8")
    if f"{name}_text" in spec:
        return spec[f"{name}_text"]
    raise ValueError(f"spec is missing {name + '_b64'!r}")


def run_spec(spec: dict) -> dict:
    """Execute one spec and return its verdict (never raises)."""
    verdict = {
        "ok": False,
        "op": spec.get("op"),
        "path": spec.get("path"),
        "before_sha256": None,
        "after_sha256": None,
        "bytes_before": None,
        "bytes_after": None,
    }
    try:
        op = spec.get("op")
        if op not in OPS:
            raise ValueError(f"unknown op {op!r}")
        raw_path = spec.get("path")
        if not raw_path or not isinstance(raw_path, str):
            raise ValueError("spec is missing 'path'")
        path = Path(raw_path)
        if op == "read":
            _run_read(spec, path, raw_path, verdict)
            verdict["ok"] = True
            return verdict
        before = _before(path)
        if before is not None:
            verdict["before_sha256"] = _sha(before)
            verdict["bytes_before"] = len(before)

        if op == "write":
            data = _decode_b64(spec, "content_b64")
            _atomic_write(path, data)
            verdict["after_sha256"] = _sha(data)
            verdict["bytes_after"] = len(data)
        elif op == "patch":
            if before is None:
                raise ValueError(f"no such file: {raw_path}")
            old = _text_field(spec, "old")
            new = _text_field(spec, "new")
            if not old:
                raise ValueError("old_text is empty")
            text = before.decode("utf-8")
            count = text.count(old)
            if count == 0:
                raise ValueError("old_text not found")
            if count > 1:
                raise ValueError(f"old_text occurs {count} times (expected exactly 1)")
            data = text.replace(old, new, 1).encode("utf-8")
            _atomic_write(path, data)
            verdict["after_sha256"] = _sha(data)
            verdict["bytes_after"] = len(data)
        else:  # delete
            if before is None and not path.exists():
                if not spec.get("missing_ok"):
                    raise ValueError(f"no such file: {raw_path}")
            else:
                path.unlink()
        verdict["ok"] = True
    except Exception as exc:  # noqa: BLE001 — failures are verdicts
        verdict["error"] = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
    return verdict


def main() -> int:
    try:
        spec = json.loads(sys.stdin.read())
        if not isinstance(spec, dict):
            raise ValueError("spec must be a JSON object")
        verdict = run_spec(spec)
    except Exception as exc:  # noqa: BLE001 — an unreadable spec is a verdict too
        verdict = {
            "ok": False,
            "op": None,
            "path": None,
            "before_sha256": None,
            "after_sha256": None,
            "bytes_before": None,
            "bytes_after": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(verdict))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
