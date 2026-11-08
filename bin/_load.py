#!/usr/bin/env python3
"""Shared module loader for the bin/ entry points.

Every Python entry point in bin/ loads its sync/ modules (and, for
the thin CLIs, bin/colab-run itself) by path, because none of these
files is an installed package. That stanza — resolve the repo root
from this file, build a spec for a path, exec it — used to be copied
into all seven entry points; it lives here once. Entry points put
bin/ on sys.path (it is sys.path[0] when an entry point runs as a
script; the insert covers by-path loads) and `import _load`.

Module names are preserved from the per-file stanzas this replaces:
sync modules load under their bare names ("lease", "transfer", ...),
except where a consumer historically chose a distinct name (the
liveness entry loads liveness_entry as "colab_liveness_entry").
bin/colab-run has no .py suffix, so it loads through an explicit
SourceFileLoader under the name "colab_run".
"""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = Path(__file__).resolve().parent


def _exec(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_package(name: str, pkg_dir: Path):
    """Load a package directory's __init__.py by path and exec it.

    A package load must register the module in sys.modules before
    exec: its leaves import each other relative to the package
    name, and the import system resolves the parent through
    sys.modules.
    """
    spec = importlib.util.spec_from_file_location(
        name, pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sync(name: str, module_name: str | None = None):
    """Load sync/<name>.py by path and exec it; when sync/<name>
    is a package directory instead, load its __init__.py as a
    package."""
    pkg_dir = REPO / "sync" / name
    if (pkg_dir / "__init__.py").is_file():
        return _exec_package(module_name or name, pkg_dir)
    return _exec(module_name or name, REPO / "sync" / f"{name}.py")


def colab_run():
    """Load bin/colab-run (no .py suffix) as the module "colab_run"."""
    spec = importlib.util.spec_from_loader("colab_run", SourceFileLoader("colab_run", str(BIN / "colab-run")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
