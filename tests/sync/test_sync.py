"""Sync-layer units: data-manifest pricing choices and pull verification."""

import hashlib
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_data_manifest_route_choices(tmp_path):
    art = tmp_path / ".artifacts"
    (art / "metadata" / "snap1").mkdir(parents=True)
    (art / "metadata" / "snap1" / "a.bin").write_bytes(b"x" * 1000)
    (art / "filing_catalog").mkdir()
    (art / "filing_catalog" / "c.bin").write_bytes(b"y" * 5000)
    (art / "run-logs").mkdir()
    (art / "run-logs" / "l.txt").write_bytes(b"z" * 10)
    dm = load("data_manifest", "sync/data_manifest.py")
    import sys

    argv = sys.argv
    sys.argv = ["data_manifest", "--artifacts", str(art)]
    try:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            dm.main()
        manifest = json.loads(buf.getvalue())
    finally:
        sys.argv = argv
    by_class = {c["class"]: c for c in manifest["classes"]}
    assert by_class["metadata"]["chosen_route"] == "transfer_self"
    assert by_class["filing_catalog"]["chosen_route"] == "transfer_driver_regen"
    assert by_class["filing_catalog"]["driver"]["bytes"] == 1000
    assert by_class["run-logs"]["chosen_route"] == "excluded"
    assert by_class["metadata"]["bytes"] == 1000


def test_pull_results_rejects_corruption(tmp_path):
    pr = load("pull_results", "sync/pull_results.py")
    f = tmp_path / "files" / "out.bin"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"payload")
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"files": [{"path": "out.bin", "bytes": 7, "sha256": hashlib.sha256(b"payload").hexdigest()}]}))
    import sys

    argv = sys.argv
    sys.argv = ["pull_results", str(man), str(tmp_path / "files")]
    try:
        assert pr.main() == 0
        f.write_bytes(b"payload!")
        assert pr.main() == 1
    finally:
        sys.argv = argv
