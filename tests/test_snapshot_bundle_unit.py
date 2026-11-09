"""The snapshot bundle must clone: it carries a HEAD naming
the snapshot commit, so a plain `git clone` materializes the
snapshot tree — the fetch-the-commit apply route in the
manifest is unchanged (2026-11-08: lanes found a bundle clone
checked out nothing, the bundle's only ref not being a HEAD
symref)."""

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_snapshot_bundle_clones_the_snapshot_tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("one\n")
    _git(src, "add", "-A")
    _git(src, "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(src, "update-ref", "refs/remotes/origin/main", "HEAD")
    (src / "b.txt").write_text("two\n")
    _git(src, "add", "-A")
    _git(src, "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "work")
    out = tmp_path / "snap.bundle"
    env = dict(os.environ, COLAB_REPO_SRC=str(src), BUNDLE_OUT=str(out))
    r = subprocess.run(["bash", str(REPO / "sync" / "make_snapshot_bundle.sh")], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads((tmp_path / "snap.bundle.manifest.json").read_text())
    clone = tmp_path / "clone"
    r = subprocess.run(["git", "clone", "-q", str(out), str(clone)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "b.txt").read_text() == "two\n"
    assert _git(clone, "rev-parse", "HEAD") == manifest["snapshot_commit"]
    assert _git(clone, "rev-parse", "HEAD^{tree}") == manifest["tree"]
