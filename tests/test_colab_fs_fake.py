"""colab-fs driver tests against a stub channel (no network, no ssh).

The driver resolves its channel as its own repo's bin/colab-sh, so
the fixture builds a scratch repo tree: the real bin/colab-fs, a
stub bin/colab-sh that captures stdin and emits a canned verdict
with a canned exit code, and the real snippets/fs_ops.py. Pins the
assembly: the captured stream's payload segment decodes to
snippets/fs_ops.py byte-for-byte, the spec JSON carries the right
op/path/base64 fields, verdicts and exit codes propagate, read
splits content (stdout) from verdict (stderr), and usage errors
exit 2.
"""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FS_OPS_BYTES = (REPO / "snippets" / "fs_ops.py").read_bytes()

STUB = """#!/usr/bin/env bash
cat > "${COLAB_FS_STUB_CAPTURE:?}"
printf '%s\\n' "${COLAB_FS_STUB_VERDICT:-}"
exit "${COLAB_FS_STUB_RC:-0}"
"""


def make_fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    (tmp_path / "snippets").mkdir()
    shutil.copy(REPO / "bin" / "colab-fs", tmp_path / "bin" / "colab-fs")
    stub = tmp_path / "bin" / "colab-sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "snippets" / "fs_ops.py").symlink_to(REPO / "snippets" / "fs_ops.py")
    return tmp_path / "bin" / "colab-fs"


def run_driver(driver: Path, tmp_path: Path, args: list, verdict: dict | None, rc: int = 0,
               stdin: bytes = b"", session: str | None = "fake-session"):
    env = dict(os.environ)
    env.pop("COLAB_SH_SESSION", None)
    capture = tmp_path / "captured-stream.txt"
    env["COLAB_FS_STUB_CAPTURE"] = str(capture)
    env["COLAB_FS_STUB_VERDICT"] = json.dumps(verdict) if verdict is not None else ""
    env["COLAB_FS_STUB_RC"] = str(rc)
    full_args = list(args)
    if session is not None and full_args:
        full_args = [full_args[0], "-s", session] + full_args[1:]
    r = subprocess.run(
        ["bash", str(driver)] + full_args,
        input=stdin,
        capture_output=True,
        env=env,
    )
    return r, capture


def segment(stream: str, marker: str) -> str:
    lines = stream.splitlines()
    opener = next(
        i for i, line in enumerate(lines) if line == marker or line.endswith(f"<<'{marker}'")
    )
    end = lines.index(marker, opener + 1)
    return "\n".join(lines[opener + 1 : end])


def captured_spec(capture: Path) -> dict:
    stream = capture.read_text()
    payload = base64.b64decode(segment(stream, "COLAB_FS_PAYLOAD_B64"))
    assert payload == FS_OPS_BYTES
    return json.loads(segment(stream, "COLAB_FS_SPEC_JSON"))


def ok_verdict(op: str, path: str, **extra) -> dict:
    v = {
        "ok": True,
        "op": op,
        "path": path,
        "before_sha256": None,
        "after_sha256": None,
        "bytes_before": None,
        "bytes_after": None,
    }
    v.update(extra)
    return v


def test_write_from_file_assembles_payload_and_spec(tmp_path):
    driver = make_fake_repo(tmp_path)
    local = tmp_path / "local.txt"
    local.write_bytes(b"payload content\n")
    verdict = ok_verdict("write", "/content/work/a.txt")
    r, capture = run_driver(driver, tmp_path, ["write", "/content/work/a.txt", "--from", str(local)], verdict)
    assert r.returncode == 0
    assert r.stdout.decode() == json.dumps(verdict) + "\n"
    spec = captured_spec(capture)
    assert spec["op"] == "write"
    assert spec["path"] == "/content/work/a.txt"
    assert base64.b64decode(spec["content_b64"]) == b"payload content\n"


