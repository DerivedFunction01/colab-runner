"""Lease UI server against a stub lease CLI (no login, no network,
no real lease state).

Covers the contract the page depends on: /api/state passes the core
JSON through (plus caps), mutations invoke bin/colab-lease with the
right argv and append the right outbox events with increasing seq,
and /api/outbox?after= filters without acknowledging.
"""

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STUB_SRC = REPO / "tests" / "fixtures" / "stub_lease_bin.py"
SERVER = REPO / "ui" / "server.py"

_fixture_spec = importlib.util.spec_from_loader("repo_fixture", SourceFileLoader("repo_fixture", str(REPO / "tests" / "fixtures" / "repo_fixture.py")))
repo_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(repo_fixture)


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
        # The server derives its repo root from its own location:
        # run a copy rooted at a fixture repo (authored config in
        # the test tree) so repo_config() serves the fixture caps,
        # never the checkout's operator config.
        root = tmp_path / "repo"
        ui_dir = root / "ui"
        ui_dir.mkdir(parents=True)
        shutil.copy(SERVER, ui_dir / "server.py")
        shutil.copy(REPO / "ui" / "config.json", ui_dir / "config.json")
        (ui_dir / "static").symlink_to(REPO / "ui" / "static")
        repo_fixture.write_config(root)
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
            [sys.executable, str(ui_dir / "server.py")],
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
    assert data["records"][0]["session"] == "amber-otter-42"
    assert data["records"][0]["record"]["session"] == "amber-otter-42"
    assert data["records"][0]["record"]["shape"] == "cpu-highram"
    assert data["records"][0]["action"] == "active"
    assert data["grant"] is None
    assert data["budget"]["balance"] == 571.7
    # The caps are the fixture repo's authored panel settings
    # (tests/fixtures/repo_fixture.py), compared against the
    # fixture constants, never the operator's live config.
    assert data["caps"]["daily_cu_budget"] == repo_fixture.FIXTURE_DAILY_CAP
    assert data["caps"]["monthly_cu_budget"] == repo_fixture.FIXTURE_MONTHLY_CAP
    assert "state" in ui.invocations()


def test_state_part_endpoints_serve_their_slice(ui):
    """Progressive loading: each portion endpoint invokes only its
    own core portion and returns only its slice (plus the repo-file
    config on the portions whose sections render it)."""
    records = ui.get_json("/api/state/records")
    assert records["records"][0]["session"] == "amber-otter-42"
    assert "budget" not in records and "specs" not in records

    specs = ui.get_json("/api/state/specs")
    assert {s["id"] for s in specs["specs"]} == {"cpu-highram", "cpu", "t4"}
    assert "profiles" not in specs
    assert "caps" in specs

    budget = ui.get_json("/api/state/budget")
    assert budget["budget"]["balance"] == 571.7
    assert "caps" in budget and "profiles" not in budget

    grant = ui.get_json("/api/state/grant")
    assert grant["grant"] is None and grant["envelope"] is None

    meta = ui.get_json("/api/state/meta")
    assert meta == {"bridge": None}

    invocations = ui.invocations()
    for part in ("records", "specs", "budget", "grant", "meta"):
        assert f"state --part {part}" in invocations
    assert "state" not in invocations  # the composed read was never used


def test_state_part_failure_is_isolated(tmp_path):
    """One portion's core failure 502s only its own endpoint."""
    server = UiServer(tmp_path, extra_env={"STUB_LEASE_FAIL": "state:records"})
    try:
        try:
            server.get("/api/state/records")
            raise AssertionError("records endpoint should have failed")
        except urllib.error.HTTPError as err:
            assert err.code == 502
        budget = server.get_json("/api/state/budget")
        assert budget["budget"]["balance"] == 571.7
    finally:
        server.close()


def test_frontend_loads_state_in_portions(ui):
    """The panel never makes the composed read: app.js polls the
    portion endpoints, and every section carries its own load-state
    line in the served page."""
    _, js = ui.get("/static/app.js")
    for part in ("records", "specs", "budget", "grant", "orphans"):
        assert f"/api/state/{part}" in js
    assert 'api("/api/state")' not in js
    _, html = ui.get("/")
    for part in ("records", "specs", "budget", "grant", "orphans", "logs"):
        assert f'data-part-status="{part}"' in html


