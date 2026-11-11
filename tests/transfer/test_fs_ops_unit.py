"""fs_ops payload unit tests — in-process against tmp dirs.

Pins the semantics bin/colab-fs promises: write creates/overwrites
atomically and reports hashes, patch replaces exactly one
occurrence (zero or several are named errors), delete honours
missing_ok, read round-trips bytes untouched (including non-UTF-8
content), and every outcome is the one-line verdict shape.
"""

import base64
import hashlib
import importlib.util
import io
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_loader("fs_ops", SourceFileLoader("fs_ops", str(REPO / "snippets" / "fs_ops.py")))
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)

VERDICT_KEYS = {"ok", "op", "path", "before_sha256", "after_sha256", "bytes_before", "bytes_after"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_write_creates_file_and_reports_hashes(tmp_path):
    target = tmp_path / "a.txt"
    v = fs.run_spec({"op": "write", "path": str(target), "content_b64": b64(b"hello\n")})
    assert v["ok"] is True
    assert set(v) == VERDICT_KEYS
    assert v["before_sha256"] is None and v["bytes_before"] is None
    assert v["after_sha256"] == sha(b"hello\n") and v["bytes_after"] == 6
    assert target.read_bytes() == b"hello\n"


def test_write_creates_missing_parents(tmp_path):
    target = tmp_path / "deep" / "deeper" / "b.txt"
    v = fs.run_spec({"op": "write", "path": str(target), "content_b64": b64(b"x")})
    assert v["ok"] is True
    assert target.read_bytes() == b"x"


def test_write_overwrite_reports_before_hash(tmp_path):
    target = tmp_path / "a.txt"
    target.write_bytes(b"old content")
    v = fs.run_spec({"op": "write", "path": str(target), "content_b64": b64(b"new")})
    assert v["ok"] is True
    assert v["before_sha256"] == sha(b"old content") and v["bytes_before"] == 11
    assert v["after_sha256"] == sha(b"new")
    assert target.read_bytes() == b"new"


def test_read_round_trips_non_utf8_bytes(tmp_path):
    payload = bytes(range(256))
    target = tmp_path / "bin.dat"
    w = fs.run_spec({"op": "write", "path": str(target), "content_b64": b64(payload)})
    assert w["ok"] is True
    r = fs.run_spec({"op": "read", "path": str(target)})
    assert r["ok"] is True
    assert base64.b64decode(r["content_b64"]) == payload
    assert r["before_sha256"] == r["after_sha256"] == sha(payload)
    assert r["bytes_before"] == r["bytes_after"] == 256


def test_read_missing_file_is_an_error_verdict(tmp_path):
    v = fs.run_spec({"op": "read", "path": str(tmp_path / "nope")})
    assert v["ok"] is False
    assert "no such file" in v["error"]
    assert "content_b64" not in v


def _lines_file(tmp_path, count=12):
    target = tmp_path / "lines.txt"
    target.write_bytes(b"".join(b"line %d\n" % i for i in range(1, count + 1)))
    return target


def test_read_under_the_cap_returns_everything(tmp_path):
    target = _lines_file(tmp_path)
    v = fs.run_spec({"op": "read", "path": str(target)})
    assert v["ok"] is True
    assert v["total_lines"] == 12 and v["lines_returned"] == 12
    assert v["offset"] == 1 and v["truncated"] is False
    assert base64.b64decode(v["content_b64"]) == target.read_bytes()
    assert v["after_sha256"] == v["before_sha256"]


def test_read_max_lines_truncates_and_reports(tmp_path):
    target = _lines_file(tmp_path)
    v = fs.run_spec({"op": "read", "path": str(target), "max_lines": 5})
    assert v["ok"] is True
    assert v["lines_returned"] == 5 and v["truncated"] is True
    first_five = b"".join(b"line %d\n" % i for i in range(1, 6))
    assert base64.b64decode(v["content_b64"]) == first_five
    assert v["after_sha256"] == sha(first_five) and v["bytes_after"] == len(first_five)
    assert v["bytes_before"] == len(target.read_bytes())


def test_read_offset_pages_forward(tmp_path):
    target = _lines_file(tmp_path)
    v = fs.run_spec({"op": "read", "path": str(target), "offset": 11, "max_lines": 5})
    assert v["ok"] is True
    assert v["lines_returned"] == 2 and v["truncated"] is False
    assert base64.b64decode(v["content_b64"]) == b"line 11\nline 12\n"
    past = fs.run_spec({"op": "read", "path": str(target), "offset": 99})
    assert past["ok"] is True
    assert past["lines_returned"] == 0 and past["truncated"] is False
    assert base64.b64decode(past["content_b64"]) == b""


def test_read_default_cap_is_900_and_zero_lifts_it(tmp_path):
    target = _lines_file(tmp_path, count=1000)
    v = fs.run_spec({"op": "read", "path": str(target)})
    assert v["lines_returned"] == 900 and v["truncated"] is True
    full = fs.run_spec({"op": "read", "path": str(target), "max_lines": 0})
    assert full["lines_returned"] == 1000 and full["truncated"] is False
    assert base64.b64decode(full["content_b64"]) == target.read_bytes()


def test_read_counts_a_final_unterminated_line(tmp_path):
    target = tmp_path / "noeol.txt"
    target.write_bytes(b"a\nb")
    v = fs.run_spec({"op": "read", "path": str(target), "offset": 2})
    assert v["total_lines"] == 2 and v["lines_returned"] == 1
    assert base64.b64decode(v["content_b64"]) == b"b"


def test_read_rejects_bad_paging_values(tmp_path):
    target = _lines_file(tmp_path)
    for spec in ({"offset": 0}, {"offset": -1}, {"max_lines": -1}):
        v = fs.run_spec({"op": "read", "path": str(target), **spec})
        assert v["ok"] is False, spec
        assert "content_b64" not in v


def test_patch_replaces_the_single_occurrence(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("alpha beta gamma\n")
    v = fs.run_spec(
        {"op": "patch", "path": str(target), "old_b64": b64(b"beta"), "new_b64": b64(b"BETA")}
    )
    assert v["ok"] is True
    assert target.read_text() == "alpha BETA gamma\n"
    assert v["after_sha256"] == sha(b"alpha BETA gamma\n")


def test_patch_zero_matches_is_old_text_not_found(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("alpha\n")
    v = fs.run_spec(
        {"op": "patch", "path": str(target), "old_b64": b64(b"zzz"), "new_b64": b64(b"q")}
    )
    assert v["ok"] is False
    assert v["error"] == "old_text not found"
    assert target.read_text() == "alpha\n"


def test_patch_multiple_matches_reports_the_count(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x x x\n")
    v = fs.run_spec(
        {"op": "patch", "path": str(target), "old_b64": b64(b"x"), "new_b64": b64(b"y")}
    )
    assert v["ok"] is False
    assert "3 times" in v["error"]
    assert target.read_text() == "x x x\n"


def test_patch_missing_file_and_empty_old_text_are_errors(tmp_path):
    v = fs.run_spec(
        {"op": "patch", "path": str(tmp_path / "nope"), "old_b64": b64(b"a"), "new_b64": b64(b"b")}
    )
    assert v["ok"] is False and "no such file" in v["error"]
    target = tmp_path / "a.txt"
    target.write_text("alpha\n")
    v = fs.run_spec(
        {"op": "patch", "path": str(target), "old_b64": b64(b""), "new_b64": b64(b"b")}
    )
    assert v["ok"] is False and "empty" in v["error"]


def test_delete_present_file(tmp_path):
    target = tmp_path / "a.txt"
    target.write_bytes(b"gone soon")
    v = fs.run_spec({"op": "delete", "path": str(target)})
    assert v["ok"] is True
    assert v["before_sha256"] == sha(b"gone soon")
    assert v["after_sha256"] is None and v["bytes_after"] is None
    assert not target.exists()


def test_delete_missing_file_errors_unless_missing_ok(tmp_path):
    target = tmp_path / "nope"
    v = fs.run_spec({"op": "delete", "path": str(target)})
    assert v["ok"] is False and "no such file" in v["error"]
    v = fs.run_spec({"op": "delete", "path": str(target), "missing_ok": True})
    assert v["ok"] is True
    assert v["before_sha256"] is None and v["after_sha256"] is None


def test_unknown_op_and_bad_base64_are_verdicts_not_tracebacks(tmp_path):
    v = fs.run_spec({"op": "frobnicate", "path": str(tmp_path / "a")})
    assert v["ok"] is False and "unknown op" in v["error"]
    v = fs.run_spec({"op": "write", "path": str(tmp_path / "a"), "content_b64": "!!!"})
    assert v["ok"] is False and "base64" in v["error"]


def _run_main(monkeypatch, stdin_text: str) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = fs.main()
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    return rc, json.loads(lines[0])


def test_main_exit_codes_and_single_verdict_line(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    spec = {"op": "write", "path": str(target), "content_b64": b64(b"hi")}
    rc, v = _run_main(monkeypatch, json.dumps(spec))
    assert rc == 0 and v["ok"] is True
    rc, v = _run_main(monkeypatch, json.dumps({"op": "read", "path": str(tmp_path / "nope")}))
    assert rc == 1 and v["ok"] is False
    rc, v = _run_main(monkeypatch, "this is not json")
    assert rc == 1 and v["ok"] is False and "error" in v
