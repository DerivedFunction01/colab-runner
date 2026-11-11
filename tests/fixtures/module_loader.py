"""The suite's one module loader.

Every test module that needs a repo module by path loads it
through here instead of repeating the SourceFileLoader /
spec_from_file_location stanza: .py paths load by file location
(the loader is inferred from the suffix), anything else rides an
explicit SourceFileLoader, and a package load gets its submodule
search locations from the caller. Each call executes a FRESH
module — callers keep the per-file isolation the repeated
stanzas gave them (one file's monkeypatched copy is never
another file's); register=True additionally mirrors the module
into sys.modules under its load name before execution, as the
stanzas that registered theirs did.
"""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent.parent


def load_module(name, path, *, register=False, search_locations=None):
    path = Path(path)
    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location(
            name, path, submodule_search_locations=search_locations)
    else:
        spec = importlib.util.spec_from_loader(
            name, SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_lease(name="lease", *, register=True):
    """The lease package (sync/lease/__init__.py) under `name`."""
    package = CHECKOUT / "sync" / "lease"
    return load_module(name, package / "__init__.py", register=register,
                       search_locations=[str(package)])
