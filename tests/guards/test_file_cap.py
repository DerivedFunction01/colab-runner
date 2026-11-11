"""File-size cap guard (the standing rule, AGENTS.md): no covered
source file exceeds 800 lines, and approaching the cap is itself
the defect — a file that grows large by absorbing different
behaviours becomes a serialization point, so bulk is decomposed
along the file's real seams rather than routed around. This test
is the enforcement: it walks the tree as it stands (no file list
to rot, no new file missed) and names every offender with its
line count.

Coverage: every *.py and *.js in the repo, plus the extensionless
Python entry points under bin/ (no suffix, a python shebang —
the shape a suffix glob alone would miss). Directories that are
not tracked source are pruned, all gitignored: VCS internals and
caches, runtime state (var/, results/), and harness working state
(.plans/, workbench/) — counting them would measure the machine,
not the repo. Line counts are wc -l counts (newline characters).

The self-check tests are part of the guard: a walker that found
nothing would pass the cap test vacuously, so the suite pins that
the covered set is large, contains named sentinels, and classifies
a miniature tree correctly.
"""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CAP_LINES = 800

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".venv",
    "var",
    "results",
    ".plans",
    "workbench",
}

SOURCE_SUFFIXES = {".py", ".js"}

# Files the covered set must contain for the walk to be believed:
# one from each coverage class (extensionless bin entry, package
# module, static JS, and this test itself).
SENTINELS = {
    "bin/colab-sh",
    "sync/cli.py",
    "ui/static/app.js",
    "tests/guards/test_file_cap.py",
}

# The repo carries well over a hundred covered files; a walk that
# returns a handful has silently missed the tree.
MIN_COVERED = 100


def _line_count(path):
    """wc -l semantics: newline characters in the file."""
    return path.read_bytes().count(b"\n")


def _is_python_entry(path):
    """An extensionless file whose first line is a python shebang."""
    try:
        with path.open("rb") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def covered_files(root=REPO):
    """Every covered source file under root, sorted, repo-relative
    order. The walk prunes EXCLUDED_DIRS in place."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix in SOURCE_SUFFIXES:
                found.append(path)
            elif (
                not path.suffix
                and path.parent == root / "bin"
                and _is_python_entry(path)
            ):
                found.append(path)
    return found


def test_walker_finds_the_repo():
    covered = covered_files()
    rel = {p.relative_to(REPO).as_posix() for p in covered}
    assert len(covered) >= MIN_COVERED, (
        f"walker found only {len(covered)} covered files under {REPO} — "
        "the walk is broken, not the tree small"
    )
    missing = SENTINELS - rel
    assert not missing, f"covered set is missing sentinels: {sorted(missing)}"


def test_walker_classifies_a_miniature_tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.js").write_text("let x = 1;\n")
    (tmp_path / "c.txt").write_text("not source\n")
    (tmp_path / "bin" / "tool").write_text("#!/usr/bin/env python3\n")
    (tmp_path / "bin" / "shelly").write_text("#!/usr/bin/env bash\n")
    (tmp_path / "pkg" / "__pycache__" / "cached.py").write_text("x = 2\n")
    rel = {p.relative_to(tmp_path).as_posix() for p in covered_files(tmp_path)}
    assert rel == {"a.py", "b.js", "bin/tool"}


def test_no_covered_file_exceeds_the_cap():
    over = []
    for path in covered_files():
        lines = _line_count(path)
        if lines > CAP_LINES:
            rel = path.relative_to(REPO).as_posix()
            over.append(f"{rel}: {lines} lines (cap {CAP_LINES})")
    assert not over, "files over the file-size cap:\n" + "\n".join(over)
