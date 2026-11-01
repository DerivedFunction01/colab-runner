"""Repo-registry unit tests over fixture registries with fictional repos.

Pins the pointer-only contract (a package list in an entry is rejected —
specs live in each repo's own init_venv.py), default_repo resolution with
no hardcoded fallback, variant dest/delta naming, remote-registry merge
semantics, the version-spec evaluator, and the no-named-repo rule: the
tracked setup files must contain no literal naming a specific target
repo, and the consumer jobs must not hardcode a clone path.
"""

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("repo_registry", SourceFileLoader("repo_registry", str(REPO / "sync" / "repo_registry.py")))
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


def entry(**over):
    base = {
        "clone_url": "https://example.invalid/alpha.git",
        "local_path": "~/alpha",
        "dest": "alpha",
        "venv": ".venv",
        "init": {"argv": ["init_venv.py"], "cwd": "clone"},
        "version_checks": {"widgetdb": ">=1.5.6,<2.1"},
        "scan_roots": ["alpha_pkg", "tests"],
        "derive_resources": None,
        "variants": {"next": {"delta": ["widgetdb==2.0.0"]}},
    }
    base.update(over)
    return base


def registry(**over):
    data = {"default_repo": "alpha", "repos": {"alpha": entry(), "beta": entry(dest="beta")}}
    data.update(over)
    return data


def test_validate_accepts_pointer_only_entries():
    assert rr.validate_registry(registry())["default_repo"] == "alpha"


@pytest.mark.parametrize("key", ["packages", "package_list", "pkgs", "requirements", "dependencies", "spec"])
def test_package_list_field_is_rejected(key):
    bad = registry()
    bad["repos"]["alpha"] = entry(**{key: ["widgetdb"]})
    with pytest.raises(ValueError, match="never in the registry"):
        rr.validate_registry(bad)


def test_missing_required_pointer_is_rejected():
    bad = registry()
    del bad["repos"]["alpha"]["venv"]
    with pytest.raises(ValueError, match="missing"):
        rr.validate_registry(bad)


def test_get_entry_default_and_explicit():
    reg = registry()
    assert rr.get_entry(reg)[0] == "alpha"
    assert rr.get_entry(reg, "beta")[1]["dest"] == "beta"


def test_get_entry_refuses_unknown_and_defaultless():
    with pytest.raises(ValueError, match="not registered"):
        rr.get_entry(registry(), "gamma")
    nodefault = registry()
    del nodefault["default_repo"]
    with pytest.raises(ValueError, match="no default_repo"):
        rr.get_entry(nodefault)


def test_deleting_an_entry_leaves_the_rest_working():
    reg = registry()
    del reg["repos"]["alpha"]
    del reg["default_repo"]
    assert rr.get_entry(reg, "beta")[0] == "beta"


def test_variant_dest_and_delta():
    e = entry()
    assert rr.variant_dest(e, None) == "alpha"
    assert rr.variant_dest(e, "next") == "alpha-next"
    assert rr.variant_delta(e, "next") == ["widgetdb==2.0.0"]
    assert rr.variant_delta(e, None) == []
    with pytest.raises(ValueError, match="not defined"):
        rr.variant_delta(e, "nope")


@pytest.mark.parametrize("version,spec,ok", [
    ("1.5.6", ">=1.5.6,<2.1", True),
    ("2.0.0", ">=1.5.6,<2.1", True),
    ("1.5.5", ">=1.5.6,<2.1", False),
    ("2.1.0", ">=1.5.6,<2.1", False),
    ("2.0.0", "==2.0.0", True),
    ("2.0.1", "==2.0.0", False),
    ("0.9", ">0.9", False),
    ("1.0", ">=1.0, !=1.0.1", True),
])
def test_version_satisfies(version, spec, ok):
    assert rr.version_satisfies(version, spec) is ok


def test_check_versions_reports_each_check():
    out = rr.check_versions({"widgetdb": "2.0.0"}, {"widgetdb": ">=1.5.6,<2.1", "gone": ">=1"})
    assert out["widgetdb"] == ("2.0.0", ">=1.5.6,<2.1", True)
    assert out["gone"][2] is False


def _record(dest, repo="alpha", variant=None):
    return {"repo": repo, "variant": variant, "dest": dest,
            "venv_python": f"/content/{dest}/.venv/bin/python",
            "cloned_head": "h", "cloned_tree": "t", "vm_python": "3.13.18",
            "init_route": "init_venv.py", "versions_fingerprint": "f"}


def test_remote_merge_never_clobbers_other_dests():
    reg = rr.merge_remote_record({}, _record("alpha"))
    reg = rr.merge_remote_record(reg, _record("beta", repo="beta"))
    reg = rr.merge_remote_record(reg, _record("alpha-next", variant="next"))
    assert sorted(reg) == ["alpha", "alpha-next", "beta"]
    reg = rr.merge_remote_record(reg, _record("alpha"))
    assert sorted(reg) == ["alpha", "alpha-next", "beta"]


def test_resolve_remote_repo(tmp_path):
    reg = rr.merge_remote_record({}, _record("alpha"))
    reg = rr.merge_remote_record(reg, _record("beta", repo="beta"))
    (tmp_path / "env-registry.json").write_text(json.dumps(reg))
    assert rr.resolve_remote_repo(tmp_path, "beta")["dest"] == "beta"
    with pytest.raises(ValueError, match="expected exactly one"):
        rr.resolve_remote_repo(tmp_path)
    with pytest.raises(ValueError, match="expected exactly one"):
        rr.resolve_remote_repo(tmp_path, "gamma")
    single = tmp_path / "single"
    single.mkdir()
    (single / "env-registry.json").write_text(json.dumps({"alpha": _record("alpha")}))
    assert rr.resolve_remote_repo(single)["dest"] == "alpha"


def test_local_registry_roundtrip(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repos.json").write_text(json.dumps(registry()))
    loaded = rr.load_local_registry(tmp_path)
    assert rr.get_entry(loaded)[0] == "alpha"
    assert rr.load_local_registry(tmp_path / "absent") == {"repos": {}}


SETUP_FILES = [
    "bin/colab-check",
    "snippets/env_build.sh",
    "snippets/bootstrap_remote.py",
    "snippets/shell_job.py",
    "sync/make_snapshot_bundle.sh",
    "sync/base_clone.sh",
    "sync/repo_registry.py",
    "sync/data_manifest.py",
    "config/repos.example.json",
    "jobs/env-build/job.json",
]
FORBIDDEN_LITERALS = ["workload-repo", "workload_sec", "AcmeOwner42", "duckdb", "WORKLOAD_REPO", "workspace/workload"]


@pytest.mark.parametrize("rel", SETUP_FILES)
def test_setup_files_name_no_specific_repo(rel):
    text = (REPO / rel).read_text()
    for literal in FORBIDDEN_LITERALS:
        assert literal not in text, f"{rel} hardcodes {literal!r}"


CONSUMER_FILES = [
    "snippets/facade_battery_job.py",
    "snippets/facade_memory_profiler.py",
    "snippets/fixture_gathering_job.py",
]


@pytest.mark.parametrize("rel", CONSUMER_FILES)
def test_consumer_jobs_hardcode_no_clone_path(rel):
    text = (REPO / rel).read_text()
    assert "/content/workload-repo" not in text
    assert "env-registry.json" in text  # they resolve via the remote registry
