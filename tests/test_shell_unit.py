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


def _plant_record(var, session, last_used):
    import json as _json

    (var / f"lease-{session}.json").write_text(_json.dumps({
        "session": session, "caller": "lease", "shape": "cpu-highram",
        "acquired_at": "2026-11-08T09:00:00+00:00", "last_used_at": last_used, "jobs": [],
    }))


def test_resolve_session_precedence(tmp_path, monkeypatch):
    # E9: the default resolves through the lease core's records —
    # the most recently used per-session record wins; the retired
    # var/lease.json is not a source (the legacy-name plant below
    # would have won under the old read).
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    _plant_record(tmp_path, "older-one", "2026-11-08T10:00:00+00:00")
    _plant_record(tmp_path, "newer-one", "2026-11-08T12:00:00+00:00")
    (tmp_path / "lease.json").write_text('{"session": "legacy-name"}')
    assert sh.resolve_session(None) == "newer-one"
    monkeypatch.setenv("COLAB_SH_SESSION", "from-env")
    assert sh.resolve_session(None) == "from-env"
    assert sh.resolve_session("from-flag") == "from-flag"


def test_resolve_session_ignores_the_retired_lease_json(tmp_path, monkeypatch):
    # E9 pin move: the legacy single-record file alone resolves to
    # nothing — it is deleted code, not a fallback.
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_SH_SESSION", raising=False)
    (tmp_path / "lease.json").write_text('{"session": "legacy-name"}')
    assert sh.resolve_session(None) is None


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


def run_main(monkeypatch, fake, argv, tmp_path, live_state=None):
    monkeypatch.setattr(sh.subprocess, "run", fake)
    # The main() scenarios below target a live session; the connect
    # guard's own refusal paths are pinned by the tests after them.
    # The fake takes the budget kwargs too: the async path passes
    # the reduced read budget through this seam.
    monkeypatch.setattr(sh, "live_state", live_state or (lambda session, **kw: "alive"))
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

# --- Sync claim gate + detached mode (sync-channel design) ---

import json

REFUSAL = {"blocked_by": "claim", "holder": "alpha",
           "evidence": [{"kind": "claim", "claimed_at": "2026-11-08T15:00:00+00:00", "expires_at": "2026-11-08T16:00:00+00:00"}],
           "expires_at": "2026-11-08T16:00:00+00:00"}


def test_main_refused_by_the_sync_gate_before_connecting(tmp_path, monkeypatch, capsys):
    """The gate runs before any ssh invocation: a refusal exits 2,
    names the holder and the expiry, and prints the detached form
    of the refused command."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun()
    monkeypatch.setattr(sh, "sync_gate", lambda session, caller: REFUSAL)
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--caller", "lease", "echo", "hi"], tmp_path)
    assert rc == 2
    assert all(cmd[0] != "ssh" for cmd, _ in fake.calls)
    err = capsys.readouterr().err
    assert "'alpha'" in err and "2026-11-08T16:00:00+00:00" in err
    assert "--detach" in err and "echo" in err


def test_detached_alternative_renders_the_callers_own_command():
    import argparse as _ap

    args = _ap.Namespace(dir="/content", env=[], file=None, command=["echo", "hi there"])
    out = sh.detached_alternative(args, "alpha", "lease", [])
    assert out.startswith("colab-sh --detach -s alpha --caller lease")
    assert "echo" in out and "'hi there'" in out


def test_detach_launch_returns_an_id_and_plants_the_script(tmp_path, monkeypatch, capsys):
    """The fake-world detach round trip, launch half: one ssh call
    whose stdin is the launcher (nohup, the sync-detached dir, the
    caller's script base64-planted), and a JSON receipt with the
    new id on stdout."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun(rc=0)
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--detach", "echo hi"], tmp_path)
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out.strip())
    assert receipt["state"] == "running" and receipt["id"]
    cmd, kw = fake.calls[-1]
    assert cmd[0] == "ssh"
    launcher = kw["input"]
    assert "sync-detached" in launcher and "nohup" in launcher
    import base64 as _b64

    assert _b64.b64encode(b"echo hi\n").decode() in launcher


class StdoutRun(FakeRun):
    def __init__(self, stdout, rc=0):
        super().__init__(rc=rc)
        self.stdout = stdout

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.rc, stdout=self.stdout)


