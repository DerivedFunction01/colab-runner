"""Unit tests for the venue payload contract fixtures
(tests/fixtures/).

The fixtures are fictional reference payloads: a preflight that
asserts every component a held session needs, and a probe's pure
reporting helpers. They pin the generic hosting contract —
component reporting that never goes silent, registry venv
resolution, browser revisions derived from package metadata with
a real launch probe, checkout HEAD reads, planted-extract shape
checks, and value-free probe line shapes.

The production payloads are workload code owned by their lane,
tracked in their repo with their workload-specific tests beside
them; these fixtures are shape-only and carry fictional markers
on purpose, so nothing here is the contract of record for a real
payload — only for the shapes the runner hosts.
"""

import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_loader(
        name, SourceFileLoader(name, str(FIXTURES / filename))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vp = _load("venue_preflight_fixture", "venue_preflight_fixture.py")
ph = _load("probe_hold_fixture", "probe_hold_fixture.py")


# --- preflight fixture: pure shape checks ------------------------------


def test_extract_shape_accepts_required_instance_unbound():
    data = {"instances": [{"id": "portal", "credential_set": "portal"}, {"id": "other"}]}
    ok, _detail = vp.extract_shape(data)
    assert ok


def test_extract_shape_rejects_forbidden_binding():
    data = {"instances": [{"id": "portal", "egress": "home_forward"}]}
    ok, detail = vp.extract_shape(data)
    assert not ok
    assert "egress" in detail


def test_extract_shape_rejects_missing_and_malformed():
    assert not vp.extract_shape({"instances": [{"id": "other"}]})[0]
    assert not vp.extract_shape({"instances": "nope"})[0]
    assert not vp.extract_shape(["not", "a", "dict"])[0]


def test_expected_browser_dirs_derives_revisions():
    browsers = {
        "browsers": [
            {"name": "chromium", "revision": "9911"},
            {"name": "chromium-headless-shell", "revision": "9911"},
            {"name": "firefox", "revision": "1234"},
            {"name": "webkit", "revision": "5678"},
        ]
    }
    assert vp.expected_browser_dirs(browsers) == [
        ("chromium", "chromium-9911"),
        ("chromium-headless-shell", "chromium_headless_shell-9911"),
    ]
    assert vp.expected_browser_dirs({}) == []


def test_registry_venv_resolution(tmp_path):
    content = tmp_path / "content"
    clone = content / "alpha"
    venv_python = clone / "var" / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    registry = {"alpha": {"repo": "alpha", "venv_python": "var/venv/bin/python"}}
    resolved, _detail = vp.registry_venv(registry, content, "alpha")
    assert resolved == venv_python
    assert vp.registry_venv({}, content, "alpha")[0] is None
    assert vp.registry_venv({"alpha": {"venv_python": ""}}, content, "alpha")[0] is None
    absolute = {"alpha": {"venv_python": str(venv_python)}}
    assert vp.registry_venv(absolute, content, "alpha")[0] == venv_python


# --- preflight fixture: checks over a fabricated tree -------------------


def _fake_interpreter(path: Path, exit_code: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo "3.13.0"; exit %d; fi\nexit 0\n'
        % exit_code
    )
    path.chmod(0o755)


def test_check_venv_pass_and_fail(tmp_path):
    repo = tmp_path / "alpha"
    interpreter = repo / "var" / "venv" / "bin" / "python"
    _fake_interpreter(interpreter, 0)
    ok, detail = vp.check_venv(repo)
    assert ok and "3.13.0" in detail
    _fake_interpreter(interpreter, 1)
    assert not vp.check_venv(repo)[0]
    assert not vp.check_venv(tmp_path / "absent")[0]


def test_run_checks_reports_every_component(tmp_path):
    content = tmp_path / "content"
    repo = content / "alpha"
    _fake_interpreter(repo / "var" / "venv" / "bin" / "python", 1)
    config = repo / "var" / "config"
    config.mkdir(parents=True)
    (config / "instances.json").write_text(
        json.dumps({"instances": [{"id": "portal", "credential_set": "portal"}]})
    )
    (content / "env-registry.json").write_text(
        json.dumps({"alpha": {"repo": "alpha", "venv_python": "var/venv/bin/python"}})
    )
    results = vp.run_checks(
        repo_dir=repo,
        registry_path=content / "env-registry.json",
        content_root=content,
        repo_name="alpha",
        browsers_root=tmp_path / "ms-playwright",
    )
    by_name = {name: (ok, detail) for name, ok, detail in results}
    assert list(by_name) == [
        "checkout_head",
        "venv_interpreter",
        "chromium_revision",
        "extracts_venue_shaped",
        "code_file_seam",
        "env_registry",
    ]
    # The fabricated tree satisfies these three outright.
    assert by_name["extracts_venue_shaped"][0]
    assert by_name["code_file_seam"][0]
    assert by_name["env_registry"][0]
    # No git repo, a failing interpreter, and no browsers.json:
    # all three FAIL, and all three are still reported.
    assert not by_name["checkout_head"][0]
    assert not by_name["venv_interpreter"][0]
    assert not by_name["chromium_revision"][0]


def _fabricate_browser_tree(tmp_path, probe_exit: int):
    repo = tmp_path / "alpha"
    interpreter = repo / "var" / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        '#!/bin/sh\n'
        'case "$2" in\n'
        '  *launch*) echo "launch ok"; exit %d ;;\n'
        '  *) echo "3.13.0"; exit 0 ;;\n'
        'esac\n' % probe_exit
    )
    interpreter.chmod(0o755)
    package = (
        repo / "var" / "venv" / "lib" / "python3.13" / "site-packages"
        / "playwright" / "driver" / "package" / "browsers.json"
    )
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"browsers": [
        {"name": "chromium", "revision": "9911"},
        {"name": "chromium-headless-shell", "revision": "9911"},
    ]}))
    browsers_root = tmp_path / "ms-playwright"
    (browsers_root / "chromium-9911").mkdir(parents=True)
    (browsers_root / "chromium_headless_shell-9911").mkdir(parents=True)
    return repo, browsers_root


