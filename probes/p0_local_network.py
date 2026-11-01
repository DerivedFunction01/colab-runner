#!/usr/bin/env python3
"""P0: local network baseline (stdlib only).

Measures TTFB to GitHub / PyPI / Colab / SEC, and the real cost of fetching
the workload-repo code payload the way a Colab bootstrap would (codeload tarball
of origin main). Writes a JSON result under probes/results/.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "colab-runner-probe/1.0"}


def ttfb(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA, method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            first = r.read(1)
            return {"url": url, "status": r.status, "ttfb_s": round(time.perf_counter() - t0, 3), "got_bytes": bool(first)}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "ttfb_s": round(time.perf_counter() - t0, 3)}
    except Exception as e:  # noqa: BLE001 - probe records, never crashes
        return {"url": url, "error": type(e).__name__, "ttfb_s": round(time.perf_counter() - t0, 3)}


def download(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    t0 = time.perf_counter()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            while chunk := r.read(1024 * 1024):
                total += len(chunk)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "error": type(e).__name__, "bytes": total, "seconds": round(time.perf_counter() - t0, 2)}
    dt = time.perf_counter() - t0
    return {"url": url, "bytes": total, "seconds": round(dt, 2), "bytes_per_s": round(total / dt)}


def reference_payload() -> dict:
    """A public file in the same size class as the workload-repo code payload.

    The workload-repo GitHub repo is not publicly readable (codeload returns
    404 unauthenticated), so throughput is calibrated on the largest pyarrow
    wheel from PyPI instead; Step 7 prices the real bundle from disk.
    """
    req = urllib.request.Request("https://pypi.org/pypi/pyarrow/json", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        meta = json.load(r)
    files = [u for u in meta["urls"] if "manylinux" in u["filename"] and "x86_64" in u["filename"]]
    biggest = max(files, key=lambda u: u["size"])
    out = download(biggest["url"])
    out["reference_for"] = f"pyarrow wheel, declared {biggest['size']} bytes"
    return out


def main() -> int:
    result = {
        "probe": "p0_local_network",
        "at": datetime.now(timezone.utc).isoformat(),
        "ttfb": [
            ttfb("https://github.com"),
            ttfb("https://pypi.org/simple/google-colab-cli/"),
            ttfb("https://colab.research.google.com"),
            ttfb("https://www.sec.gov/cgi-bin/browse-workload?action=getcompany"),
        ],
        "payload": reference_payload(),
    }
    out = REPO / "probes" / "results" / f"p0-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