def test_write_from_stdin(tmp_path):
    driver = make_fake_repo(tmp_path)
    verdict = ok_verdict("write", "/content/b.txt")
    r, capture = run_driver(
        driver, tmp_path, ["write", "/content/b.txt", "--stdin"], verdict, stdin=b"from stdin\x00bytes"
    )
    assert r.returncode == 0
    spec = captured_spec(capture)
    assert base64.b64decode(spec["content_b64"]) == b"from stdin\x00bytes"


def test_not_ok_verdict_and_exit_code_propagate(tmp_path):
    driver = make_fake_repo(tmp_path)
    local = tmp_path / "local.txt"
    local.write_bytes(b"x")
    verdict = ok_verdict("write", "/content/a.txt")
    verdict["ok"] = False
    verdict["error"] = "old_text not found"
    r, _ = run_driver(driver, tmp_path, ["write", "/content/a.txt", "--from", str(local)], verdict, rc=1)
    assert r.returncode == 1
    assert r.stdout.decode() == json.dumps(verdict) + "\n"


def test_patch_text_and_file_forms_base64_the_texts(tmp_path):
    driver = make_fake_repo(tmp_path)
    verdict = ok_verdict("patch", "/content/a.txt")
    r, capture = run_driver(
        driver, tmp_path, ["patch", "/content/a.txt", "--old", "a b", "--new", "c\nd"], verdict
    )
    assert r.returncode == 0
    spec = captured_spec(capture)
    assert spec["op"] == "patch"
    assert base64.b64decode(spec["old_b64"]) == b"a b"
    assert base64.b64decode(spec["new_b64"]) == b"c\nd"

    old_f = tmp_path / "old.txt"
    new_f = tmp_path / "new.txt"
    old_f.write_bytes(b"old\x00bytes")
    new_f.write_bytes(b"new bytes")
    r, capture = run_driver(
        driver, tmp_path, ["patch", "/content/a.txt", "--old-file", str(old_f), "--new-file", str(new_f)], verdict
    )
    assert r.returncode == 0
    spec = captured_spec(capture)
    assert base64.b64decode(spec["old_b64"]) == b"old\x00bytes"
    assert base64.b64decode(spec["new_b64"]) == b"new bytes"


def test_delete_missing_ok_flag_reaches_the_spec(tmp_path):
    driver = make_fake_repo(tmp_path)
    verdict = ok_verdict("delete", "/content/a.txt")
    r, capture = run_driver(driver, tmp_path, ["delete", "/content/a.txt", "--missing-ok"], verdict)
    assert r.returncode == 0
    assert captured_spec(capture)["missing_ok"] is True
    r, capture = run_driver(driver, tmp_path, ["delete", "/content/a.txt"], verdict)
    assert r.returncode == 0
    assert captured_spec(capture)["missing_ok"] is False


def test_read_splits_content_to_stdout_and_verdict_to_stderr(tmp_path):
    driver = make_fake_repo(tmp_path)
    content = bytes(range(256))
    verdict = ok_verdict(
        "read",
        "/content/bin.dat",
        before_sha256="ab" * 32,
        after_sha256="ab" * 32,
        bytes_before=256,
        bytes_after=256,
        content_b64=base64.b64encode(content).decode("ascii"),
    )
    r, capture = run_driver(driver, tmp_path, ["read", "/content/bin.dat"], verdict)
    assert r.returncode == 0
    assert r.stdout == content
    assert json.loads(r.stderr.decode()) == verdict
    spec = captured_spec(capture)
    assert spec["op"] == "read"
    assert spec["offset"] == 1 and spec["max_lines"] == 900  # the built-in defaults

    verdict["ok"] = False
    verdict.pop("content_b64")
    verdict["error"] = "no such file: /content/bin.dat"
    r, _ = run_driver(driver, tmp_path, ["read", "/content/bin.dat"], verdict, rc=1)
    assert r.returncode == 1
    assert r.stdout == b""
    assert "no such file" in r.stderr.decode()