def test_poll_round_trip_states(tmp_path, monkeypatch, capsys):
    """The poll half: the probe's framed output parses into the
    verdict JSON — running exits 0, done exits with the job's own
    exit code, an unknown id is a not-found 2."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    meta = '{"id": "x1", "session": "alpha", "caller": "lease", "started_at": "2026-11-08T15:00:00+00:00", "state": "running"}'
    running = StdoutRun(meta + "\nSTATE running\nEXIT -\n---LOG---\nworking\n")
    rc = run_main(monkeypatch, running, ["-s", "alpha", "--poll", "x1"], tmp_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["state"] == "running" and out["log_tail"] == "working\n"
    done = StdoutRun(meta + "\nSTATE done\nEXIT 7\n---LOG---\nfinished\n")
    rc = run_main(monkeypatch, done, ["-s", "alpha", "--poll", "x1"], tmp_path)
    assert rc == 7
    out = json.loads(capsys.readouterr().out.strip())
    assert out["state"] == "done" and out["exit_code"] == 7
    missing = StdoutRun("NOT-FOUND\n")
    rc = run_main(monkeypatch, missing, ["-s", "alpha", "--poll", "nope"], tmp_path)
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert out["state"] == "not-found"


# --- Duration cap, --long, busy verdicts, budgets (E3/E4/E15) ---

from datetime import datetime, timedelta, timezone

BUSY = (255, "", "ssh: connect: 429 Too Many Requests")


class SeqRun(FakeRun):
    """Answers ssh from a scripted sequence of (rc, stdout, stderr).
    Non-ssh subprocess calls (the mirror restore) succeed silently
    without consuming the sequence."""

    def __init__(self, results):
        super().__init__(rc=0)
        self.results = list(results)

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[0] != "ssh":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        rc, out, err = self.results.pop(0) if self.results else (0, "", "")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


def _ssh_calls(fake):
    return [(c, k) for c, k in fake.calls if c[0] == "ssh"]


def _detached_path(did):
    return REPO / "results" / "detached" / f"{did}.json"


def test_over_cap_sync_refuses_before_any_dial(tmp_path, monkeypatch, capsys):
    """E3 + E15(a): an over-cap sync call refuses on local reads
    alone — the liveness dial is never consulted, ssh never runs,
    and the refusal names the cap and prints the detached form."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    dials = []

    def counting(session, **kw):
        dials.append(session)
        return "alive"

    fake = FakeRun()
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--timeout", "99999", "echo", "hi"], tmp_path, live_state=counting)
    assert rc == 2
    assert dials == [] and _ssh_calls(fake) == []
    err = capsys.readouterr().err
    assert "max_sync_seconds" in err and "--detach" in err


def test_gate_refusal_makes_zero_dials(tmp_path, monkeypatch):
    """E15(a): the claim gate is evaluated before the liveness
    dial on the sync path — a gate refusal never pays for one."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    dials = []

    def counting(session, **kw):
        dials.append(session)
        return "alive"

    monkeypatch.setattr(sh, "sync_gate", lambda session, caller: REFUSAL)
    fake = FakeRun()
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--caller", "lease", "echo", "hi"], tmp_path, live_state=counting)
    assert rc == 2 and dials == [] and _ssh_calls(fake) == []


def test_default_sync_timeout_is_the_cap(tmp_path, monkeypatch):
    """E3: the resolved sync timeout defaults to the cap (300),
    superseding the wrapper's old 900 default."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    fake = FakeRun(rc=0)
    assert run_main(monkeypatch, fake, ["-s", "alpha", "echo", "hi"], tmp_path) == 0
    ssh = _ssh_calls(fake)
    assert len(ssh) == 1 and ssh[0][1]["timeout"] == 300


