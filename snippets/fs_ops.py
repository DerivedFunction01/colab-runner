#!/usr/bin/env python3
"""File operations on a Colab session — the payload behind bin/colab-fs.

One JSON spec arrives on stdin:

    {"op": "write",  "path": P, "content_b64": B}
    {"op": "patch",  "path": P, "old_b64": B, "new_b64": B}
    {"op": "delete", "path": P, "missing_ok": false}
    {"op": "read",   "path": P, "offset": 1, "max_lines": 900}

Text fields travel base64 so the spec survives the shell channel
byte-exact; patch also accepts plain "old_text"/"new_text" fields.
Semantics mirror the agent file tools: write creates or overwrites
(creating missing parents), patch replaces exactly one occurrence
of the old text (zero or several occurrences are errors), delete
unlinks (a missing file is an error unless missing_ok), read
mutates nothing. A read is paged: "offset" is the 1-based first
line (default 1) and "max_lines" caps the lines returned (default
DEFAULT_READ_MAX_LINES; 0 means no cap), so a read never returns a
monolith unasked. Writes land atomically — a fresh sibling temp in
the same directory, then os.replace over the target.

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
        elif op == "delete":
            if before is None and not path.exists():
                if not spec.get("missing_ok"):
                    raise ValueError(f"no such file: {raw_path}")
            else:
                path.unlink()
        else:  # read
            if before is None:
                raise ValueError(f"no such file: {raw_path}")
            offset = spec.get("offset", 1)
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
                raise ValueError("offset must be a positive integer (1-based)")
            max_lines = spec.get("max_lines", DEFAULT_READ_MAX_LINES)
            if max_lines is None:
                max_lines = DEFAULT_READ_MAX_LINES
            if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 0:
                raise ValueError("max_lines must be a non-negative integer (0 means no cap)")
            lines = before.splitlines(keepends=True)
            window = lines[offset - 1 :] if max_lines == 0 else lines[offset - 1 : offset - 1 + max_lines]
            data = b"".join(window)
            verdict["content_b64"] = base64.b64encode(data).decode("ascii")
            verdict["after_sha256"] = _sha(data)
            verdict["bytes_after"] = len(data)
            verdict["total_lines"] = len(lines)
            verdict["lines_returned"] = len(window)
            verdict["offset"] = offset
            verdict["truncated"] = (offset - 1 + len(window)) < len(lines)
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
