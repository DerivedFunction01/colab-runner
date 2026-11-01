"""Lease UI server against a stub lease CLI (no login, no network,
no real lease state).

Covers the contract the page depends on: /api/state passes the core
JSON through (plus profiles + caps), mutations invoke bin/colab-lease
with the right argv and append the right outbox events with increasing
seq, and /api/outbox?after= filters without acknowledging.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STUB_SRC = REPO / "tests" / "fixtures" / "stub_lease_bin.py"
SERVER = REPO / "ui" / "server.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UiServer:
    def __init__(self, tmp_path: Path, extra_env: dict | None = None):
        self.var = tmp_path / "var"
        self.log = tmp_path / "invocations.log"
        stub = tmp_path / "stub_lease_bin.py"
        stub.write_text(STUB_SRC.read_text())
        stub.chmod(0o755)
        self.port = free_port()
        env = dict(
            os.environ,
            COLAB_LEASE_BIN=str(stub),
            COLAB_RUNNER_VAR=str(self.var),
            COLAB_LEASE_UI_PORT=str(self.port),
            STUB_LEASE_LOG=str(self.log),
        )
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.get("/")
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"server exited early: {self.proc.stdout.read()}")
                time.sleep(0.1)
        raise RuntimeError("server did not come up")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str):
        with urllib.request.urlopen(self.url(path), timeout=10) as resp:
            body = resp.read().decode()
            return resp.status, body

    def get_json(self, path: str):
        _, body = self.get(path)
        return json.loads(body)

    def post(self, path: str, payload: dict):
        req = urllib.request.Request(
            self.url(path),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode() or "{}")

    def invocations(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def outbox_events(self) -> list[dict]:
        return self.get_json("/api/outbox?after=0")["events"]


@pytest.fixture()
def ui(tmp_path):
    server = UiServer(tmp_path)
    yield server
    server.close()


def test_index_and_theme_served(ui):
    status, body = ui.get("/")
    assert status == 200
    assert "Colab Lease" in body
    assert "/static/theme.css" in body
    status, css = ui.get("/static/theme.css")
    assert status == 200
    assert "--wb-accent" in css  # workbench token, mirrored


def test_state_passes_core_json_through(ui):
    data = ui.get_json("/api/state")
    assert data["records"][0]["profile"] == "workload"
    assert data["records"][0]["record"]["session"] == "workload"
    assert data["records"][0]["action"] == "active"
    assert data["grant"] is None
    assert data["budget"]["balance"] == 571.7
    # profiles + caps are served alongside, from the repo config
    assert data["profiles"]["profiles"]["workload"]["high_mem"] is True
    assert data["profiles"]["default_profile"] == "workload"
    # The caps are the operator's live panel settings: compare
    # against the config file, never a pinned constant.
    live_cfg = json.loads((REPO / "config" / "lease.json").read_text())
    assert data["caps"]["daily_cu_budget"] == live_cfg["daily_cu_budget"]
    assert data["caps"]["monthly_cu_budget"] == live_cfg["monthly_cu_budget"]
    assert "state" in ui.invocations()


def test_state_serves_specs_including_null_rate(ui):
    data = ui.get_json("/api/state")
    specs = {s["id"]: s for s in data["specs"]}
    assert set(specs) == {"workload", "lobby", "standard", "t4"}
    assert specs["workload"]["rate_cu_per_hour"] == 0.26
    assert specs["workload"]["high_mem"] is True
    assert specs["t4"]["gpu"] == "T4"
    # the null-rate entry is what drives the UI's "Cost unconfirmed"
    assert specs["t4"]["rate_cu_per_hour"] is None
    # every spec carries its catalog label (display name; ids unchanged)
    assert specs["workload"]["label"] == "High-RAM"
    assert specs["lobby"]["label"] == "Lobby"
    assert specs["standard"]["label"] == "Standard"
    assert specs["t4"]["label"] == "T4"


def test_frontend_renders_specs_and_cost_unconfirmed():
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "specs" in js  # picker is rendered from state.specs
    assert "Cost unconfirmed" in js  # null-rate rendering path
    assert "label" in js  # spec rows and card titles render the label
    html = (REPO / "ui" / "static" / "index.html").read_text()
    assert 'id="burst-specs"' in html  # two-tier burst spec picker
    assert 'id="profile-specs"' in html  # normal-lease spec picker
    # the full duration picker from the mockup: presets to 3d + custom
    assert 'data-hours="72"' in html
    assert 'id="normal-custom"' in html
    assert 'id="grant-custom"' in html


def test_grant_passes_burst_profile(ui):
    status, result = ui.post("/api/grant", {"hours": 4, "burst": "t4"})
    assert status == 200 and result["action"] == "granted"
    assert "grant --hours 4 --burst t4" in ui.invocations()
    events = ui.outbox_events()
    assert events[0]["kind"] == "grant-started"
    assert events[0]["detail"]["burst"] == "t4"


def test_start_invokes_acquire_and_appends_event(ui):
    status, result = ui.post("/api/lease/start", {"profile": "workload", "hours": 2})
    assert status == 200
    assert result["action"] == "acquired"
    assert "acquire workload --hours 2 --via panel" in ui.invocations()
    events = ui.outbox_events()
    assert len(events) == 1
    assert events[0]["kind"] == "lease-started"
    assert events[0]["seq"] == 1
    assert events[0]["detail"]["profile"] == "workload"
    assert events[0]["detail"]["hours"] == 2


def test_stop_grant_ungrant_flow_and_seqs(ui):
    status, result = ui.post("/api/lease/stop", {"profile": "workload"})
    assert status == 200 and result["action"] == "released"
    status, result = ui.post("/api/grant", {"hours": 4})
    assert status == 200 and result["action"] == "granted"
    status, result = ui.post("/api/ungrant", {})
    assert status == 200 and result["action"] == "grant-ended"

    invocations = ui.invocations()
    assert "release workload" in invocations
    assert "grant --hours 4" in invocations
    assert "ungrant" in invocations

    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["lease-stopped", "grant-started", "grant-ended"]
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_outbox_after_filters_without_acknowledging(ui):
    ui.post("/api/lease/start", {"profile": "workload", "hours": 1})
    ui.post("/api/lease/stop", {"profile": "workload"})

    page = ui.get_json("/api/outbox?after=1")
    assert [e["seq"] for e in page["events"]] == [2]
    assert page["last_seq"] == 2
    # reading never acknowledges: the same query returns the same page
    again = ui.get_json("/api/outbox?after=1")
    assert again == page
    everything = ui.get_json("/api/outbox?after=0")
    assert [e["seq"] for e in everything["events"]] == [1, 2]


def test_start_core_failure_appends_failed_event(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_LEASE_FAIL": "acquire"})
    try:
        status, _ = server.post("/api/lease/start", {"profile": "workload", "hours": 2})
        assert status == 502
        events = server.outbox_events()
        assert len(events) == 1
        assert events[0]["kind"] == "lease-start-failed"
        assert events[0]["detail"]["profile"] == "workload"
    finally:
        server.close()


def test_start_requires_profile(ui):
    status, body = ui.post("/api/lease/start", {"hours": 2})
    assert status == 400
    assert ui.outbox_events() == []


def test_extend_lease_endpoint_invokes_core_and_appends_event(ui):
    status, result = ui.post("/api/lease/extend", {"profile": "workload", "hours": 2})
    assert status == 200
    assert result["action"] == "extended"
    assert "extend workload --hours 2" in ui.invocations()
    events = ui.outbox_events()
    assert len(events) == 1
    assert events[0]["kind"] == "lease-extended"
    assert events[0]["detail"]["profile"] == "workload"
    assert events[0]["detail"]["hours"] == 2
    assert events[0]["detail"]["old_deadline"] == "2026-11-02T00:00:00+00:00"
    assert events[0]["detail"]["new_deadline"] == "2026-11-02T02:00:00+00:00"


def test_extend_grant_endpoint_invokes_core_and_appends_event(ui):
    status, result = ui.post("/api/grant/extend", {"hours": 4})
    assert status == 200
    assert result["action"] == "grant-extended"
    assert "extend-grant --hours 4" in ui.invocations()
    events = ui.outbox_events()
    assert len(events) == 1
    assert events[0]["kind"] == "grant-extended"
    assert events[0]["detail"]["hours"] == 4
    assert events[0]["detail"]["old_until"] == "2026-11-02T00:00:00+00:00"
    assert events[0]["detail"]["new_until"] == "2026-11-02T04:00:00+00:00"


def test_extend_refusals_are_failures_with_events(tmp_path):
    """A core refusal (exit 1 + refusal action) is classified as a
    failure by is_success and lands in the outbox as the *-failed
    kind; the refusal body still passes through to the page so the
    reason surfaces."""
    server = UiServer(tmp_path, extra_env={"STUB_EXTEND_REFUSE": "1"})
    try:
        status, result = server.post("/api/lease/extend", {"profile": "workload", "hours": 2})
        assert status == 200
        assert result["action"] == "refused-budget"
        status, result = server.post("/api/grant/extend", {"hours": 2})
        assert status == 200
        assert result["action"] == "grant-inactive"
        assert [e["kind"] for e in server.outbox_events()] == ["lease-extend-failed", "grant-extend-failed"]
    finally:
        server.close()


def test_extend_validation(ui):
    assert ui.post("/api/lease/extend", {"hours": 2})[0] == 400
    assert ui.post("/api/lease/extend", {"profile": "workload"})[0] == 400
    assert ui.post("/api/lease/extend", {"profile": "workload", "hours": -1})[0] == 400
    assert ui.post("/api/grant/extend", {})[0] == 400
    assert ui.post("/api/grant/extend", {"hours": 0})[0] == 400
    assert ui.invocations() == []
    assert ui.outbox_events() == []


def test_server_restart_spawns_ui_sh_detached(tmp_path):
    """POST /api/server/restart answers immediately with
    restart-requested and spawns `ui.sh restart` detached (via the
    COLAB_LEASE_UI_SH override); the answering server itself is not
    blocked or stopped by the spawn."""
    sh_log = tmp_path / "ui-sh.log"
    stub_sh = tmp_path / "stub_ui.sh"
    stub_sh.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{sh_log}"\n')
    stub_sh.chmod(0o755)
    server = UiServer(tmp_path, extra_env={"COLAB_LEASE_UI_SH": str(stub_sh)})
    try:
        status, result = server.post("/api/server/restart", {})
        assert status == 200
        assert result == {"action": "restart-requested"}
        # the detached spawn lands without the request waiting on it
        deadline = time.time() + 5
        while time.time() < deadline:
            if sh_log.exists() and "restart" in sh_log.read_text():
                break
            time.sleep(0.1)
        assert sh_log.exists() and sh_log.read_text().splitlines() == ["restart"]
        # and this server is still alive and serving afterwards
        data = server.get_json("/api/state")
        assert data["records"][0]["profile"] == "workload"
        # a restart is not a lease event: nothing enters the outbox
        assert server.outbox_events() == []
    finally:
        server.close()


def test_reaper_spool_drains_into_the_outbox_once(ui):
    """Core-side events (lease-renewed, spooled by reap() into
    var/reaper-events.jsonl) are folded into the outbox by the
    server — its only writer — exactly once, and a line the writer
    has not finished appending is left for the next drain."""
    spool = ui.var / "reaper-events.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(json.dumps({"kind": "lease-renewed", "profile": "workload", "driver": 2, "old_session": "workload", "new_session": "workload-r123"}) + "\n")
    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["lease-renewed"]
    assert events[0]["detail"] == {"profile": "workload", "driver": 2, "old_session": "workload", "new_session": "workload-r123"}
    assert ui.outbox_events() == events  # drained once, never duplicated
    with spool.open("a") as fh:
        fh.write('{"kind": "lease-renewed", "profile": "l4"')  # unterminated
    assert ui.outbox_events() == events
    with spool.open("a") as fh:
        fh.write(', "driver": 1, "old_session": "l4", "new_session": "l4-r9"}\n')
    events2 = ui.outbox_events()
    assert [e["kind"] for e in events2] == ["lease-renewed", "lease-renewed"]
    assert events2[1]["detail"]["new_session"] == "l4-r9"


def test_envelope_state_passes_through(tmp_path):
    """With a grant active the core serves an envelope block (scope,
    snapshot caps, attributed leases); the server passes it through
    untouched for the grant card to render."""
    server = UiServer(tmp_path, extra_env={"STUB_GRANT": "1"})
    try:
        data = server.get_json("/api/state")
        env = data["envelope"]
        assert env["id"] == "2026-11-01T00:00:00+00:00"
        assert env["caps"] == {"daily": 10, "monthly": 100, "standing_daily": 2.0}
        assert env["scope"]["profiles"] == ["lobby", "workload"]
        assert env["records"][0]["profile"] == "workload"
        assert env["records"][0]["via"] == "xbrl-thread"
        assert data["grant"]["scope"]["caps"]["daily"] == 10
    finally:
        server.close()


def test_envelope_spool_kinds_drain_into_the_outbox(ui):
    """The envelope outbox kinds ride the same reaper-events drain as
    lease-renewed: spooled by the core, folded in by the server."""
    spool = ui.var / "reaper-events.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(
        json.dumps({"kind": "envelope-lease-started", "profile": "workload", "envelope_id": "2026-11-01T00:00:00+00:00", "via": "xbrl-thread"}) + "\n"
        + json.dumps({"kind": "envelope-lease-released", "profile": "workload", "envelope_id": "2026-11-01T00:00:00+00:00", "reason": "deadline"}) + "\n"
    )
    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["envelope-lease-started", "envelope-lease-released"]
    assert events[0]["detail"]["via"] == "xbrl-thread"
    assert events[1]["detail"]["reason"] == "deadline"


def test_frontend_renders_envelope():
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "envelope" in js  # grant card renders the served envelope block
    assert "Envelope leases" in js
    assert "Envelope caps (at grant)" in js
    assert "envelope-lease-started" in js
    assert "envelope-lease-released" in js


def test_budget_caps_endpoint_invokes_core_and_appends_event(ui):
    status, data = ui.post("/api/config/budget", {"daily_cu_budget": 25, "monthly_cu_budget": None})
    assert status == 200
    assert data["action"] == "caps-updated"
    assert data["caps"] == {"daily": 25, "monthly": None, "standing_daily": 2.0}
    assert data["previous"]["daily"] == 10
    assert "budget-caps --daily 25 --monthly none" in ui.invocations()
    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["budget-caps-changed"]
    assert events[0]["detail"]["caps"]["daily"] == 25


def test_budget_caps_endpoint_rejects_bad_bodies(ui):
    for payload in ({}, {"unknown_cap": 5}, {"daily_cu_budget": -1}, {"daily_cu_budget": "10"}, {"daily_cu_budget": True}):
        status, _ = ui.post("/api/config/budget", payload)
        assert status == 400, payload
    assert ui.invocations() == []  # nothing reached the core
    assert ui.outbox_events() == []


def test_budget_caps_core_refusal_is_a_400(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_CAPS_INVALID": "1"})
    try:
        status, data = server.post("/api/config/budget", {"daily_cu_budget": 5})
        assert status == 400
        assert data["action"] == "caps-invalid"
        assert server.outbox_events() == []
    finally:
        server.close()


def test_frontend_has_budget_cap_editor():
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "/api/config/budget" in js
    assert "budget-caps-changed" in js
    assert "Edit caps" in js
