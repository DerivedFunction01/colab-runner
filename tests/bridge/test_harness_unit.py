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

REPO = Path(__file__).resolve().parent.parent.parent
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


def test_execute_request_carries_the_envelope_id_for_dedupe(stub_service):
    """The connector's dedupe contract: the ferry envelope id
    (the request-file stem) rides as a sibling of args, so a
    repeat performance of the same envelope returns the
    existing bridge request instead of queueing a twin."""
    url, seen = stub_service
    resp = harness.execute_request(
        {"id": "cafe0123abcd", "method": "POST", "path": "/tools/chat_list",
         "token": "tok-abc", "body": {"args": {}}}, url)
    assert resp["status"] == 200
    assert seen[0]["body"] == {"args": {}, "envelope_id": "cafe0123abcd"}


def test_execute_request_omits_an_out_of_shape_envelope_id(stub_service):
    """An id outside the connector's validation shape is
    omitted rather than risking a bad_args refusal of the
    whole call."""
    url, seen = stub_service
    harness.execute_request(
        {"id": "bad id/with spaces", "method": "POST", "path": "/tools/chat_list",
         "token": "tok-abc", "body": {"args": {}}}, url)
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

    served_log = []
    cycle = harness.relay_cycle(fake_colab, tmp_path, "any-session", url,
                                log=lambda *entry: served_log.append(entry))
    assert cycle == {"requests": 1, "served": ["cafe01"]}
    assert served_log == [("cafe01", "POST", "/tools/chat_list", 200)]
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


def test_relay_lock_refuses_a_second_holder(tmp_path):
    """One relay per session: two relays cycling one ferry both
    perform the same envelope (live-proven duplication — a
    hand-started relay overlapped the driver's). The second
    holder is refused while the first holds the lock."""
    import pytest as _pytest
    with harness.relay_lock(tmp_path, "sess-1"):
        with _pytest.raises(harness.RelayBusy):
            with harness.relay_lock(tmp_path, "sess-1"):
                pass
    with harness.relay_lock(tmp_path, "sess-1"):  # free again after release
        pass


def test_relay_until_runs_reconcile_only_when_the_lock_is_held(tmp_path):
    """The bootstrap's fallback: another relay holds the ferry,
    so the driver serves nothing itself but still reconciles
    and finishes on the run's terminal state."""

    def forbidden_colab(*argv, **_kw):
        raise AssertionError("no channel use in reconcile-only mode")

    with harness.relay_lock(tmp_path, "sess-2"):
        totals = harness.relay_until(
            forbidden_colab, tmp_path, "sess-2", lambda: True, interval_s=0)
    assert totals["relay_external"] is True
    assert totals["served"] == 0


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


def test_relay_until_survives_failed_cycles(stub_service, tmp_path):
    """A channel exec failure is a skipped cycle, never the
    relay's death: the first live proof's smoke starved when
    the relay exited on its first failed cycle."""
    url, _seen = stub_service
    out_dir = tmp_path / "out"
    in_dir = tmp_path / "in"
    out_dir.mkdir()
    (out_dir / "cafe02.json").write_text(json.dumps(
        {"id": "cafe02", "method": "GET", "path": "/tools", "token": "t", "body": None}))
    calls = {"n": 0}

    def flaky_colab(*argv, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("channel busy (429)")
        script = Path(argv[-1])
        text = script.read_text().replace(harness.FERRY_OUT_REMOTE, str(out_dir)).replace(
            harness.FERRY_IN_REMOTE, str(in_dir))
        proc = subprocess.run([sys.executable, "-c", text], capture_output=True, text=True)
        return subprocess.CompletedProcess(argv, proc.returncode, proc.stdout, proc.stderr)

    checks = {"n": 0}

    def done():
        checks["n"] += 1
        return checks["n"] >= 2

    totals = harness.relay_until(flaky_colab, tmp_path, "any-session", done, url, interval_s=0)
    assert totals["errors"] == 1
    assert totals["served"] == 1


def test_bootstrap_launch_survives_pipe_holding_descendants(tmp_path):
    """The runner's detached launch leaves a background poller
    holding the launch's stdout; a captured pipe would keep the
    driver waiting for that descendant's EOF (the first live
    bootstrap hung exactly so). The driver reads a file instead
    and must return promptly."""
    import time
    import types

    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "var" / "harness").mkdir(parents=True)
    (repo / "var" / "harness" / "sweeper-venue.token").write_text("tok-staged\n")
    (repo / "results" / "r1").mkdir(parents=True)
    fake_run = repo / "bin" / "colab-run"
    fake_run.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--wait\" ]; then\n"
        f"  echo '{{\"ok\": true}}' > {repo}/results/r1/smoke-result.json\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"--poll\" ]; then\n"
        "  exit 0\n"
        "fi\n"
        "(sleep 20) &\n"
        "echo '{'\n"
        "echo '  \"run_id\": \"r1\",'\n"
        "echo '  \"session\": \"s1\",'\n"
        "echo '  \"state\": \"polling\"'\n"
        "echo '}'\n"
        "exit 0\n")
    fake_run.chmod(0o755)
    sched_bin = tmp_path / "scheduler" / "bin"
    sched_bin.mkdir(parents=True)
    (sched_bin / "harness").write_text("#!/bin/sh\n")

    class _RunMod:
        TERMINAL_STATES = ("complete", "failed", "orphaned")

        @staticmethod
        def load_run_record(_rid):
            return {"state": "complete"}

    def fake_colab(*argv, **_kw):
        return subprocess.CompletedProcess(argv, 0, '{"requests": {}}', "")

    started = time.time()
    verdict = harness.bootstrap(
        repo, scheduler_repo=tmp_path / "scheduler",
        run_mod=_RunMod, colab=fake_colab)
    assert time.time() - started < 15  # not the grandchild's 20s
    assert verdict["ok"] and verdict["run_id"] == "r1"
