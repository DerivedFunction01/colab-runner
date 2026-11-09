"""Unit tests for the harness connector venue support
(sync/harness.py) and the bootstrap payload
(jobs/harness-bootstrap/bootstrap.py).

The relay's request execution is tested against a stub HTTP
server on loopback; the ferry channel scripts are executed
against a local tree (the scripts take their dirs as
parameters for exactly this); the payload is installed into a
fabricated content root with a stub client. No session, no
service, no real token anywhere — the tokens in these tests
are literals on purpose, to prove they move through the
envelope and nowhere else.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))
import _load  # noqa: E402

harness = _load.sync("harness")


def _load_payload():
    spec = importlib.util.spec_from_loader(
        "harness_bootstrap_payload",
        SourceFileLoader("harness_bootstrap_payload", str(REPO / "jobs" / "harness-bootstrap" / "bootstrap.py")),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- request execution against a stub service ---------------------------


class _StubHandler(BaseHTTPRequestHandler):
    seen = []

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body) if body else None,
        })
        if self.path == "/tools/secret":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"ok": false, "error": "unauthenticated"}')
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true, "tools": ["chat_list"]}')

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):  # silence the stub's logging
        pass


@pytest.fixture()
def stub_service():
    _StubHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _StubHandler.seen
    server.shutdown()


def test_execute_request_performs_with_bearer(stub_service):
    url, seen = stub_service
    resp = harness.execute_request(
        {"method": "POST", "path": "/tools/chat_list", "token": "tok-abc", "body": {"args": {}}}, url)
    assert resp["status"] == 200
    assert resp["body"]["ok"] is True
    assert seen[0]["auth"] == "Bearer tok-abc"
    assert seen[0]["body"] == {"args": {}}


def test_execute_request_refuses_non_tools_paths_locally(stub_service):
    url, seen = stub_service
    resp = harness.execute_request(
        {"method": "GET", "path": "/ops", "token": "tok-abc", "body": None}, url)
    assert resp == {"status": 403, "body": {"ok": False, "error": "relay_refused"}}
    assert seen == []  # the service never saw it


def test_execute_request_passes_refusals_through(stub_service):
    url, _seen = stub_service
    resp = harness.execute_request(
        {"method": "GET", "path": "/tools/secret", "token": "bad", "body": None}, url)
    assert resp["status"] == 401
    assert resp["body"]["error"] == "unauthenticated"


def test_execute_request_unreachable_service():
    resp = harness.execute_request(
        {"method": "GET", "path": "/tools", "token": "t", "body": None},
        "http://127.0.0.1:1")
    assert resp["status"] == 503
    assert resp["body"]["error"] == "service_unreachable"


# --- ferry channel scripts against a local tree --------------------------


def _run_script(text: str) -> dict:
    proc = subprocess.run([sys.executable, "-c", text], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ferry_scripts_round_trip(tmp_path):
    out_dir = tmp_path / "out"
    in_dir = tmp_path / "in"
    out_dir.mkdir()
    envelope = {"id": "abc123", "method": "POST", "path": "/tools/chat_list",
                "token": "tok-abc", "body": {"args": {}}}
    (out_dir / "abc123.json").write_text(json.dumps(envelope))
    listing = _run_script(harness.read_requests_script(str(out_dir)))
    assert listing["requests"]["abc123"]["token"] == "tok-abc"
    _run_script(harness.deliver_script(
        {"abc123": {"status": 200, "body": {"ok": True}}}, str(out_dir), str(in_dir)))
    assert json.loads((in_dir / "abc123.json").read_text()) == {"status": 200, "body": {"ok": True}}
    assert not (out_dir / "abc123.json").exists()  # served requests are deleted


def test_relay_cycle_with_fake_channel(stub_service, tmp_path):
    url, seen = stub_service
    out_dir = tmp_path / "out"
    in_dir = tmp_path / "in"
    out_dir.mkdir()
    (out_dir / "cafe01.json").write_text(json.dumps(
        {"id": "cafe01", "method": "POST", "path": "/tools/chat_list",
         "token": "tok-abc", "body": {"args": {}}}))

    def fake_colab(*argv, **_kw):
        script = Path(argv[-1])
        text = script.read_text()
        text = text.replace(harness.FERRY_OUT_REMOTE, str(out_dir)).replace(
            harness.FERRY_IN_REMOTE, str(in_dir))
        proc = subprocess.run([sys.executable, "-c", text], capture_output=True, text=True)
        return subprocess.CompletedProcess(argv, proc.returncode, proc.stdout, proc.stderr)

    cycle = harness.relay_cycle(fake_colab, tmp_path, "any-session", url)
    assert cycle == {"requests": 1, "served": ["cafe01"]}
    assert seen[0]["auth"] == "Bearer tok-abc"
    assert json.loads((in_dir / "cafe01.json").read_text())["body"]["ok"] is True
    empty = harness.relay_cycle(fake_colab, tmp_path, "any-session", url)
    assert empty == {"requests": 0, "served": []}


# --- bootstrap payload ----------------------------------------------------


def _fabricate_content(tmp_path, smoke_ok: bool) -> Path:
    root = tmp_path / "content"
    root.mkdir()
    client = root / "harness"
    if smoke_ok:
        client.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "assert os.environ.get('HARNESS_TOKEN') == 'tok-planted'\n"
            "print(json.dumps({'ok': True, 'status': 'done', "
            "'result': {'chats': [{'chat_id': 'c1'}, {'chat_id': 'c2'}]}}))\n")
    else:
        client.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
    client.chmod(0o755)
    (root / "sweeper-venue.token").write_text("tok-planted\n")
    return root


def _run_payload(tmp_path, smoke_ok: bool):
    root = _fabricate_content(tmp_path, smoke_ok)
    out_dir = tmp_path / "run-out"
    env = dict(os.environ)
    env["HARNESS_BOOTSTRAP_ROOT"] = str(root)
    proc = subprocess.run(
        [sys.executable, str(REPO / "jobs" / "harness-bootstrap" / "bootstrap.py"),
         "--out", str(out_dir)],
        capture_output=True, text=True, env=env, timeout=300)
    return proc, root, out_dir


def test_payload_installs_token_shreds_upload_and_smokes(tmp_path):
    proc, root, out_dir = _run_payload(tmp_path, smoke_ok=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    token = root / "harness-client" / "token"
    assert token.read_text().strip() == "tok-planted"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert not (root / "sweeper-venue.token").exists()  # the upload copy was shredded
    assert (root / "harness-client" / "harness").is_file()
    assert (root / "harness-ferry" / "out").is_dir()
    verdict = json.loads((out_dir / "smoke-result.json").read_text())
    assert verdict["ok"] and verdict["chats"] == 2
    assert "tok-planted" not in json.dumps(verdict)  # the token reaches no output


def test_payload_failed_smoke_fails_the_run(tmp_path):
    proc, _root, out_dir = _run_payload(tmp_path, smoke_ok=False)
    assert proc.returncode == 1
    verdict = json.loads((out_dir / "smoke-result.json").read_text())
    assert verdict["ok"] is False and verdict["exit_code"] == 3


def test_mint_token_never_prints_and_stages_0600(tmp_path, monkeypatch):
    fake_sched_dir = tmp_path / "scheduler" / "bin"
    fake_sched_dir.mkdir(parents=True)
    fake = fake_sched_dir / "sched"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'connector caller sweeper-venue (scopes: read) — token, shown once:'\n"
        "echo 'tok-minted-123'\n"
        "echo 'deliver it through the secure route'\n")
    fake.chmod(0o755)
    dest = harness.mint_token(tmp_path, "sweeper-venue", "read", tmp_path / "scheduler")
    assert dest.read_text().strip() == "tok-minted-123"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
