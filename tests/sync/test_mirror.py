"""Mirror engine round-trip in a sandboxed HOME (COLAB_RUNNER_HOME)."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MIRROR = REPO / "ops" / "mirror_transient.py"


def run_mirror(op, home):
    env = dict(os.environ, COLAB_RUNNER_HOME=str(home))
    return subprocess.run([sys.executable, str(MIRROR), op, "--json"], capture_output=True, text=True, env=env)


def report_for(result, item_id):
    return next(r for r in json.loads(result.stdout) if r["id"] == item_id)


def test_capture_restore_and_newer_canonical_wins(tmp_path):
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"refresh_token": "v1-0123456789abcdef0123456789"}')
    mirror_token = REPO / "state" / "transient" / "colab-cli" / "token.json"
    backup = mirror_token.read_bytes() if mirror_token.exists() else None
    try:
        assert report_for(run_mirror("capture", home), "colab-token")["capture"] == "captured"
        token.unlink()
        assert report_for(run_mirror("restore", home), "colab-token")["restore"] == "restored"
        assert token.read_text() == '{"refresh_token": "v1-0123456789abcdef0123456789"}'
        assert oct(token.stat().st_mode)[-3:] == "600"
        # A newer canonical file is never clobbered and is captured on exit.
        token.write_text('{"refresh_token": "v2-0123456789abcdef0123456789"}')
        os.utime(token, (token.stat().st_atime, token.stat().st_mtime + 3600))
        assert report_for(run_mirror("restore", home), "colab-token")["restore"] == "canonical-newer-kept"
        assert token.read_text() == '{"refresh_token": "v2-0123456789abcdef0123456789"}'
        run_mirror("capture", home)
        assert mirror_token.read_text() == '{"refresh_token": "v2-0123456789abcdef0123456789"}'
    finally:
        if backup is None:
            mirror_token.unlink(missing_ok=True)
        else:
            mirror_token.write_bytes(backup)


VALID_TOKEN = '{"refresh_token": "good-0123456789abcdef0123456789"}'
STUB_TOKEN = '{"refresh_token": "stub15chars000"}'


def _token_paths(home):
    canonical = home / ".config" / "colab-cli" / "token.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    mirror = REPO / "state" / "transient" / "colab-cli" / "token.json"
    return canonical, mirror


def test_capture_heals_stub_canonical_from_valid_mirror(tmp_path):
    """2026-11-01: the interactive flow's stub overwrote the canonical
    token and a capture carried the stub into the mirror, destroying
    the last good copy. Capture must now heal instead of propagate."""
    home = tmp_path / "home"
    canonical, mirror = _token_paths(home)
    backup = mirror.read_bytes() if mirror.exists() else None
    try:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(VALID_TOKEN)
        canonical.write_text(STUB_TOKEN)
        assert report_for(run_mirror("capture", home), "colab-token")["capture"] == "canonical-healed-from-mirror"
        assert canonical.read_text() == VALID_TOKEN
    finally:
        if backup is None:
            mirror.unlink(missing_ok=True)
        else:
            mirror.write_bytes(backup)


def test_capture_never_propagates_stub(tmp_path):
    home = tmp_path / "home"
    canonical, mirror = _token_paths(home)
    backup = mirror.read_bytes() if mirror.exists() else None
    try:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(STUB_TOKEN)
        canonical.write_text(STUB_TOKEN)
        assert report_for(run_mirror("capture", home), "colab-token")["capture"] == "canonical-invalid-kept"
        assert mirror.read_text() == STUB_TOKEN
    finally:
        if backup is None:
            mirror.unlink(missing_ok=True)
        else:
            mirror.write_bytes(backup)


def test_restore_never_writes_invalid_mirror(tmp_path):
    home = tmp_path / "home"
    canonical, mirror = _token_paths(home)
    backup = mirror.read_bytes() if mirror.exists() else None
    try:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(STUB_TOKEN)
        # Even a much newer stub mirror must not win over a valid canonical.
        canonical.write_text(VALID_TOKEN)
        assert report_for(run_mirror("restore", home), "colab-token")["restore"] == "mirror-invalid-kept"
        assert canonical.read_text() == VALID_TOKEN
    finally:
        if backup is None:
            mirror.unlink(missing_ok=True)
        else:
            mirror.write_bytes(backup)
