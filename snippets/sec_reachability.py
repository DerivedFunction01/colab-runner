#!/usr/bin/env python3
"""One fair-access SEC request from wherever this runs (the Colab VM).

SEC_USER_AGENT must be present in the environment (passed via --env by the
caller); it is never printed and never stored. Prints status + timing only.
"""

import json
import os
import time
import urllib.request

ua = os.environ.get("SEC_USER_AGENT")
if not ua:
    print(json.dumps({"verdict": "SEC_USER_AGENT not set in environment"}))
    raise SystemExit(0)
req = urllib.request.Request("https://www.sec.gov/cgi-bin/browse-workload?action=getcompany", headers={"User-Agent": ua})
t0 = time.perf_counter()
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read(512)
        print(json.dumps({"status": r.status, "seconds": round(time.perf_counter() - t0, 2), "bytes_sampled": len(body), "verdict": "reachable"}))
except Exception as e:  # noqa: BLE001
    print(json.dumps({"verdict": "not-reachable", "error": type(e).__name__, "seconds": round(time.perf_counter() - t0, 2)}))
