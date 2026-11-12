"""The git identity plant (2026-11-12): a tree the machinery
forms carries the source repo's configured identity — its
repo-local user.name/user.email — never the forming shell's
inheritance. Unit pins for the reader, the payload (including
a real commit made with an identity-free environment: the
plant, and only the plant, can be what the commit carries),
and the apply-bundle plant in seed mode."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "sync"))
import git_identity  # noqa: E402


def _git(cwd, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check, env=env)


def _repo(tmp_path, name=None, email=None, email_key=True):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    if name is not None:
        _git(src, "config", "user.name", name)
    if email_key:
        _git(src, "config", "user.email", email if email is not None else "")
    return src


def test_reader_returns_the_repo_local_pair(tmp_path):
    src = _repo(tmp_path, name="DL", email="dev@example.com")
    assert git_identity.repo_identity(src) == {"name": "DL", "email": "dev@example.com"}


def test_reader_treats_a_set_empty_email_as_configured(tmp_path):
    # The DL <> convention: the email key present with an
    # empty value is a configured identity, not an absence.
    src = _repo(tmp_path, name="DL", email="")
    assert git_identity.repo_identity(src) == {"name": "DL", "email": ""}


def test_reader_refuses_a_partial_pair(tmp_path):
    src = _repo(tmp_path, name="DL", email_key=False)
    assert git_identity.repo_identity(src) is None


def test_reader_refuses_a_non_repo(tmp_path):
    assert git_identity.repo_identity(tmp_path / "nope") is None


def test_payload_plants_and_a_clean_env_commit_carries_the_pair(tmp_path):
    """The acceptance shape in miniature: after the payload
    runs, a commit made with no identity anywhere in the
    environment (empty HOME, no GIT_* vars) still carries the
    planted pair as author AND committer — the plant is the
    only place it could have come from."""
    tree = tmp_path / "lane-tree"
    tree.mkdir()
    _git(tree, "init", "-q", "-b", "main")
    payload = tmp_path / "plant.py"
    payload.write_text(git_identity.plant_payload("lane-tree", "DL", "dev@example.com"))
    r = subprocess.run([sys.executable, str(payload)], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    verdict = git_identity.parse_plant_output(r.stdout)
    assert verdict == {"planted": True, "name": "DL", "email": "dev@example.com"}
    (tree / "f.txt").write_text("x\n")
    clean_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "empty-home")}
    _git(tree, "add", "-A", env=clean_env)
    _git(tree, "commit", "-qm", "test", env=clean_env)
    out = _git(tree, "log", "-1", "--format=%an|%ae|%cn|%ce").stdout.strip()
    assert out == "DL|dev@example.com|DL|dev@example.com"


def test_parse_reads_the_verdict_past_a_cli_preamble():
    stdout = "\n[colab] A new version of Colab CLI is available: 0.8.0 (current: 0.7.5)\n\n" + '{"planted": true, "name": "DL", "email": ""}\n'
    assert git_identity.parse_plant_output(stdout) == {"planted": True, "name": "DL", "email": ""}


def _snapshot_bundle(tmp_path):
    src = tmp_path / "snap-src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("one\n")
    _git(src, "add", "-A")
    _git(src, "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(src, "update-ref", "refs/remotes/origin/main", "HEAD")
    out = tmp_path / "snap.bundle"
    env = dict(os.environ, COLAB_REPO_SRC=str(src), BUNDLE_OUT=str(out))
    r = subprocess.run(["bash", str(REPO / "sync" / "make_snapshot_bundle.sh")], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def _apply(tmp_path, bundle, dest, *extra):
    r = subprocess.run(
        [sys.executable, str(REPO / "sync" / "apply_bundle_remote.py"), str(bundle), str(dest), "--origin-url", "https://example.invalid/x.git", *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_apply_seed_mode_plants_the_identity(tmp_path):
    bundle = _snapshot_bundle(tmp_path)
    dest = tmp_path / "seeded"
    out = _apply(tmp_path, bundle, dest, "--user-name", "DL", "--user-email", "")
    assert out["applied"] is True and out["seeded"] is True
    assert out["git_identity"] == {"planted": True, "name": "DL", "email": ""}
    assert _git(dest, "config", "--local", "--get", "user.name").stdout.strip() == "DL"
    assert _git(dest, "config", "--local", "--get", "user.email").stdout.strip() == ""


def test_apply_without_identity_flags_plants_nothing(tmp_path):
    bundle = _snapshot_bundle(tmp_path)
    dest = tmp_path / "seeded"
    out = _apply(tmp_path, bundle, dest)
    assert out["applied"] is True
    assert out["git_identity"] is None
    assert _git(dest, "config", "--local", "--get", "user.name", check=False).returncode != 0
