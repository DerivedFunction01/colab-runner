"""Lease UI supervision tests: the systemd unit template, the
install-unit renderer, the ui.sh delegation, and the watchdog
ladder's healthy paths — all against stubs in tmp dirs (no real
systemctl, no real /etc, no lease state).

The supervised shape is the sibling companion servers' pattern:
a tracked unit template rendered by install-unit.sh, systemd as
the supervisor (Restart=always), and a watchdog ladder that
reinstalls the unit when a VM replacement wipes /etc.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "ui"
UNIT_NAME = "colab-lease-ui.service"


def stub_systemctl(tmp_path: Path, active: bool = False) -> tuple[Path, Path]:
    """A systemctl stub that logs its argv and answers is-active
    from a state file other stubs/tests can flip."""
    log = tmp_path / "systemctl.log"
    state = tmp_path / "systemctl.state"
    if active:
        state.write_text("active")
    stub = tmp_path / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        f'state="$(cat "{state}" 2>/dev/null || true)"\n'
        'case "$1" in\n'
        '  is-active) [[ "$state" == "active" ]] ;;\n'
        f'  start|restart) echo active > "{state}" ;;\n'
        f'  stop) rm -f "{state}" ;;\n'
        "  *) true ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return stub, log


def env_for(tmp_path: Path, stub: Path, **extra) -> dict:
    env = dict(
        os.environ,
        COLAB_UI_UNIT_DIR=str(tmp_path / "units"),
        COLAB_UI_SYSTEMCTL=str(stub),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
    )
    env.update(extra)
    return env


def run(script: Path, *args, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_install_unit_renders_template_without_placeholders(tmp_path):
    stub, log = stub_systemctl(tmp_path)
    env = env_for(tmp_path, stub)
    result = run(UI / "install-unit.sh", env=env)
    assert result.returncode == 0, result.stderr
    unit = (tmp_path / "units" / UNIT_NAME).read_text()
    assert "@ROOT@" not in unit and "@PYTHON@" not in unit and "@HOME@" not in unit
    assert f"ExecStart=/usr/bin/python3 {REPO}/ui/server.py" in unit
    assert f"WorkingDirectory={REPO}" in unit
    assert "Restart=always" in unit
    assert f"StandardOutput=append:{REPO}/var/lease-ui.log" in unit
    calls = log.read_text()
    assert "daemon-reload" in calls
    # Idempotent: a second render changes nothing and says so.
    again = run(UI / "install-unit.sh", env=env)
    assert again.returncode == 0
    assert "already installed" in again.stdout


def test_ui_sh_delegates_to_systemd_when_unit_installed(tmp_path):
    stub, log = stub_systemctl(tmp_path, active=False)
    env = env_for(tmp_path, stub)
    assert run(UI / "install-unit.sh", env=env).returncode == 0
    log.write_text("")

    status = run(UI / "ui.sh", "status", env=env)
    assert "not running" in status.stdout

    start = run(UI / "ui.sh", "start", env=env)
    assert start.returncode == 0, start.stderr
    assert "systemd" in start.stdout
    assert f"start {UNIT_NAME}" in log.read_text()
    # No nohup pidfile instance was spawned behind systemd's back.
    assert not (tmp_path / "var" / "lease-ui.pid").exists()

    status = run(UI / "ui.sh", "status", env=env)
    assert "running (systemd" in status.stdout

    stop = run(UI / "ui.sh", "stop", env=env)
    assert stop.returncode == 0
    assert f"stop {UNIT_NAME}" in log.read_text()


def test_watchdog_healthy_reinstalls_missing_unit_quietly(tmp_path):
    """EC-12 shape: the probe answers but /etc lost the unit —
    the ladder reinstalls it and exits 0 without a start."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Ok(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stub, log = stub_systemctl(tmp_path)
        env = env_for(
            tmp_path,
            stub,
            COLAB_UI_URL=f"http://127.0.0.1:{port}/",
            COLAB_UI_POLL_SECS="5",
        )
        result = run(UI / "watchdog.sh", env=env)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "units" / UNIT_NAME).exists()
        calls = log.read_text() if log.exists() else ""
        assert "start" not in calls
    finally:
        server.shutdown()
