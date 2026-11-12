"""The plant-git-identity verb against the fake CLI
(2026-11-12): a session tree formed outside the seed path (a
key clone, a branch transport) gets the source repo's
configured identity planted repo-locally, read back, and
recorded on its own run record — beside the key plant."""

import json
import subprocess
import sys
from pathlib import Path

from runner_harness import run_verb, runner_env, runs_dir


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _source_repo(tmp_path, name="DL", email="dev@example.com", email_key=True):
    src = tmp_path / "source-repo"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    if name is not None:
        _git(src, "config", "user.name", name)
    if email_key:
        _git(src, "config", "user.email", email)
    return src


def _session_tree(env, session, tree):
    root = Path(env["FAKE_COLAB_STATE"]) / "sessions" / session / tree
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    return root


def test_verb_plants_reads_back_and_records(tmp_path):
    env = runner_env(tmp_path)
    src = _source_repo(tmp_path)
    tree = _session_tree(env, "fake-sess", "lane-tree")
    r = run_verb(env, "plant-git-identity", "--session", "fake-sess", "--repo", str(src), "--tree", "lane-tree", "--caller", "test")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert out["git_identity"] == {"name": "DL", "email": "dev@example.com"}
    assert out["plant"] == {"planted": True, "name": "DL", "email": "dev@example.com"}
    # The tree itself carries the pair, repo-locally.
    assert _git(tree, "config", "--local", "--get", "user.name") == "DL"
    assert _git(tree, "config", "--local", "--get", "user.email") == "dev@example.com"
    # And the plant is on its run record.
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec["job"] == "plant-git-identity" and rec["state"] == "complete"
    assert rec["session"] == "fake-sess" and rec["tree"] == "lane-tree"


def test_verb_refuses_an_incomplete_source_identity(tmp_path):
    env = runner_env(tmp_path)
    src = _source_repo(tmp_path, email_key=False)
    tree = _session_tree(env, "fake-sess", "lane-tree")
    r = run_verb(env, "plant-git-identity", "--session", "fake-sess", "--repo", str(src), "--tree", "lane-tree")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no complete repo-local identity" in r.stderr
    # Nothing was planted.
    probe = subprocess.run(["git", "-C", str(tree), "config", "--local", "--get", "user.name"], capture_output=True, text=True)
    assert probe.returncode != 0
