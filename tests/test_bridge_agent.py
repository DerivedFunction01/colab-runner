"""Bridge protocol units: the agent's pure seams (command
validation + hashing, segmented log sequencing, registry
persistence) run off-VM — the agent module is imported from its
snippet path; only main() enforces the Colab-VM guard.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("bridge_agent", ROOT / "snippets" / "bridge_agent.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def signed(fields):
    out = dict(fields)
    out["sha256"] = bridge.command_hash(out)
    return out


def test_validate_accepts_signed_commands():
    for fields in (
        {"v": 1, "id": "a1", "verb": "run", "target": "self", "cmd": "echo hi"},
        {"v": 1, "id": "a2", "verb": "mint", "profile": "workload"},
        {"v": 1, "id": "a3", "verb": "stop", "name": "bridge-workload-x"},
        {"v": 1, "id": "a4", "verb": "ping"},
    ):
        cmd, reason = bridge.validate_command(signed(fields))
        assert cmd is not None and reason is None


def test_validate_refuses_tampered_and_partial():
    cmd = signed({"v": 1, "id": "a1", "verb": "run", "target": "self", "cmd": "echo hi"})
    cmd["cmd"] = "echo tampered"
    assert bridge.validate_command(cmd) == (None, "hash-mismatch")
    assert bridge.validate_command({"v": 1, "id": "a1", "verb": "run"})[1] in ("hash-mismatch",)
    partial = {"v": 1, "id": "a1", "verb": "run", "target": "self"}
    partial["sha256"] = bridge.command_hash(partial)
    assert bridge.validate_command(partial) == (None, "missing-cmd")
    assert bridge.validate_command({"v": 2, "id": "x", "verb": "ping", "sha256": "z"})[1] == "protocol-mismatch"
    assert bridge.validate_command(["not", "a", "dict"])[1] == "not-an-object"


def test_log_sequences_and_resumes(tmp_path):
    log = bridge.BridgeLog(tmp_path, "gen-1")
    a = log.write({"kind": "pong", "id": "1"})
    b = log.write({"kind": "pong", "id": "2"})
    assert (a["seq"], b["seq"]) == (1, 2)
    assert a["gen"] == "gen-1"
    resumed = bridge.BridgeLog(tmp_path, "gen-1")
    c = resumed.write({"kind": "pong", "id": "3"})
    assert c["seq"] == 3


def test_registry_round_trips(tmp_path):
    path = tmp_path / "registry.json"
    reg = bridge.Registry(path)
    reg.add_child("bridge-workload-1", {"profile": "workload", "minted_at": "2026-11-01T00:00:00+00:00", "last_used_at": "2026-11-01T00:00:00+00:00"})
    reg.mark_processed("cmd-1", 7)
    reg.mark_inflight("cmd-2")
    again = bridge.Registry(path)
    assert again.children["bridge-workload-1"]["profile"] == "workload"
    assert again.processed == {"cmd-1": 7}
    assert again.inflight == {"cmd-2"}
    assert again.remove_child("bridge-workload-1")["profile"] == "workload"
    again.clear_inflight("cmd-2")
    assert bridge.Registry(path).inflight == set()
    assert json.loads(path.read_text())["children"] == {}
    assert again.note_attempt("cmd-9") == 1
    assert again.note_attempt("cmd-9") == 2
    assert bridge.Registry(path).attempts == {"cmd-9": 2}


# --- Session shape gate: bridge minting (plan: .plans/2026-11-04-agent-session-shape-gate.md) --

PROFILES = {
    "workload": {"high_mem": True, "gpu": None, "tpu": None, "rate_cu_per_hour": 0.26},
    "standard": {"high_mem": False, "gpu": None, "tpu": None, "rate_cu_per_hour": 0.08},
    "t4": {"high_mem": False, "gpu": "T4", "tpu": None, "rate_cu_per_hour": None},
}


def make_agent(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(bridge, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(bridge, "LOGDIR", tmp_path / "log")
    monkeypatch.setattr(bridge, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(bridge, "REGISTRY_FILE", tmp_path / "registry.json")
    agent = bridge.Agent(cfg)
    agent.cli_calls = []

    def fake_cli(*args, timeout=600):
        agent.cli_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    agent._cli = fake_cli
    return agent


def log_lines(tmp_path):
    out = []
    for seg in sorted((tmp_path / "log").glob("bridge-*.jsonl")):
        out += [json.loads(line) for line in seg.read_text().splitlines() if line.strip()]
    return out


def test_mint_refused_for_standard_and_gpu_stamped(tmp_path, monkeypatch):
    cfg = {"session": "lobby", "generation": "gen-t", "limits": {"max_children": 3}, "profiles": PROFILES, "agent_allowed_profiles": ["workload"]}
    agent = make_agent(tmp_path, monkeypatch, cfg)
    agent.do_mint({"id": "m1", "verb": "mint", "profile": "standard"})
    agent.do_mint({"id": "m2", "verb": "mint", "profile": "t4"})
    assert agent.cli_calls == []  # refused before any CLI call
    assert agent.registry.children == {}
    errors = [line for line in log_lines(tmp_path) if line["kind"] == "error"]
    assert [e["error"] for e in errors] == ["shape-refused", "shape-refused"]
    assert [e["profile"] for e in errors] == ["standard", "t4"]
    agent.do_mint({"id": "m3", "verb": "mint", "profile": "workload"})
    assert [c for c in agent.cli_calls if c[0] == "new"]
    assert list(agent.registry.children) == ["bridge-workload-m3"]
    assert [line["kind"] for line in log_lines(tmp_path) if line["kind"] == "mint"] == ["mint"]


def test_mint_allowed_derived_when_stamp_absent(tmp_path, monkeypatch):
    # A bridge armed before the gate has no stamp: the agent derives
    # the same set from the snapshot's shapes.
    cfg = {"session": "lobby", "generation": "gen-t", "limits": {"max_children": 3}, "profiles": PROFILES}
    agent = make_agent(tmp_path, monkeypatch, cfg)
    assert agent.mint_allowed == {"workload"}
    agent.do_mint({"id": "m1", "verb": "mint", "profile": "standard"})
    assert agent.cli_calls == []
    assert [e["error"] for e in log_lines(tmp_path) if e["kind"] == "error"] == ["shape-refused"]
