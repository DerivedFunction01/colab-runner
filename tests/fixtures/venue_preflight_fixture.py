"""Fictional preflight payload: a reference implementation of
the hosted-payload contract the runner's venue jobs follow —
assert, by name, every component a held session needs; one
PASS/FAIL line per component, all components reported even when
one fails, exit non-zero on any FAIL, repairs nothing.

This fixture is shape-only by design. The production payloads
are workload code owned by their lane and tracked in their own
repo; their workload-specific checks (a named instance's extract
shape, portal verdicts) are the contract of record there. What
this fixture pins is the generic skeleton those payloads share:
checkout HEAD reads, registry venv resolution, a venv whose
interpreter actually imports its browser stack, browser revisions
derived from the package's own metadata with a real launch probe
(presence is not launchability), a planted-extract shape check,
a creatable state seam, and a registry that resolves.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CONTENT_ROOT = Path("/content")
REPO_NAME = "acme-app"
REPO_DIR = CONTENT_ROOT / REPO_NAME
REGISTRY_PATH = CONTENT_ROOT / "env-registry.json"


def extract_shape(data, required_id="portal", forbidden_key="egress"):
    """A planted instances extract is well-shaped: the required
    instance is present and carries no forbidden binding key (an
    undeclared binding resolves to the default route; a forbidden
    binding fails closed at session open, so its presence here is
    a defect to catch early). Parameterised: each workload binds
    its own required id in its own payload."""
    if not isinstance(data, dict):
        return False, "instances extract is not a JSON object"
    instances = data.get("instances")
    if not isinstance(instances, list):
        return False, "instances extract carries no instances list"
    for instance in instances:
        if isinstance(instance, dict) and instance.get("id") == required_id:
            if forbidden_key in instance:
                return False, f"{required_id} instance carries a {forbidden_key} binding"
            return True, f"{required_id} instance present, no {forbidden_key} binding"
    return False, f"no {required_id} instance in the planted extract"


def expected_browser_dirs(browsers: dict) -> list[tuple[str, str]]:
    """(name, cache dir) for every Chromium-family entry in a
    Playwright browsers.json payload — revisions derived from the
    venv's own package metadata, never hardcoded here.
    """
    out: list[tuple[str, str]] = []
    entries = browsers.get("browsers") if isinstance(browsers, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        revision = entry.get("revision")
        if not name or not revision:
            continue
        if name == "chromium":
            out.append((name, f"chromium-{revision}"))
        elif name == "chromium-headless-shell":
            out.append((name, f"chromium_headless_shell-{revision}"))
    return out


def registry_venv(registry, content_root: Path, repo_name: str) -> tuple[Path | None, str]:
    """Resolve the registry's record for one repo dest to its venv
    interpreter path. The registry is keyed by dest (the clone's
    basename); a record's venv_python may be absolute or relative
    to the content root or the clone.
    """
    if not isinstance(registry, dict):
        return None, "env registry is not a JSON object"
    record = registry.get(repo_name)
    if not isinstance(record, dict):
        return None, f"no env-registry record for {repo_name}"
    venv_python = record.get("venv_python")
    if not venv_python:
        return None, "registry record carries no venv_python"
    raw = Path(str(venv_python))
    candidates = (
        [raw]
        if raw.is_absolute()
        else [content_root / raw, content_root / repo_name / raw]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate, f"venv_python {candidate}"
    return None, f"registry venv_python {venv_python} not found on the venue"


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def check_checkout(repo_dir: Path) -> tuple[bool, str]:
    if not repo_dir.is_dir():
        return False, f"checkout missing at {repo_dir}"
    try:
        proc = _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git rev-parse failed ({type(exc).__name__})"
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or len(sha) < 7:
        return False, "git rev-parse returned no HEAD sha"
    return True, f"HEAD {sha}"


def check_venv(repo_dir: Path) -> tuple[bool, str]:
    venv_python = repo_dir / "var" / "venv" / "bin" / "python"
    if not venv_python.is_file():
        return False, f"venv interpreter missing at {venv_python}"
    try:
        proc = _run(
            [
                str(venv_python),
                "-c",
                "import playwright, sys; print(sys.version.split()[0])",
            ]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"venv interpreter did not run ({type(exc).__name__})"
    if proc.returncode != 0:
        return False, "venv interpreter runs but playwright does not import"
    return True, f"python {(proc.stdout or '').strip()} + playwright importable"


def check_chromium(repo_dir: Path, browsers_root: Path) -> tuple[bool, str]:
    metadata = None
    for candidate in sorted(
        (repo_dir / "var" / "venv" / "lib").glob(
            "python*/site-packages/playwright/driver/package/browsers.json"
        )
    ):
        metadata = candidate
        break
    if metadata is None:
        return False, "no browsers.json under the repo venv's playwright package"
    try:
        browsers = json.loads(metadata.read_text())
    except (OSError, ValueError) as exc:
        return False, f"browsers.json unreadable ({type(exc).__name__})"
    expected = expected_browser_dirs(browsers)
    if not expected:
        return False, "browsers.json names no chromium-family browser"
    missing = [dirname for _name, dirname in expected if not (browsers_root / dirname).is_dir()]
    if missing:
        return False, "browser cache missing: " + ", ".join(missing)
    found = ", ".join(f"{name} ({dirname})" for name, dirname in expected)
    # Presence is not launchability: an image can carry the bundle
    # but lack its system libraries, so the browser cannot start.
    # Probe a real headless launch.
    venv_python = repo_dir / "var" / "venv" / "bin" / "python"
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    b = p.chromium.launch(headless=True)\n"
        "    pg = b.new_page(); pg.goto('about:blank'); b.close()\n"
        "print('launch ok')\n"
    )
    try:
        proc = _run([str(venv_python), "-c", probe], timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"chromium present ({found}) but the launch probe did not run ({type(exc).__name__})"
    if proc.returncode != 0 or "launch ok" not in (proc.stdout or ""):
        detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["no output"])[0]
        return False, f"chromium present ({found}) but cannot launch: {detail[:160]}"
    return True, f"expected revisions installed and launchable: {found}"


def check_extracts(repo_dir: Path) -> tuple[bool, str]:
    planted = repo_dir / "var" / "config" / "instances.json"
    if not planted.is_file():
        return False, f"planted instances extract missing at {planted}"
    try:
        data = json.loads(planted.read_text())
    except (OSError, ValueError) as exc:
        return False, f"planted instances extract unreadable ({type(exc).__name__})"
    return extract_shape(data)


def check_code_file_seam(repo_dir: Path) -> tuple[bool, str]:
    seam = repo_dir / "var" / "state" / "code-files"
    try:
        seam.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"code-file seam not creatable ({type(exc).__name__})"
    if not seam.is_dir():
        return False, "code-file seam path exists but is not a directory"
    return True, f"code-file seam present at {seam}"


def check_registry(registry_path: Path, content_root: Path, repo_name: str) -> tuple[bool, str]:
    if not registry_path.is_file():
        return False, f"env registry missing at {registry_path}"
    try:
        registry = json.loads(registry_path.read_text())
    except (OSError, ValueError) as exc:
        return False, f"env registry unreadable ({type(exc).__name__})"
    resolved, detail = registry_venv(registry, content_root, repo_name)
    if resolved is None:
        return False, detail
    return True, f"registry resolves {repo_name} to {detail}"


def run_checks(
    repo_dir: Path = REPO_DIR,
    registry_path: Path = REGISTRY_PATH,
    content_root: Path = CONTENT_ROOT,
    repo_name: str = REPO_NAME,
    browsers_root: Path | None = None,
) -> list[tuple[str, bool, str]]:
    if browsers_root is None:
        browsers_root = Path(
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            or (Path.home() / ".cache" / "ms-playwright")
        )
    checks = [
        ("checkout_head", lambda: check_checkout(repo_dir)),
        ("venv_interpreter", lambda: check_venv(repo_dir)),
        ("chromium_revision", lambda: check_chromium(repo_dir, browsers_root)),
        ("extracts_venue_shaped", lambda: check_extracts(repo_dir)),
        ("code_file_seam", lambda: check_code_file_seam(repo_dir)),
        ("env_registry", lambda: check_registry(registry_path, content_root, repo_name)),
    ]
    results: list[tuple[str, bool, str]] = []
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001 - a crashed check is a FAIL line, never silence
            ok, detail = False, f"check raised {type(exc).__name__}"
        results.append((name, ok, detail))
    return results


def main() -> int:
    results = run_checks()
    failed = 0
    for name, ok, detail in results:
        if not ok:
            failed += 1
        print(f"PREFLIGHT {name}: {'PASS' if ok else 'FAIL'} {detail}", flush=True)
    total = len(results)
    if failed:
        print(f"PREFLIGHT overall: FAIL ({failed} of {total} components failed)", flush=True)
        return 1
    print(f"PREFLIGHT overall: PASS ({total} components)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
