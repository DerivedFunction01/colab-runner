"""Lease unit tests: session_state classification and read
resilience, with stub colab callables (no subprocess)."""

import subprocess

from unit_harness import cp, gate_permissive, lease, repo  # noqa: F401


def test_session_state_auth_prompt_is_unknown_not_absent(tmp_path, monkeypatch):
    """2026-11-01: with the credential dead, the CLI printed its
    interactive login prompt where a session list was expected. The
    prompt is not a list: the state is unknown, never absent — absent
    is the one reading that deletes records."""
    monkeypatch.setenv("COLAB_RUNNER_VAR", str(tmp_path / "var"))
    prompt = lambda *a, **k: cp("Enter the authorization code:")  # noqa: E731
    assert lease.session_state(prompt, "standard") == "unknown"


# --- session_state read resilience (2026-11-07 classification defect) ---


def test_session_state_retries_a_transient_failure(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired("colab", 120)
        return cp("lilac-reef-93 some-host")

    diag = {}
    assert lease.session_state(flaky, "lilac-reef-93", diag) == "alive"
    assert calls["n"] == 2


def test_session_state_unknown_only_after_exhausted_retries(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))

    def dead(*a, **k):
        raise subprocess.TimeoutExpired("colab", 120)

    diag = {}
    assert lease.session_state(dead, "lilac-reef-93", diag) == "unknown"
    assert diag["attempts"] == 3 and "TimeoutExpired" in diag["read_error"]


def test_session_state_empty_output_retries_then_unknown(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    diag = {}
    assert lease.session_state(lambda *a, **k: cp(""), "lilac-reef-93", diag) == "unknown"
    assert diag == {"attempts": 3, "read_error": "empty-output"}


def test_session_state_auth_prompt_is_not_retried(monkeypatch):
    monkeypatch.setattr(lease.sessions, "_SESSION_READ_BACKOFF_S", (0, 0))
    calls = {"n": 0}

    def prompt(*a, **k):
        calls["n"] += 1
        return cp("Enter the authorization code:")

    diag = {}
    assert lease.session_state(prompt, "lilac-reef-93", diag) == "unknown"
    assert calls["n"] == 1 and diag["read_error"] == "auth-prompt"