def test_check_chromium_requires_a_launchable_browser(tmp_path):
    """Presence is not launchability: an image can carry the
    bundle but lack its system libraries, so the check probes a
    real launch."""
    repo, browsers_root = _fabricate_browser_tree(tmp_path / "ok", 0)
    ok, detail = vp.check_chromium(repo, browsers_root)
    assert ok and "launchable" in detail
    repo, browsers_root = _fabricate_browser_tree(tmp_path / "bad", 1)
    ok, detail = vp.check_chromium(repo, browsers_root)
    assert not ok and "cannot launch" in detail


def test_check_checkout_reads_head_sha(tmp_path):
    repo = tmp_path / "alpha"
    repo.mkdir()

    def git(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *argv], capture_output=True, text=True
        )

    git("init", "-q")
    (repo / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    commit = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=probe@example.invalid",
            "-c", "user.name=probe",
            "-c", "core.fsync=none",
            "commit", "-q", "-m", "seed",
        ],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        pytest.skip("git commit unavailable in this environment")
    sha = git("rev-parse", "HEAD").stdout.strip()
    ok, detail = vp.check_checkout(repo)
    assert ok and sha in detail
    assert not vp.check_checkout(tmp_path / "absent")[0]


# --- probe fixture: pure helpers ----------------------------------------


def test_parse_window_end():
    parsed = ph.parse_window_end("2026-11-07T10:30:00Z")
    assert (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute) == (
        2026, 11, 7, 10, 30,
    )
    assert parsed.tzinfo is not None
    assert ph.parse_window_end("2026-11-07T10:30:00") == parsed
    assert ph.parse_window_end("2026-11-07T12:30:00+02:00") == parsed
    with pytest.raises(ValueError):
        ph.parse_window_end("")
    with pytest.raises(ValueError):
        ph.parse_window_end("not-a-time")


def test_map_verify_verdict():
    assert ph.map_verify_verdict("live") == "signed_in"
    assert ph.map_verify_verdict("dead") == "dropped"
    assert ph.map_verify_verdict("unverified") == "error"
    assert ph.map_verify_verdict("anything-else") == "error"


def test_touch_line_shape():
    line = ph.touch_line("2026-11-07T02:10:00+00:00", "signed_in", "/portal/home")
    payload = json.loads(line)
    assert payload == {
        "ts": "2026-11-07T02:10:00+00:00",
        "verdict": "signed_in",
        "landing": "/portal/home",
    }
    dropped = json.loads(ph.touch_line("2026-11-07T03:10:00+00:00", "dropped", None))
    assert dropped["landing"] is None


def test_outcome_line_shape():
    report = {
        "login": "signed_in",
        "hold_started": "2026-11-07T02:00:00+00:00",
        "touches": 51,
        "ended_by": "window",
        "last_landing": "/portal/home",
    }
    line = ph.outcome_line(report)
    assert line.startswith(ph.OUTCOME_PREFIX)
    assert json.loads(line[len(ph.OUTCOME_PREFIX):]) == report


def test_destination_class_is_value_free():
    text = "Your verification code was sent to someone@Example.INVALID for sign-in"
    result = ph.destination_class(text)
    assert result["channel"] == "email"
    assert result["domain"] == "example.invalid"
    assert "someone" not in json.dumps(result)
    assert ph.destination_class("Your verification code was sent via text message")["channel"] == "phone"
    assert ph.destination_class("nothing relevant") == {
        "channel": "unknown",
        "line_found": False,
    }