def test_long_call_journals_the_hold_on_the_claim(tmp_path, monkeypatch):
    """E3: --long connects over the cap, and the hold lands on the
    claim with its bound — long_hold_until at now + the timeout,
    the claim's expiry extended to match."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun(rc=0)
    before = datetime.now(timezone.utc)
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--caller", "lease", "--long", "--timeout", "18000", "echo", "hi"], tmp_path)
    assert rc == 0 and len(_ssh_calls(fake)) == 1
    claim = json.loads((tmp_path / "sync-claim-alpha.json").read_text())
    hold_until = datetime.fromisoformat(claim["long_hold_until"])
    assert hold_until > before + timedelta(seconds=3600)
    assert claim["expires_at"] == claim["long_hold_until"]
    lines = [json.loads(x) for x in (tmp_path / "caller-claims.jsonl").read_text().splitlines() if x.strip()]
    assert any(l["seam"] == "sync-long-hold" and l["caller"] == "lease" for l in lines)


def test_sync_busy_answer_becomes_a_named_verdict(tmp_path, monkeypatch, capsys):
    """E3: a bridge-busy failure is translated — channel busy,
    the claim holder and its expiry — never the raw ssh result.
    (The gate auto-claims for 'lease' first, so the claim the
    verdict names is the caller's own fresh claim.)"""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = SeqRun([BUSY])
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--caller", "lease", "echo", "hi"], tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    claim = json.loads((tmp_path / "sync-claim-alpha.json").read_text())
    assert "channel busy" in err and "'lease'" in err and claim["expires_at"] in err


def test_detach_launch_retries_busy_then_succeeds(tmp_path, monkeypatch, capsys):
    """E4: busy answers retry inside the wrapper — two busies and
    a success is a successful launch, three ssh attempts."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    monkeypatch.setattr(sh, "BUSY_BACKOFF_S", (0, 0))
    fake = SeqRun([BUSY, BUSY, (0, "", "")])
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--detach", "echo hi"], tmp_path)
    assert rc == 0 and len(_ssh_calls(fake)) == 3
    did = json.loads(capsys.readouterr().out.strip())["id"]
    _detached_path(did).unlink(missing_ok=True)


def test_detach_launch_busy_exhaustion_is_a_named_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    monkeypatch.setattr(sh, "BUSY_BACKOFF_S", (0, 0))
    fake = SeqRun([BUSY, BUSY, BUSY])
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--detach", "echo hi"], tmp_path)
    assert rc == 2 and len(_ssh_calls(fake)) == 3
    assert "channel busy" in capsys.readouterr().err


def test_poll_retries_busy_then_reads(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    monkeypatch.setattr(sh, "BUSY_BACKOFF_S", (0, 0))
    meta = '{"id": "x1", "session": "alpha", "caller": "lease", "started_at": "2026-11-08T15:00:00+00:00", "state": "running"}'
    fake = SeqRun([BUSY, (0, meta + "\nSTATE running\nEXIT -\n---LOG---\nworking\n", "")])
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--poll", "x1"], tmp_path)
    assert rc == 0 and len(_ssh_calls(fake)) == 2


def test_async_path_uses_the_reduced_read_budget(tmp_path, monkeypatch, capsys):
    """E4: detach/poll classify at the reap budget (2 x 45 s);
    the sync path keeps the standard budget."""
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    calls = []

    def recording(session, attempts=None, timeout=120):
        calls.append((session, attempts, timeout))
        return "alive"

    var2 = tmp_path / "v2"
    var2.mkdir()
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    fake = FakeRun(rc=0)
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--detach", "echo hi"], tmp_path, live_state=recording)
    assert rc == 0 and calls == [("alpha", 2, 45)]
    did = json.loads(capsys.readouterr().out.strip())["id"]
    _detached_path(did).unlink(missing_ok=True)
    calls.clear()
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(var2))
    assert run_main(monkeypatch, FakeRun(rc=0), ["-s", "alpha", "echo", "hi"], tmp_path, live_state=recording) == 0
    assert calls == [("alpha", None, 120)]


def test_liveness_cache_serves_a_burst_and_a_stale_cache_redials(tmp_path, monkeypatch):
    """E4: five successive attaches pay one liveness dial; once
    the cached verdict is stale, the next attach dials again."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    dials = []

    def counting(session, **kw):
        dials.append(session)
        return "alive"

    for _ in range(5):
        assert run_main(monkeypatch, FakeRun(rc=0), ["-s", "alpha", "echo", "hi"], tmp_path, live_state=counting) == 0
    assert dials == ["alpha"]
    cache_path = tmp_path / "liveness-alpha.json"
    data = json.loads(cache_path.read_text())
    data["colab_sh_cache"]["at"] = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    cache_path.write_text(json.dumps(data))
    assert run_main(monkeypatch, FakeRun(rc=0), ["-s", "alpha", "echo", "hi"], tmp_path, live_state=counting) == 0
    assert dials == ["alpha", "alpha"]


def test_mirror_restore_skips_while_fresh(tmp_path, monkeypatch, capsys):
    """E4: the mirror restore runs once; the wrapper's freshness
    stamp skips it for the next attach inside the window."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun(rc=0)
    dids = []
    for _ in range(2):
        assert run_main(monkeypatch, fake, ["-s", "alpha", "--detach", "echo hi"], tmp_path) == 0
        dids.append(json.loads(capsys.readouterr().out.strip())["id"])
    restores = [c for c, _ in fake.calls if any("mirror_transient" in str(part) for part in c)]
    assert len(restores) == 1
    for did in dids:
        _detached_path(did).unlink(missing_ok=True)


def test_orphan_advisory_names_the_adopt_verb(tmp_path, monkeypatch, capsys):
    """E1(a): a live target with no lease record draws the
    advisory — the state named, the adopt command printed — and
    the attach proceeds."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    fake = FakeRun(rc=0)
    assert run_main(monkeypatch, fake, ["-s", "alpha", "echo", "hi"], tmp_path) == 0
    err = capsys.readouterr().err
    assert "no lease record (orphaned)" in err and "bin/colab-lease adopt alpha" in err


# --- Detached registration (E8) and local waits (E13) ---


def test_detach_launch_registers_and_poll_unregisters(tmp_path, monkeypatch, capsys):
    """E8: the launch writes the local detached record in the
    run-record vocabulary and registers detached:<id> on the
    lease record's jobs; the poll that observes the end marks
    the record terminal and unregisters."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path))
    monkeypatch.delenv("COLAB_CALLER", raising=False)
    _plant_record(tmp_path, "alpha", "2026-11-08T12:00:00+00:00")
    fake = FakeRun(rc=0)
    rc = run_main(monkeypatch, fake, ["-s", "alpha", "--detach", "--caller", "lease", "echo hi"], tmp_path)
    assert rc == 0
    did = json.loads(capsys.readouterr().out.strip())["id"]
    try:
        rec = json.loads(_detached_path(did).read_text())
        assert rec["id"] == did and rec["session"] == "alpha" and rec["caller"] == "lease"
        assert rec["state"] == "running" and rec["exit_code"] is None and rec["finished_at"] is None
        record = json.loads((tmp_path / "lease-alpha.json").read_text())
        assert record["jobs"] == [f"detached:{did}"]
        meta = json.dumps({"id": did, "session": "alpha", "caller": "lease", "started_at": rec["started_at"], "state": "running"})
        done = StdoutRun(meta + "\nSTATE done\nEXIT 7\n---LOG---\nfinished\n")
        rc = run_main(monkeypatch, done, ["-s", "alpha", "--poll", did], tmp_path)
        assert rc == 7
        rec = json.loads(_detached_path(did).read_text())
        assert rec["state"] == "complete" and rec["exit_code"] == 7 and rec["finished_at"] is not None
        record = json.loads((tmp_path / "lease-alpha.json").read_text())
        assert record["jobs"] == []
    finally:
        _detached_path(did).unlink(missing_ok=True)


def _plant_detached(did, state, exit_code=None):
    path = _detached_path(did)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": did, "session": "alpha", "caller": "lease", "state": state,
        "exit_code": exit_code, "started_at": "2026-11-08T15:00:00+00:00",
        "finished_at": None if state == "running" else "2026-11-08T15:01:00+00:00",
    }))
    return path


def test_wait_reads_only_the_local_record(tmp_path, monkeypatch):
    """E13: --wait's exit codes come from the local detached
    record alone — the channel seams are rigged to explode, and
    a missing record is a failed outcome (1), a running one at
    the bound is 3."""
    monkeypatch.setattr(sh.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("channel used")))
    monkeypatch.setattr(sh, "live_state", lambda session, **kw: (_ for _ in ()).throw(AssertionError("dialed")))
    paths = [_plant_detached("waitdone1", "complete", 0), _plant_detached("waitfail1", "failed", 3), _plant_detached("waitrun1", "running")]
    try:
        assert sh.main(["--wait", "waitdone1"]) == 0
        assert sh.main(["--wait", "waitfail1"]) == 1
        assert sh.main(["--wait", "waitrun1", "--max", "0"]) == 3
        assert sh.main(["--wait", "waitgone1"]) == 1
    finally:
        for path in paths:
            path.unlink(missing_ok=True)