def test_read_paging_flags_and_env_default_reach_the_spec(tmp_path, monkeypatch):
    driver = make_fake_repo(tmp_path)
    verdict = ok_verdict("read", "/content/a.txt", content_b64="")
    r, capture = run_driver(
        driver, tmp_path, ["read", "/content/a.txt", "--offset", "3", "--max-lines", "7"], verdict
    )
    assert r.returncode == 0
    spec = captured_spec(capture)
    assert spec["offset"] == 3 and spec["max_lines"] == 7
    monkeypatch.setenv("COLAB_FS_READ_MAX_LINES", "42")
    r, capture = run_driver(driver, tmp_path, ["read", "/content/a.txt"], verdict)
    assert r.returncode == 0
    assert captured_spec(capture)["max_lines"] == 42
    r, capture = run_driver(
        driver, tmp_path, ["read", "/content/a.txt", "--max-lines", "0"], verdict
    )
    assert r.returncode == 0
    assert captured_spec(capture)["max_lines"] == 0  # 0 lifts the cap


def test_session_falls_back_to_colab_sh_session_env(tmp_path, monkeypatch):
    driver = make_fake_repo(tmp_path)
    verdict = ok_verdict("delete", "/content/a.txt")
    r, capture = run_driver(driver, tmp_path, ["delete", "/content/a.txt"], verdict, session=None)
    assert r.returncode == 2  # no -s and no env: usage error
    monkeypatch.setenv("COLAB_SH_SESSION", "env-session")
    env = dict(os.environ)
    env["COLAB_FS_STUB_CAPTURE"] = str(tmp_path / "captured-stream.txt")
    env["COLAB_FS_STUB_VERDICT"] = json.dumps(verdict)
    env["COLAB_FS_STUB_RC"] = "0"
    r = subprocess.run(
        ["bash", str(driver), "delete", "/content/a.txt"], capture_output=True, env=env
    )
    assert r.returncode == 0


def test_usage_errors_exit_2(tmp_path):
    driver = make_fake_repo(tmp_path)
    verdict = ok_verdict("write", "/content/a.txt")
    local = tmp_path / "local.txt"
    local.write_bytes(b"x")
    cases = [
        [],  # no op at all
        ["frobnicate", "/content/a.txt"],
        ["write", "/content/a.txt"],  # neither --from nor --stdin
        ["write", "/content/a.txt", "--from", str(local), "--stdin"],  # both
        ["write", "/content/a.txt", "--from", str(tmp_path / "missing")],  # unreadable source
        ["write", '/content/has"quote', "--from", str(local)],  # path refused
        ["patch", "/content/a.txt", "--old", "a"],  # half a patch
        ["read", "/content/a.txt", "--missing-ok"],  # foreign option
        ["read", "/content/a.txt", "--offset", "0"],  # offset is 1-based
        ["read", "/content/a.txt", "--offset", "x"],  # not a number
        ["read", "/content/a.txt", "--max-lines", "-1"],  # not a count
        ["write", "/content/a.txt", "--from", str(local), "--offset", "2"],  # paging is read-only
    ]
    for args in cases:
        r, _ = run_driver(driver, tmp_path, args, verdict)
        assert r.returncode == 2, args
        assert b"usage:" in r.stderr

def test_channel_refusal_is_terminal_no_retry(tmp_path):
    """A claim refusal from the channel (colab-sh exit 2) propagates
    as the driver's own exit with exactly one channel invocation —
    the driver never retries a refusal into a second hold."""
    driver = make_fake_repo(tmp_path)
    count = tmp_path / "sh-calls.txt"
    stub = tmp_path / "bin" / "colab-sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"echo x >> {count}\n"
        "cat > /dev/null\n"
        "printf '%s\\n' 'colab-sh: synchronous attach refused' >&2\n"
        "exit 2\n"
    )
    stub.chmod(0o755)
    local = tmp_path / "local.txt"
    local.write_bytes(b"x")
    r, _ = run_driver(driver, tmp_path, ["write", "/content/a.txt", "--from", str(local)], None)
    assert r.returncode == 2
    assert len(count.read_text().splitlines()) == 1

