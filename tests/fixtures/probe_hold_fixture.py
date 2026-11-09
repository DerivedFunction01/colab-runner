"""Fictional probe-hold payload: the pure reporting helpers of a
held-session probe, as a reference implementation of the shapes
the runner's venue jobs print — a per-touch JSON line, one
prefixed outcome line carrying the report verbatim, a window-end
parser, a verdict mapping with an error default, and a
value-free destination classifier (a code page's stated
destination is reported as a channel and a domain, never the
address itself).

Shape-only by design: the production probe's marker grammar is
the contract of record in its owner's backend, and its session
machinery (logins, holds, touches against a live portal) is
workload code tracked there. This fixture deliberately carries
a fictional outcome prefix so it cannot be mistaken for that
contract; what it pins is the shape — prefix + one JSON object,
keys as named, values value-free where they must be.
"""

import json
import re
from datetime import datetime, timezone

OUTCOME_PREFIX = "PROBE_OUTCOME "


def destination_class(text: str) -> dict:
    """Classify a code page's stated destination, value-free."""
    for line in (text or "").splitlines():
        low = line.lower()
        if "sent" not in low:
            continue
        if "code" not in low and "verification" not in low and "@" not in line:
            continue
        match = re.search(r"@([A-Za-z0-9.\-]+)", line)
        if match:
            return {
                "channel": "email",
                "domain": match.group(1).lower(),
                "line_found": True,
            }
        if "phone" in low or "text" in low or "sms" in low:
            return {"channel": "phone", "line_found": True}
        return {"channel": "other", "line_found": True}
    return {"channel": "unknown", "line_found": False}


def parse_window_end(text: str) -> datetime:
    """Parse the window-end argument: an absolute UTC ISO time.
    A trailing Z and a naive value both read as UTC; an explicit
    offset is honoured and normalised to UTC."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("window end argument is empty")
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def map_verify_verdict(raw: str) -> str:
    """Map a session verify verdict onto the probe's vocabulary;
    anything unrecognised is an error, never a pass."""
    if raw == "live":
        return "signed_in"
    if raw == "dead":
        return "dropped"
    return "error"


def touch_line(ts: str, verdict: str, landing: str | None) -> str:
    """One hold-loop touch as a value-free JSON line."""
    return json.dumps({"ts": ts, "verdict": verdict, "landing": landing})


def outcome_line(report: dict) -> str:
    return OUTCOME_PREFIX + json.dumps(report)
