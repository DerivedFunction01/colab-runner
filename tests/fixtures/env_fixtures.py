"""CLI environment builders for the fake-CLI suites.

The subprocess suites (lease fake, two-tier fake, auto fake,
runner fake) each built the same scratch-HOME + fixture-repo
environment inline; the builders live here once, parameterised
only where the suites actually differ (the auto-acquire flag,
the runner's extra env). Every builder points the binary under
test at a fixture repo from repo_fixture (authored config in
the test tree) — never the checkout's operator config.
"""

import os
from pathlib import Path

import repo_fixture

CHECKOUT = Path(__file__).resolve().parent.parent.parent
FAKE = CHECKOUT / "tests" / "fixtures" / "fake_colab.py"


def _scratch_home(tmp_path):
    # A scratch HOME with a dummy token: the post-start shape
    # assertion reads the local sessions binding under HOME, and
    # the real binding belongs to live sessions — a CLI test's
    # fresh create must never be judged against it. The empty
    # binding file also blocks the transient mirror's
    # restore-if-missing from planting the real binding here;
    # tests that exercise the assertion write their own binding.
    home = tmp_path / "home"
    token = home / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"refresh_token": "fake-test-token"}')
    (home / ".config" / "colab-cli" / "sessions.json").write_text("{}")
    return home


def lease_env(tmp_path):
    home = _scratch_home(tmp_path)
    # The CLI under test runs against a fixture repo (authored
    # config in the test tree): bin/colab-lease derives its repo
    # root from its own location, so the checkout's untracked
    # operator config must never be the config it reads.
    root = repo_fixture.cli_repo(tmp_path / "repo", tmp_path / "var")
    return dict(
        os.environ,
        HOME=str(home),
        TEST_REPO_ROOT=str(root),
        COLAB_BIN=str(FAKE),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        # Session shape gate: the suite's door is the whitelisted
        # test actor claim (the live config's session_gate block
        # lists it); gate-specific tests pop it to run as a plain
        # agent actor.
        COLAB_LEASE_ACTOR="test",
        # Keepalive: two instant cycles in the sandbox, then exit — an
        # unbounded loop here would leak a process onto this machine.
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
    )


def standing_env(tmp_path):
    """lease_env without the test actor claim: the standing duty's
    admission is the infra exemption (the infra actor + the
    standing marker on the standing shape), never a bypass."""
    env = lease_env(tmp_path)
    env.pop("COLAB_LEASE_ACTOR", None)
    return env


def auto_env(tmp_path, enabled):
    home = _scratch_home(tmp_path)
    # The CLI under test runs against a fixture repo (authored
    # config in the test tree), never the checkout's operator one.
    root = repo_fixture.cli_repo(tmp_path / "repo", tmp_path / "var")
    return dict(
        os.environ,
        HOME=str(home),
        TEST_REPO_ROOT=str(root),
        COLAB_BIN=str(FAKE),
        FAKE_COLAB_STATE=str(tmp_path / "fake"),
        COLAB_RUNNER_VAR=str(tmp_path / "var"),
        COLAB_RUNNER_AUTO_ACQUIRE="1" if enabled else "0",
        # Session shape gate: the suite's test actor claim (see
        # lease_env); gate tests pop it.
        COLAB_LEASE_ACTOR="test",
        KEEPALIVE_MAX_CYCLES="2",
        KEEPALIVE_INTERVAL_SECONDS="0",
        KEEPALIVE_ALLOW_NONCOLAB="1",
    )


FAKE_ENV_KEEPALIVE = dict(KEEPALIVE_MAX_CYCLES="2", KEEPALIVE_INTERVAL_SECONDS="0", KEEPALIVE_ALLOW_NONCOLAB="1")

# Session shape gate: the suite's test actor claim (see
# lease_env) — without it the runner's acquire is
# an agent actor and caller attribution refuses the fresh start.
FAKE_ENV_GATE = dict(COLAB_LEASE_ACTOR="test")


def runner_env(tmp_path, **extra):
    # A scratch HOME, exactly as lease_env builds: without it the
    # fake CLI writes its session bindings into the real
    # ~/.config/colab-cli/sessions.json (the 2026-11-05 pollution
    # that blinded the lease machinery to live sessions).
    home = _scratch_home(tmp_path)
    # The runner under test runs against a fixture repo (authored
    # config in the test tree): bin/colab-run derives its repo root
    # from its own location, so its lease reads and its run records
    # land in the fixture, never the checkout.
    root = repo_fixture.cli_repo(tmp_path / "repo", tmp_path / "var")
    return dict(os.environ, HOME=str(home), TEST_REPO_ROOT=str(root), COLAB_BIN=str(FAKE), FAKE_COLAB_STATE=str(tmp_path / "fake"), COLAB_RUNNER_VAR=str(tmp_path / "var"), **FAKE_ENV_KEEPALIVE, **FAKE_ENV_GATE, **extra)