def test_state_serves_specs_including_null_rate(ui):
    data = ui.get_json("/api/state")
    specs = {s["id"]: s for s in data["specs"]}
    assert set(specs) == {"cpu-highram", "cpu", "t4"}
    assert specs["cpu-highram"]["rate_cu_per_hour"] == 0.26
    assert specs["cpu-highram"]["high_mem"] is True
    assert specs["t4"]["gpu"] == "T4"
    # the null-rate entry is what drives the UI's "Cost unconfirmed"
    assert specs["t4"]["rate_cu_per_hour"] is None
    # every spec carries its catalog label
    assert specs["cpu-highram"]["label"] == "High-RAM CPU"
    assert specs["cpu"]["label"] == "Standard CPU"
    assert specs["t4"]["label"] == "T4 GPU"


def test_frontend_renders_specs_and_cost_unconfirmed():
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "specs" in js  # picker is rendered from state.specs
    assert "Cost unconfirmed" in js  # null-rate rendering path
    assert "label" in js  # spec rows and card titles render the label
    html = (REPO / "ui" / "static" / "index.html").read_text()
    assert 'id="burst-specs"' in html  # two-tier burst spec picker
    assert 'id="shape-specs"' in html  # normal-lease spec picker
    # the full duration picker from the mockup: presets to 3d + custom
    assert 'data-hours="72"' in html
    assert 'id="normal-custom"' in html
    assert 'id="grant-custom"' in html


def test_grant_passes_burst_shape(ui):
    status, result = ui.post("/api/grant", {"hours": 4, "burst": "t4"})
    assert status == 200 and result["action"] == "granted"
    assert "grant --hours 4 --burst t4" in ui.invocations()
    events = ui.outbox_events()
    assert events[0]["kind"] == "grant-started"
    assert events[0]["detail"]["burst"] == "t4"


def test_start_invokes_acquire_and_appends_event(ui):
    status, result = ui.post("/api/lease/start", {"shape": "cpu-highram", "hours": 2})
    assert status == 200
    assert result["action"] == "acquired"
    assert "acquire --shape cpu-highram --create --hours 2 --via panel --caller panel" in ui.invocations()
    events = ui.outbox_events()
    assert len(events) == 1
    assert events[0]["kind"] == "lease-started"
    assert events[0]["seq"] == 1
    assert events[0]["detail"]["shape"] == "cpu-highram"
    assert events[0]["detail"]["hours"] == 2


def test_start_carries_panel_actor_claim(ui):
    # Session shape gate: the server claims the panel actor in the
    # child env on every core call, so a panel start can ride the
    # core's bypass (env claim + --via panel together).
    status, _result = ui.post("/api/lease/start", {"shape": "cpu", "hours": 1})
    assert status == 200
    assert "acquire --shape cpu --create --hours 1 --via panel --caller panel" in ui.invocations()
    assert "actor:panel" in ui.invocations()


def test_stop_grant_ungrant_flow_and_seqs(ui):
    status, result = ui.post("/api/lease/stop", {"session": "amber-otter-42"})
    assert status == 200 and result["action"] == "released"
    status, result = ui.post("/api/grant", {"hours": 4})
    assert status == 200 and result["action"] == "granted"
    status, result = ui.post("/api/ungrant", {})
    assert status == 200 and result["action"] == "grant-ended"

    invocations = ui.invocations()
    assert "release amber-otter-42" in invocations
    assert "grant --hours 4" in invocations
    assert "ungrant" in invocations

    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["lease-stopped", "grant-started", "grant-ended"]
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_outbox_after_filters_without_acknowledging(ui):
    ui.post("/api/lease/start", {"shape": "cpu-highram", "hours": 1})
    ui.post("/api/lease/stop", {"session": "amber-otter-42"})

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
        status, _ = server.post("/api/lease/start", {"shape": "cpu-highram", "hours": 2})
        assert status == 502
        events = server.outbox_events()
        assert len(events) == 1
        assert events[0]["kind"] == "lease-start-failed"
        assert events[0]["detail"]["shape"] == "cpu-highram"
    finally:
        server.close()


def test_start_requires_shape(ui):
    status, body = ui.post("/api/lease/start", {"hours": 2})
    assert status == 400
    assert ui.outbox_events() == []


