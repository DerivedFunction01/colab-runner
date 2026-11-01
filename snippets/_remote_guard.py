"""Remote-only guard for limit/OOM snippets (operator ruling, 2026-10-29).

The OOM loop is for Colab, not this machine: these snippets allocate until
the ceiling and would OOM the local 2-vCPU VM if run here. They refuse to
run unless a Colab runtime marker is present. A deliberate local dry run
needs the explicit --force-local flag, which also caps the workload.
"""

import os
import sys
from pathlib import Path


def on_colab() -> bool:
    return any(k.startswith("COLAB_") for k in os.environ) or Path("/content").exists()


def require_colab(name: str) -> bool:
    """Return True if forced-local (caller must cap the workload)."""
    if "--force-local" in sys.argv:
        print(f"{name}: --force-local given; running capped on this machine", file=sys.stderr)
        return True
    if not on_colab():
        print(
            f"{name}: REFUSED — this is a Colab-only limit probe and no Colab runtime "
            "marker was found (no COLAB_* env, no /content). Run it via the probe "
            "driver (probes/p3_oom_lowtier.py) on a Colab session, never locally.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return False
