"""colab-sh unit tests with a stubbed subprocess (no network, no ssh).

Pins the shell channel's construction rules: the ProxyCommand goes
through the CLI venv python by absolute path (PATH is not trusted in
automation), --auth oauth2 is passed explicitly, the identity is the
ed25519 key, remote scripts arrive on stdin to `bash -s` with env
exports shlex-quoted ahead of them, and exit codes propagate (124 on
timeout).
"""

import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("colab_sh", SourceFileLoader("colab_sh", str(REPO / "bin" / "colab-sh")))
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)


def test_proxy_command_uses_cli_venv_python_and_oauth2():
    cmd = sh.proxy_command("alpha")
    assert str(sh.CLI_VENV_PYTHON) in cmd
    assert "-m colab_cli.cli" in cmd
    assert "--auth oauth2" in cmd
    assert "ssh --proxy-mode -s alpha" in cmd


def test_ssh_argv_shape():
    argv = sh.ssh_argv("alpha", "/content", ["--skip-system"], sh.DEFAULT_IDENTITY)
    assert argv[0] == "ssh"
    assert "root@colab-runtime" in argv
    assert str(sh.DEFAULT_IDENTITY) in argv
    assert "StrictHostKeyChecking=no" in argv
    remote = argv[-1]
    assert remote.startswith("cd /content && bash -s")
    assert "-- --skip-system" in remote


def test_build_stdin_quotes_env_values():
    script = "echo hi\n"
    out = sh.build_stdin(script, [("A", "plain"), ("B", "has space"), ("C", "it's")])
    lines = out.split("\n")
    assert lines[0] == "export A=plain"
    assert lines[1] == "export B='has space'"
    assert lines[2] == "export C='it'\"'\"'s'"
    assert out.endswith(script)
    assert sh.build_stdin(script, []) == script


def test_proxy_env_appends_suffixes_both_cases():
    env = sh.proxy_env({"NO_PROXY": "localhost"})
    assert env["NO_PROXY"] == "localhost,google.com,googleapis.com,googleusercontent.com,colab.dev"
    assert env["no_proxy"].endswith("colab.dev")


def test_resolve_session_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    (tmp_path / "lease.json").write_text('{"session": "from-record"}')
    assert sh.resolve_session(None) == "from-record"
    monkeypatch.setenv("COLAB_SH_SESSION", "from-env")
    assert sh.resolve_session(None) == "from-env"
    assert sh.resolve_session("from-flag") == "from-flag"


def test_resolve_session_none_without_any_source(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_SH_SESSION", raising=False)
    assert sh.resolve_session(None) is None


class FakeRun:
    def __init__(self, rc=0, exc=None):
        self.rc = rc
        self.exc = exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.rc)


def run_main(monkeypatch, fake, argv, tmp_path):
    monkeypatch.setattr(sh.subprocess, "run", fake)
    # The main() scenarios below target a live session; the connect
    # guard's own refusal paths are pinned by the tests after them.
    monkeypatch.setattr(sh, "live_state", lambda session: "alive")
    # main() refuses before connecting when the local identity or
    # the CLI venv python is absent; point both seams at scratch
    # files so the scenarios never depend on the real HOME's key
    # or this machine's uv tool install.
    identity = tmp_path / "id_ed25519"
    identity.write_text("fake-test-identity")
    venv_python = tmp_path / "venv-python"
    venv_python.write_text("")
    monkeypatch.setattr(sh, "DEFAULT_IDENTITY", identity)
    monkeypatch.setattr(sh, "CLI_VENV_PYTHON", venv_python)
    return sh.main(argv)


def test_main_propagates_remote_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    fake = FakeRun(rc=7)
    assert run_main(monkeypatch, fake, ["-s", "alpha", "exit 7"], tmp_path) == 7
    cmd, kw = fake.calls[-1]
    assert cmd[0] == "ssh"
    assert kw["input"].strip() == "exit 7"
    assert kw["env"]["NO_PROXY"].endswith("colab.dev")


def test_main_timeout_is_124(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    fake = FakeRun(exc=subprocess.TimeoutExpired(cmd=["ssh"], timeout=1))
    assert run_main(monkeypatch, fake, ["-s", "alpha", "--timeout", "1", "sleep 9"], tmp_path) == 124


def test_main_no_session_is_2(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_SH_SESSION", raising=False)
    fake = FakeRun()
    assert run_main(monkeypatch, fake, ["echo hi"], tmp_path) == 2
    assert fake.calls == [] or fake.calls[-1][0][0] != "ssh"


def test_main_refuses_a_dead_session_without_connecting(tmp_path, monkeypatch):
    """The connect guard: a target that does not read alive server-side
    is refused before any ssh invocation — the CLI's ssh flow would
    otherwise assign a fresh default-shape runtime for the dead name
    (the 2026-11-04 lane-probe mints)."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    fake = FakeRun()
    monkeypatch.setattr(sh.subprocess, "run", fake)
    monkeypatch.setattr(sh, "live_state", lambda session: "absent")
    assert sh.main(["-s", "alpha", "echo hi"]) == 2
    assert all(cmd[0] != "ssh" for cmd, _ in fake.calls)


def test_main_refuses_when_liveness_is_unknown(tmp_path, monkeypatch):
    """An unreadable sessions list is not a pass: the guard fails
    closed rather than risk minting a runtime."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    fake = FakeRun()
    monkeypatch.setattr(sh.subprocess, "run", fake)

    def boom(session):
        raise RuntimeError("list failed")

    monkeypatch.setattr(sh, "live_state", boom)
    assert sh.main(["-s", "alpha", "echo hi"]) == 2
    assert all(cmd[0] != "ssh" for cmd, _ in fake.calls)


# --- Caller claims (caller-attribution plan, Step 2) --
# Resolution runs against the repo's real registry: only machinery
# callers are safe to name here (thread callers live in the local
# untracked registry and must never leak into tests).


def test_claim_caller_flag_env_and_refusal(monkeypatch):
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    assert sh.claim_caller("lease") == ("lease", None)
    assert sh.claim_caller("lease:lane-1") == ("lease:lane-1", None)
    assert sh.claim_caller(None) == (None, None)
    caller, err = sh.claim_caller("bogus-name-zzz")
    assert caller is None and err is not None and "bogus-name-zzz" in err
    monkeypatch.setenv("COLAB_CALLER", "panel")
    assert sh.claim_caller(None) == ("panel", None)
    assert sh.claim_caller("lease") == ("lease", None)  # flag over env


def test_main_journals_the_attach_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun(rc=0)
    assert run_main(monkeypatch, fake, ["-s", "alpha", "--caller", "lease", "echo hi"], tmp_path) == 0
    import json as _json
    lines = [_json.loads(x) for x in (tmp_path / "caller-claims.jsonl").read_text().splitlines() if x.strip()]
    assert lines[-1]["session"] == "alpha" and lines[-1]["caller"] == "lease" and lines[-1]["seam"] == "colab-sh"


def test_main_refuses_an_unregistered_caller_before_connecting(tmp_path, monkeypatch):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun()
    assert run_main(monkeypatch, fake, ["-s", "alpha", "--caller", "bogus-name-zzz", "echo hi"], tmp_path) == 2
    assert all(cmd[0] != "ssh" for cmd, _ in fake.calls)