def test_extend_lease_endpoint_invokes_core_and_appends_event(ui):
    status, result = ui.post("/api/lease/extend", {"session": "amber-otter-42", "hours": 2})
    assert status == 200
    assert result["action"] == "extended"
    assert "extend amber-otter-42 --hours 2" in ui.invocations()
    events = ui.outbox_events()
    assert len(events) == 1
    assert events[0]["kind"] == "lease-extended"
    assert events[0]["detail"]["session"] == "amber-otter-42"
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
        status, result = server.post("/api/lease/extend", {"session": "amber-otter-42", "hours": 2})
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
    assert ui.post("/api/lease/extend", {"session": "amber-otter-42"})[0] == 400
    assert ui.post("/api/lease/extend", {"session": "amber-otter-42", "hours": -1})[0] == 400
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
        assert data["records"][0]["session"] == "amber-otter-42"
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
    spool.write_text(json.dumps({"kind": "lease-renewed", "session": "amber-otter-42", "shape": "cpu-highram", "driver": 2, "old_session": "amber-otter-42", "new_session": "amber-otter-42-r123"}) + "\n")
    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["lease-renewed"]
    assert events[0]["detail"] == {"session": "amber-otter-42", "shape": "cpu-highram", "driver": 2, "old_session": "amber-otter-42", "new_session": "amber-otter-42-r123"}
    assert ui.outbox_events() == events  # drained once, never duplicated
    with spool.open("a") as fh:
        fh.write('{"kind": "lease-renewed", "session": "l4"')  # unterminated
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
        assert env["scope"]["shapes"] == ["cpu", "cpu-highram"]
        assert env["records"][0]["session"] == "amber-otter-42"
        assert env["records"][0]["caller"] == "xbrl-thread"
        assert data["grant"]["scope"]["caps"]["daily"] == 10
    finally:
        server.close()


def test_records_serve_the_sync_claim_and_the_card_renders_it(tmp_path):
    """The records portion carries each lease's live sync claim
    (the state leaf serves it on the status entry); the server
    passes it through untouched, and the lease card renders the
    holder + expiry from the served field — nothing when the
    claim is absent (the default stub entry serves null)."""
    server = UiServer(tmp_path, extra_env={"STUB_SYNC_CLAIM": "1"})
    try:
        data = server.get_json("/api/state")
        entry = data["records"][0]
        assert entry["sync_claim"]["caller"] == "alpha"
        assert entry["sync_claim"]["expires_at"] == "2099-01-01T01:00:00+00:00"
    finally:
        server.close()
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "entry.sync_claim" in js and "Sync claim" in js


def test_envelope_spool_kinds_drain_into_the_outbox(ui):
    """The envelope outbox kinds ride the same reaper-events drain as
    lease-renewed: spooled by the core, folded in by the server."""
    spool = ui.var / "reaper-events.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(
        json.dumps({"kind": "envelope-lease-started", "session": "amber-otter-42", "shape": "cpu-highram", "envelope_id": "2026-11-01T00:00:00+00:00", "caller": "xbrl-thread"}) + "\n"
        + json.dumps({"kind": "envelope-lease-released", "session": "amber-otter-42", "shape": "cpu-highram", "envelope_id": "2026-11-01T00:00:00+00:00", "reason": "deadline"}) + "\n"
    )
    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["envelope-lease-started", "envelope-lease-released"]
    assert events[0]["detail"]["caller"] == "xbrl-thread"
    assert events[1]["detail"]["reason"] == "deadline"


def test_frontend_renders_envelope():
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "envelope" in js  # grant card renders the served envelope block
    assert "Envelope leases" in js
    assert "Envelope caps (at grant)" in js
    assert "envelope-lease-released" in js  # in the logs vocabulary (closed, 2026-11-03)


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
    assert "Edit caps" in js


def test_access_release_endpoint_and_outbox(ui):
    """Step 5: the one-tap release routes through the core verb; a
    released session lands in the outbox, a record-backed one is
    refused with the core's answer."""
    status, result = ui.post("/api/access/release", {"session": "stray-1"})
    assert status == 200 and result["action"] == "session-released"
    assert "release-session stray-1" in ui.invocations()
    events = ui.outbox_events()
    assert [e["kind"] for e in events] == ["session-released"]
    assert events[0]["detail"]["session"] == "stray-1"
    status, result = ui.post("/api/access/release", {"session": "recorded-session"})
    assert status == 409 and result["action"] == "session-has-record"
    status, _ = ui.post("/api/access/release", {})
    assert status == 400


def test_frontend_renders_access_list():
    js = (REPO / "ui" / "static" / "app.js").read_text()
    html = (REPO / "ui" / "static" / "index.html").read_text()
    assert 'id="access-list"' in html
    assert "renderAccess" in js and "/api/access/release" in js
    assert "data-access-release" in js


