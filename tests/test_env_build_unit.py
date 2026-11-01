"""env_build.sh unit tests: Unit 0 uv resolution over fake HOME/PATH
fixtures, the no-bare-uv rule, the shell-canonical job route (Step 4),
and one functional fixture build — a fictional repo whose own
init_venv.py builds its venv, driven end to end by the shell build.
No network, no Colab: uv and the installer are stubs.
"""

import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_BUILD = REPO / "snippets" / "env_build.sh"
BASE_PATH = "/usr/bin:/bin"

FAKE_UV = """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "uv 9.9.9"; exit 0; fi
if [ "$1" = "venv" ]; then
  shift
  args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --python) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  exec python3 -m venv "${args[@]}"
fi
if [ "$1" = "pip" ]; then exit 0; fi
echo "fake uv: unhandled $*" >&2; exit 2
"""


def write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def run_print_uv(home: Path, path: str, extra_env=None):
    env = {"HOME": str(home), "PATH": path}
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(ENV_BUILD), "--print-uv"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().split("\n")[-1])


def test_print_uv_finds_home_candidate_in_path_stripped_shell(tmp_path):
    home = tmp_path / "home"
    uv = write_exe(home / ".local" / "bin" / "uv", FAKE_UV)
    out = run_print_uv(home, BASE_PATH)
    assert out == {"uv_path": str(uv), "uv_version": "9.9.9", "uv_install_route": "present"}


def test_print_uv_finds_cargo_candidate(tmp_path):
    home = tmp_path / "home"
    uv = write_exe(home / ".cargo" / "bin" / "uv", FAKE_UV)
    out = run_print_uv(home, BASE_PATH)
    assert out["uv_path"] == str(uv)
    assert out["uv_install_route"] == "present"


def test_print_uv_prefers_path_over_home(tmp_path):
    home = tmp_path / "home"
    write_exe(home / ".local" / "bin" / "uv", FAKE_UV)
    on_path = write_exe(tmp_path / "bin" / "uv", FAKE_UV)
    out = run_print_uv(home, f"{tmp_path / 'bin'}:{BASE_PATH}")
    assert out["uv_path"] == str(on_path)


def test_print_uv_installer_fallback(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "bin"
    installer = (
        "#!/usr/bin/env bash\n"
        "cat <<'EOS'\n"
        "mkdir -p \"$HOME/.local/bin\"\n"
        "printf '#!/usr/bin/env bash\\necho \"uv 8.8.8\"\\n' > \"$HOME/.local/bin/uv\"\n"
        "chmod +x \"$HOME/.local/bin/uv\"\n"
        "EOS\n"
    )
    write_exe(fakebin / "curl", installer)
    out = run_print_uv(home, f"{fakebin}:{BASE_PATH}")
    assert out["uv_install_route"] == "installer"
    assert out["uv_path"] == str(home / ".local" / "bin" / "uv")
    assert out["uv_version"] == "8.8.8"
    # Idempotent: a second run resolves the installed uv, no reinstall.
    out2 = run_print_uv(home, BASE_PATH)
    assert out2["uv_install_route"] == "present"


def test_no_bare_uv_invocation_in_builders():
    bare = re.compile(r'(?<![\w$".])uv\s+(pip|venv)\b')
    for rel in ("snippets/env_build.sh", "snippets/bootstrap_remote.py"):
        for line in (REPO / rel).read_text().split("\n"):
            if "command -v uv" in line:
                continue
            assert not bare.search(line), f"{rel}: bare uv invocation: {line}"
    boot = (REPO / "snippets" / "bootstrap_remote.py").read_text()
    assert '"uv",' not in boot and "['uv'" not in boot  # no bare-name argv
    assert '"--system"' not in boot  # the retired system install route (argv form)


def test_shell_canonical_route_step4():
    assert not (REPO / "snippets" / "env_build_job.py").exists()
    manifest = json.loads((REPO / "jobs" / "env-build" / "job.json").read_text())
    assert manifest["script"] == "snippets/shell_job.py"
    assert manifest["args"] == ["--script", "/content/env_build.sh"]
    assert manifest["units"] == 1


FIXTURE_INIT = '''import shutil, subprocess, sys
from pathlib import Path

uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
subprocess.run([uv, "venv", ".venv", "--python", sys.executable], check=True)
print("fixture venv built")
'''


def make_fixture(tmp_path):
    home = tmp_path / "home"
    write_exe(home / ".local" / "bin" / "uv", FAKE_UV)
    src = tmp_path / "alpha-src"
    src.mkdir()
    (src / "init_venv.py").write_text(FIXTURE_INIT)
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=src, check=True, capture_output=True)
    registry = {
        "default_repo": "alpha",
        "repos": {"alpha": {
            "clone_url": str(src), "local_path": str(src), "dest": "alpha",
            "venv": ".venv", "init": {"argv": ["init_venv.py"], "cwd": "clone"},
            "version_checks": {}, "scan_roots": [], "derive_resources": None,
            "variants": {},
        }},
    }
    reg_path = tmp_path / "repos.json"
    reg_path.write_text(json.dumps(registry))
    return home, reg_path


def run_build(tmp_path, *extra):
    home, reg_path = make_fixture(tmp_path)
    dest = tmp_path / "content" / "alpha"
    out = tmp_path / "out"
    env = {"HOME": str(home), "PATH": BASE_PATH}
    r = subprocess.run(
        ["bash", str(ENV_BUILD), "--registry", str(reg_path),
         "--dest", str(dest), "--out", str(out), *extra],
        capture_output=True, text=True, env=env, timeout=600)
    return r, dest, out


def test_functional_fixture_build(tmp_path):
    r, dest, out = run_build(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads((out / "env_report.json").read_text())
    assert report["repo"] == "alpha"
    assert report["uv_install_route"] == "present"
    assert report["init_route"] == "init_venv.py"
    assert report["venv_isolated"] is True
    assert report["venv_inherits_system_packages"] is False
    assert report["venv_rebuilt_legacy"] is False
    assert report["assertions_passed"] is True
    assert report["versions_fingerprint"]
    remote = json.loads((tmp_path / "content" / "env-registry.json").read_text())
    assert remote["alpha"]["repo"] == "alpha"
    assert remote["alpha"]["venv_python"] == str(dest / ".venv" / "bin" / "python")


def test_legacy_inherited_venv_is_pre_cleaned(tmp_path):
    home, reg_path = make_fixture(tmp_path)
    dest = tmp_path / "content" / "alpha"
    # A dest that already holds the clone plus a legacy inherited venv.
    subprocess.run(["git", "clone", "-q", str(tmp_path / "alpha-src"), str(dest)],
                   check=True, capture_output=True)
    legacy = dest / ".venv"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "pyvenv.cfg").write_text(
        "home = /usr\ninclude-system-site-packages = true\n")
    (legacy / "bin" / "pip").write_text("#!/bin/sh\n")
    out = tmp_path / "out"
    env = {"HOME": str(home), "PATH": BASE_PATH}
    r = subprocess.run(
        ["bash", str(ENV_BUILD), "--registry", str(reg_path), "--dest", str(dest),
         "--out", str(out), "--skip-clone"],
        capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads((out / "env_report.json").read_text())
    assert report["venv_rebuilt_legacy"] is True
    assert report["venv_isolated"] is True
    assert report["assertions_passed"] is True
