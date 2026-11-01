#!/usr/bin/env python3
"""Release server-side Colab assignments that have no local session binding.

Why this exists (observed 2026-10-29): `colab run -s <name>` creates its own
VM, releases it, and removes the local binding for <name> — orphaning any
still-live assignment that `colab new -s <name>` created. `colab stop` then
reports the session "not found" while the assignment keeps billing. The
server list (shown by `colab sessions` with a [?] marker) is the truth.

Run with the CLI's own interpreter so colab_cli is importable:
  ~/.local/share/uv/tools/google-colab-cli/bin/python snippets/unassign_orphans.py [--apply]
Without --apply it only lists. Contents of credentials are never printed.
"""

import json
import sys
from pathlib import Path


def main() -> int:
    apply = "--apply" in sys.argv
    from colab_cli.common import state

    local = state.store.load() if hasattr(state.store, "load") else {}
    bound_endpoints = set()
    try:
        sessions_file = Path.home() / ".config" / "colab-cli" / "sessions.json"
        bound_endpoints = {v.get("endpoint") for v in json.loads(sessions_file.read_text()).values() if isinstance(v, dict)}
    except Exception:
        pass
    assignments = state.client.list_assignments()
    orphans = [a for a in assignments if a.endpoint not in bound_endpoints]
    print(json.dumps({"assignments": [a.endpoint for a in assignments], "orphans": [a.endpoint for a in orphans], "applied": False}, indent=2))
    if apply:
        for a in orphans:
            state.client.unassign(a.endpoint)
            print(f"unassigned {a.endpoint}")
        print(json.dumps({"remaining": [a.endpoint for a in state.client.list_assignments()]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
