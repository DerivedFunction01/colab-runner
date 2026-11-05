"""Lobby bridge core behaviour (plan: .plans/2026-11-01-lobby-bridge.md):
the shared command builder validates under the agent's own rules,
the duty installs under a grant and cools down, and an armed pass
ferries the log (mirror, outbox acknowledgement, re-plant) and
folds bridge-child segments into the attributed spend journal.
In-process against a dict-backed fake colab.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sync import lease  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("bridge_agent", ROOT / "snippets" / "bridge_agent.py")
agent_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_mod)

NOW = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)


class Fake:
    def __init__(self):
        self.remote: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def __call__(self, *args, timeout=None):
        self.calls.append(args)
        verb = args[0]
        if verb == "download":
            remote, local = args[3], args[4]
            if remote in self.remote:
                Path(local).parent.mkdir(parents=True, exist_ok=True)
                Path(local).write_bytes(self.remote[remote])
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        if verb == "upload":
            local, remote = args[3], args[4]
            self.remote[remote] = Path(local).read_bytes()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if verb == "exec":
            return SimpleNamespace(returncode=0, stdout="launched", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "var").mkdir(parents=True)
    (repo / "snippets").mkdir(parents=True)
    (repo / "config" / "lease.json").write_text(json.dumps({"enabled": True, "standing": {"profile": "lobby"}}))
    (repo / "config" / "profiles.json").write_text(json.dumps({
        "default_profile": "workload",
        "profiles": {
            "workload": {"session": "workload", "spec": "high-ram", "rate_cu_per_hour": 0.26, "high_mem": True},
            "lobby": {"session": "lobby", "spec": "standard", "rate_cu_per_hour": 0.08},
        },
    }))
    for name in ("bridge_agent.py", "_remote_guard.py"):
        (repo / "snippets" / name).write_text("# test stand-in\n")
    return repo


def seed_lobby(repo, now=NOW):
    rec = {"profile": "lobby", "session": "lobby", "acquired_at": now.isoformat(), "deadline": (now + timedelta(hours=3)).isoformat()}
    (repo / "var" / "lease-lobby.json").write_text(json.dumps(rec))
    return rec


def test_command_built_by_core_validates_in_agent():
    cmd = lease.bridge_command("run", target="self", cmd="echo hi")
    ok, reason = agent_mod.validate_command(cmd)
    assert ok is not None and reason is None
    cmd["cmd"] = "echo tampered"
    assert agent_mod.validate_command(cmd) == (None, "hash-mismatch")


def test_duty_no_grant(tmp_path):
    repo = make_repo(tmp_path)
    assert lease.bridge_duty(Fake(), repo, NOW) == {"bridge": "no-grant"}


def test_duty_installs_then_cools_down(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "colab-cli").mkdir(parents=True)
    (tmp_path / ".config" / "colab-cli" / "token.json").write_text("{}")
    lease.write_grant(repo, 3, now_dt=NOW)
    seed_lobby(repo)
    fake = Fake()
    out = lease.bridge_duty(fake, repo, NOW)
    assert out["bridge_install"] == "installed"
    assert "/content/bridge-kit/bridge_agent.py" in fake.remote
    assert "/content/bridge-kit/token.json" in fake.remote
    uploads = len([c for c in fake.calls if c[0] == "upload"])
    out2 = lease.bridge_duty(fake, repo, NOW + timedelta(seconds=30))
    assert out2["bridge_install"] == "cooldown"
    assert len([c for c in fake.calls if c[0] == "upload"]) == uploads


def test_install_stamps_agent_allowed_profiles(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "colab-cli").mkdir(parents=True)
    (tmp_path / ".config" / "colab-cli" / "token.json").write_text("{}")
    lease.write_grant(repo, 3, now_dt=NOW)
    seed_lobby(repo)
    out = lease.bridge_duty(Fake(), repo, NOW)
    assert out["bridge_install"] == "installed"
    cfg = json.loads((repo / "var" / "bridge-config.json").read_text())
    # Session shape gate: only the High-RAM profile is agent-mintable.
    assert cfg["agent_allowed_profiles"] == ["workload"]


def armed_setup(tmp_path):
    repo = make_repo(tmp_path)
    lease.write_grant(repo, 3, now_dt=NOW)
    rec = seed_lobby(repo)
    arm = {"session": "lobby", "acquired_at": rec["acquired_at"], "generation": "gen-test-1", "last_install_ts": NOW.timestamp()}
    (repo / "var" / "bridge-arm.json").write_text(json.dumps(arm))
    fake = Fake()
    minted = (NOW - timedelta(hours=1)).isoformat()
    lines = [
        {"v": 1, "gen": "gen-test-1", "seq": 1, "ts": minted, "kind": "mint", "id": "m1", "name": "bridge-workload-x1", "profile": "workload", "rate": 0.26},
        {"v": 1, "gen": "gen-test-1", "seq": 2, "ts": NOW.isoformat(), "kind": "result", "id": "r1", "target": "self", "rc": 0, "out": "hi"},
        {"v": 1, "gen": "gen-test-1", "seq": 3, "ts": NOW.isoformat(), "kind": "stop", "id": "s1", "name": "bridge-workload-x1", "profile": "workload", "minted_at": minted, "reason": "requested"},
    ]
    fake.remote["/content/bridge/log/bridge-000001.jsonl"] = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    status = {"v": 1, "gen": "gen-test-1", "ts": datetime.now(timezone.utc).isoformat(), "session": "lobby", "auth_down": False, "children": [], "queue": 0, "processed": 3, "log_segment": "bridge-000001.jsonl"}
    fake.remote["/content/bridge/status.json"] = json.dumps(status).encode()
    return repo, fake


def test_duty_armed_ferries_acks_and_folds(tmp_path):
    repo, fake = armed_setup(tmp_path)
    outbox = repo / "var" / "bridge-outbox"
    outbox.mkdir(parents=True)
    (outbox / "r1.json").write_text("{}")
    (outbox / "u1.json").write_text("{}")
    out = lease.bridge_duty(fake, repo, NOW)
    assert out["bridge"] == "armed" and out["mirrored"] == 3 and out["planted"] == 1
    mirror = (repo / "var" / "bridge-mirror.jsonl").read_text()
    assert '"kind": "generation"' in mirror and '"id": "r1"' in mirror
    assert not (outbox / "r1.json").exists()
    assert (outbox / "u1.json").exists()
    assert fake.remote["/content/bridge/queue/u1.json"] == b"{}"
    journal = [json.loads(x) for x in (repo / "var" / "lease-spend-journal.jsonl").read_text().splitlines()]
    assert len(journal) == 1 and journal[0]["session"] == "bridge-workload-x1" and journal[0]["profile"] == "workload"
    # A second pass mirrors nothing new and never double-folds.
    out2 = lease.bridge_duty(fake, repo, NOW)
    assert out2["mirrored"] == 0
    assert len((repo / "var" / "lease-spend-journal.jsonl").read_text().splitlines()) == 1


def test_bridge_block_serves_agent_status(tmp_path):
    repo, fake = armed_setup(tmp_path)
    # _bridge_block reads the wall clock (it is called from the state
    # assembly), so the grant must be live at wall-now: armed_setup's
    # grant is written against the file's frozen NOW for the duty
    # tests, which pass their clock in explicitly.
    lease.write_grant(repo, 3)
    block = lease._bridge_block(fake, repo)
    assert block["armed"] is True and block["generation"] == "gen-test" and block["children"] == []


def test_install_stamps_the_lobby_records_caller(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "colab-cli").mkdir(parents=True)
    (tmp_path / ".config" / "colab-cli" / "token.json").write_text("{}")
    lease.write_grant(repo, 3, now_dt=NOW)
    rec = seed_lobby(repo)
    rec["caller"] = "alpha"
    (repo / "var" / "lease-lobby.json").write_text(json.dumps(rec))
    out = lease.bridge_duty(Fake(), repo, NOW)
    assert out["bridge_install"] == "installed"
    cfg = json.loads((repo / "var" / "bridge-config.json").read_text())
    assert cfg["caller"] == "alpha"


def test_install_without_a_record_caller_stamps_the_machinery_default(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "colab-cli").mkdir(parents=True)
    (tmp_path / ".config" / "colab-cli" / "token.json").write_text("{}")
    lease.write_grant(repo, 3, now_dt=NOW)
    seed_lobby(repo)
    out = lease.bridge_duty(Fake(), repo, NOW)
    assert out["bridge_install"] == "installed"
    cfg = json.loads((repo / "var" / "bridge-config.json").read_text())
    assert cfg["caller"] == "lease"


def test_bridge_stop_line_caller_folds_into_the_spend_segment(tmp_path):
    repo, fake = armed_setup(tmp_path)
    key = "/content/bridge/log/bridge-000001.jsonl"
    lines = [json.loads(x) for x in fake.remote[key].decode().splitlines()]
    for line in lines:
        if line["kind"] == "stop":
            line["caller"] = "alpha"
    fake.remote[key] = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    out = lease.bridge_duty(fake, repo, NOW)
    assert out["bridge"] == "armed"
    journal = [json.loads(x) for x in (repo / "var" / "lease-spend-journal.jsonl").read_text().splitlines()]
    assert len(journal) == 1 and journal[0]["caller"] == "alpha"
