"""bin/colab-run's auth guard, exercised end to end.

2026-11-01: a failed token refresh dropped the Colab CLI into its
interactive login flow mid-automation; the flow overwrote the stored
token with a stub and printed its prompt where callers expected data,
and a live lease's record was deleted under the junk reading. The
guard in bin/colab-run's colab() is the fix: never invoke the CLI on
an invalid token, snapshot + atomically restore around every call,
and turn any prompt-carrying result into an auth-required refusal.

The fake CLI here is an executable on PATH, NOT $COLAB_BIN — the
guard is deliberately inert under COLAB_BIN so the fake-CLI suites
keep their old behaviour.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

VALID_TOKEN = '{"refresh_token": "good-0123456789abcdef0123456789", "client_id": "x"}'
STUB_TOKEN = '{"refresh_token": "stub15chars000"}'

DRIVER = """
import importlib.util, json, sys
from importlib.machinery import SourceFileLoader
spec = importlib.util.spec_from_loader("colab_run", SourceFileLoader("colab_run", sys.argv[1]))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
try:
    r = cr.colab("usage", timeout=int(sys.argv[2]))
    print(json.dumps({"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}))
except Exception as e:
    print(json.dumps({"raised": type(e).__name__}))
"""

FAKE = """#!/bin/sh
echo called >> "$FAKE_LOG"
case "$FAKE_MODE" in
  clobber)
    printf '%s' '{"refresh_token": "stub15chars000"}' > "$HOME/.config/colab-cli/token.json"
    echo "Enter the authorization code:"
    exit 0 ;;
  hang)
    printf '%s' '{"refresh_token": "stub15chars000"}' > "$HOME/.config/colab-cli/token.json"
    sleep 30 ;;
  bind-truncate)
    : > "$HOME/.config/colab-cli/sessions.json"
    echo "Current balance: 100 compute units"
    exit 0 ;;
  bind-delete)
    rm -f "$HOME/.config/colab-cli/sessions.json"
    echo "Current balance: 100 compute units"
    exit 0 ;;
  bind-rewrite)
    printf '%s' '{"other-session": {"name": "other-session"}}' > "$HOME/.config/colab-cli/sessions.json"
    echo "Current balance: 100 compute units"
    exit 0 ;;
  healthy)
    echo "Current balance: 100 compute units"
    exit 0 ;;
esac
"""

BINDING = '{"dawn-test-1": {"name": "dawn-test-1", "endpoint": "m-hm-test"}}'


def setup_env(tmp_path, mode, token_text, binding_text=None):
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True)
    if token_text is not None:
        token.write_text(token_text)
    if binding_text is not None:
        (home / ".config" / "colab-cli" / "sessions.json").write_text(binding_text)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake = fakebin / "colab"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{fakebin}:/usr/bin:/bin",
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        FAKE_MODE=mode,
        FAKE_LOG=str(tmp_path / "fake-calls.log"),
    )
    env.pop("COLAB_BIN", None)
    return env, driver, token, tmp_path / "var" / "auth-required.json", tmp_path / "fake-calls.log"


def run_driver(env, driver, timeout_s=2):
    r = subprocess.run([sys.executable, str(driver), str(REPO / "bin" / "colab-run"), str(timeout_s)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_clobbering_call_is_blocked_and_token_restored(tmp_path):
    env, driver, token, marker, _log = setup_env(tmp_path, "clobber", VALID_TOKEN)
    out = run_driver(env, driver)
    assert out["rc"] == 3
    assert out["stderr"].startswith("colab auth required")
    assert out["stdout"] == ""
    assert token.read_text() == VALID_TOKEN
    assert json.loads(marker.read_text())["reason"]


def test_invalid_token_means_cli_is_never_invoked(tmp_path):
    env, driver, token, marker, log = setup_env(tmp_path, "healthy", STUB_TOKEN)
    out = run_driver(env, driver)
    assert out["rc"] == 3
    assert not log.exists()
    assert token.read_text() == STUB_TOKEN
    assert marker.exists()


def test_timed_out_flow_still_restores_token(tmp_path):
    env, driver, token, _marker, _log = setup_env(tmp_path, "hang", VALID_TOKEN)
    out = run_driver(env, driver, timeout_s=2)
    assert out["raised"] == "TimeoutExpired"
    assert token.read_text() == VALID_TOKEN


def test_binding_truncated_by_call_is_restored(tmp_path):
    """2026-11-13: the CLI rewrites sessions.json non-atomically;
    a write denied after the truncate left 0 bytes and every
    recorded session read absent. The funnel restores its
    snapshot whenever a call leaves the binding unparseable."""
    env, driver, _token, _marker, _log = setup_env(tmp_path, "bind-truncate", VALID_TOKEN, BINDING)
    out = run_driver(env, driver)
    assert out["rc"] == 0
    sessions = tmp_path / "home" / ".config" / "colab-cli" / "sessions.json"
    assert sessions.read_text() == BINDING


def test_binding_deleted_by_call_is_restored(tmp_path):
    env, driver, _token, _marker, _log = setup_env(tmp_path, "bind-delete", VALID_TOKEN, BINDING)
    out = run_driver(env, driver)
    assert out["rc"] == 0
    sessions = tmp_path / "home" / ".config" / "colab-cli" / "sessions.json"
    assert sessions.read_text() == BINDING


def test_binding_rewritten_valid_is_the_clis_own_view(tmp_path):
    """A rewrite that still parses is the CLI's validated view
    (it drops entries it rejects): the guard never overrides it."""
    env, driver, _token, _marker, _log = setup_env(tmp_path, "bind-rewrite", VALID_TOKEN, BINDING)
    out = run_driver(env, driver)
    assert out["rc"] == 0
    sessions = tmp_path / "home" / ".config" / "colab-cli" / "sessions.json"
    assert json.loads(sessions.read_text()) == {"other-session": {"name": "other-session"}}


def test_healthy_call_clears_marker(tmp_path):
    env, driver, token, marker, _log = setup_env(tmp_path, "healthy", VALID_TOKEN)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"noted_at": "2026-11-01T13:05:58+00:00", "reason": "earlier"}')
    out = run_driver(env, driver)
    assert out["rc"] == 0
    assert "balance" in out["stdout"]
    assert not marker.exists()
    assert token.read_text() == VALID_TOKEN
