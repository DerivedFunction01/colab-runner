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


def unassign_with_body(client, endpoint: str) -> None:
    """The client's unassign flow with a body on the POST. The stock
    call sends the POST bodiless and the tunnel endpoint answers
    411 Length Required (diagnosed 2026-11-04), so the client's own
    two steps — GET the unassign URL for an XSRF token, then POST it
    with the token header — are replayed here with an explicit
    empty-JSON body attached (data=b"{}"), the form that succeeds."""
    from urllib.parse import urljoin

    from pydantic import BaseModel

    from colab_cli.client import COLAB_XSRF_TOKEN_HEADER, TUN_ENDPOINT, GetUnassignRequest

    url = urljoin(client.colab_domain, f"{TUN_ENDPOINT}/unassign/{endpoint}")
    resp = client._issue_request(url, schema=GetUnassignRequest)
    headers = {COLAB_XSRF_TOKEN_HEADER["key"]: resp.token}
    client._issue_request(url, method="POST", headers=headers, schema=BaseModel, data=b"{}")


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
            unassign_with_body(state.client, a.endpoint)
            print(f"unassigned {a.endpoint}")
        print(json.dumps({"remaining": [a.endpoint for a in state.client.list_assignments()]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
