"""Bridge protocol units: the agent's pure seams (command
validation + hashing, segmented log sequencing, registry
persistence) run off-VM — the agent module is imported from its
snippet path; only main() enforces the Colab-VM guard.
"""

import importlib.util
import json
from pathlib import Path

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