def test_state_orphans_part_served(ui):
    data = ui.get_json("/api/state/orphans")
    assert [o["session"] for o in data["orphans"]] == ["stray-fox-7"]
    assert data["orphans"][0]["derived_deadline"] == "2026-11-09T00:00:00+00:00"
    assert data["orphans"][0]["deadline_source"] == "renewal-config"
    assert "state --part orphans" in ui.invocations()


def test_state_orphans_failure_is_isolated(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_LEASE_FAIL": "state:orphans"})
    try:
        try:
            server.get("/api/state/orphans")
            raise AssertionError("orphans endpoint should have failed")
        except urllib.error.HTTPError as err:
            assert err.code == 502
        budget = server.get_json("/api/state/budget")
        assert budget["budget"]["balance"] == 571.7
    finally:
        server.close()


def test_adopt_endpoint_invokes_core_and_appends_event(ui):
    status, data = ui.post("/api/lease/adopt", {"session": "stray-fox-7"})
    assert status == 200 and data["action"] == "adopted"
    assert "adopt stray-fox-7 --caller panel" in ui.invocations()
    events = [e for e in ui.outbox_events() if e["kind"] == "lease-adopted"]
    assert len(events) == 1 and events[0]["detail"]["session"] == "stray-fox-7"


def test_adopt_endpoint_forwards_deadline_and_shape(ui):
    status, _ = ui.post("/api/lease/adopt", {"session": "stray-fox-7", "deadline": "2026-11-09T00:00:00+00:00", "shape": "cpu-highram"})
    assert status == 200
    assert "adopt stray-fox-7 --deadline 2026-11-09T00:00:00+00:00 --shape cpu-highram --caller panel" in ui.invocations()


def test_adopt_endpoint_refusal_is_409_with_event(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_ADOPT_REFUSE": "1"})
    try:
        status, data = server.post("/api/lease/adopt", {"session": "stray-fox-7"})
        assert status == 409 and data["action"] == "adopt-deadline-past"
        events = [e for e in server.outbox_events() if e["kind"] == "lease-adopt-failed"]
        assert len(events) == 1 and events[0]["detail"]["session"] == "stray-fox-7"
    finally:
        server.close()


def test_adopt_endpoint_core_failure_is_502(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_LEASE_FAIL": "adopt"})
    try:
        status, _ = server.post("/api/lease/adopt", {"session": "stray-fox-7"})
        assert status == 502
    finally:
        server.close()


def test_adopt_endpoint_validation(ui):
    status, _ = ui.post("/api/lease/adopt", {})
    assert status == 400
    status, _ = ui.post("/api/lease/adopt", {"session": "stray-fox-7", "deadline": "not-a-date"})
    assert status == 400
    status, _ = ui.post("/api/lease/adopt", {"session": "stray-fox-7", "hours": -1})
    assert status == 400
    status, _ = ui.post("/api/lease/adopt", {"session": "stray-fox-7", "deadline": "2026-11-09T00:00:00+00:00", "hours": 3})
    assert status == 400
    assert not any(line.startswith("adopt ") for line in ui.invocations())


def test_shorten_endpoint_invokes_core_and_appends_event(ui):
    status, data = ui.post("/api/lease/shorten", {"session": "amber-otter-42", "hours": 2})
    assert status == 200 and data["action"] == "shortened"
    assert "shorten amber-otter-42 --hours 2 --caller panel" in ui.invocations()
    events = [e for e in ui.outbox_events() if e["kind"] == "lease-shortened"]
    assert len(events) == 1 and events[0]["detail"]["session"] == "amber-otter-42"


def test_shorten_endpoint_refusal_is_409_with_event(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_SHORTEN_REFUSE": "1"})
    try:
        status, data = server.post("/api/lease/shorten", {"session": "amber-otter-42", "hours": 2})
        assert status == 409 and data["action"] == "shorten-not-earlier"
        events = [e for e in server.outbox_events() if e["kind"] == "lease-shorten-failed"]
        assert len(events) == 1 and events[0]["detail"]["session"] == "amber-otter-42"
    finally:
        server.close()


def test_shorten_endpoint_core_failure_is_502(tmp_path):
    server = UiServer(tmp_path, extra_env={"STUB_LEASE_FAIL": "shorten"})
    try:
        status, _ = server.post("/api/lease/shorten", {"session": "amber-otter-42", "hours": 2})
        assert status == 502
    finally:
        server.close()


def test_shorten_endpoint_validation(ui):
    status, _ = ui.post("/api/lease/shorten", {})
    assert status == 400
    status, _ = ui.post("/api/lease/shorten", {"session": "amber-otter-42"})
    assert status == 400
    status, _ = ui.post("/api/lease/shorten", {"session": "amber-otter-42", "deadline": "2026-11-09T00:00:00+00:00", "hours": 2})
    assert status == 400
    status, _ = ui.post("/api/lease/shorten", {"session": "amber-otter-42", "hours": -1})
    assert status == 400
    status, _ = ui.post("/api/lease/shorten", {"session": "amber-otter-42", "deadline": "not-a-date"})
    assert status == 400
    assert not any(line.startswith("shorten ") for line in ui.invocations())


def test_frontend_polls_orphans_portion_and_loads_module(ui):
    """The orphan census reaches the page as its own state portion
    and its renderer ships as a sibling module: app.js polls the
    portion, index.html carries the section (with its own
    load-state line) and loads orphans.js after app.js."""
    _, js = ui.get("/static/app.js")
    assert "/api/state/orphans" in js
    _, html = ui.get("/")
    assert 'id="slot-orphans"' in html
    assert 'id="orphan-cards"' in html
    assert "/static/orphans.js" in html


def test_frontend_renders_orphans_and_posts_adoptions():
    """orphans.js renders only the served census fields (derived
    deadline with its source and stale flag, conflicts, evidenced
    or candidate shape, creator caller, in-flight jobs, handover
    flag) and posts the adoption to the panel endpoint, carrying
    the picked shape for a class-ambiguous orphan."""
    js = (REPO / "ui" / "static" / "orphans.js").read_text()
    assert "renderOrphans" in js
    assert "/api/lease/adopt" in js
    for field in ("derived_deadline", "deadline_source", "deadline_stale_possible", "conflicts", "shape_candidates", "shape_evidenced", "creator_caller", "jobs_in_flight", "jobs_in_flight_entries", "handover_pending"):
        assert field in js, field
    assert "adopt-shape-ambiguous" in js  # refusal text reads the verdict


def test_frontend_orphan_jobs_expander_and_headline():
    """The orphan card's jobs truth: the count renders as a
    number from the served field, the kinded entries render
    only inside the keyed-state expander (never interpolated
    as a raw list), and a past-deadline headline is the
    deadline time with a past-deadline chip — carrying no
    data-countdown attribute for the ticker to overwrite."""
    js = (REPO / "ui" / "static" / "orphans.js").read_text()
    assert "orphanJobsOpen" in js  # open state keyed per session, outside the render
    assert "orphanJobsHTML" in js and "jobRowsHTML" in js
    assert "jobs_in_flight_entries" in js
    assert "past deadline" in js
    # The past branch builds the big line without data-countdown;
    # the future branch keeps it for the ticker.
    assert 'past\n      ? `<p class="big num">${esc(fmtTime(dl))}<span class="chip warn">past deadline</span></p>`' in js
    assert '`<p class="big num" data-countdown="${esc(dl)}">${esc(countdownText(dl))}</p>`' in js


def test_frontend_shorten_picker_beside_extend():
    """The lease cards carry a Shorten toggle beside Extend; the
    picker posts hours or an absolute deadline to the shorten
    endpoint through the verdict-returning helper and surfaces
    the core's refusal actions."""
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "/api/lease/shorten" in js
    assert "apiResult" in js
    assert "data-stoggle" in js and "data-sapply" in js
    for action in ("shorten-not-earlier", "shorten-deadline-past", "shorten-standing-managed"):
        assert action in js, action


def test_frontend_busy_labels_are_keyed_per_picker():
    """An in-flight extend or shorten paints its busy label only
    on the picker it was submitted from: the label map is keyed
    by the extend key / the shorten session key, each picker
    renders the label only for its own key, and each apply
    handler sets and clears its own key around the call — one
    session's action never shows progress on another's card."""
    js = (REPO / "ui" / "static" / "app.js").read_text()
    assert "const actBusy = {}" in js
    assert "actBusy[extKey(kind, key)]" in js
    assert "actBusy[`shorten:${session}`]" in js
    assert 'actBusy[k] = "Extending…"' in js
    assert 'actBusy[k] = "Shortening…"' in js


def test_deploy_syncs_the_orphans_module():
    """deploy.sh's client file list is the artifact's source set:
    a static file it does not name never reaches the published
    page, and check would flag the deployed copy as drift."""
    deploy = (REPO / "ui" / "deploy.sh").read_text()
    assert "orphans.js" in deploy
