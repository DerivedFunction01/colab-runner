import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, records

def append_spend_segment(repo: Path, rec: dict, end_dt: datetime) -> bool:
    """Journal the completed segment of the session `rec` currently
    names: [rec.acquired_at, end_dt] at the record's rate snapshot.
    Called at every seam that ends a session's assignment under a
    record. Best-effort — a failed journal write never breaks the
    lifecycle action it records."""
    try:
        start = foundation.parse_dt(rec["acquired_at"])
    except (KeyError, ValueError):
        return False
    if end_dt <= start:
        return False
    rate = rec.get("rate_cu_per_hour")
    if rate is None and rec.get("shape"):
        rate = catalog.shape_rate(repo, rec["shape"])
    line = {"session": rec.get("session"), "shape": rec.get("shape"), "start": rec["acquired_at"], "end": end_dt.isoformat(), "rate": rate}
    if rec.get("caller"):
        line["caller"] = rec["caller"]
    try:
        p = foundation.spend_journal_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        return True
    except OSError:
        return False


def _journal_segments(repo: Path) -> list[dict]:
    out = []
    try:
        lines = foundation.spend_journal_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            seg = json.loads(line)
            out.append({"session": seg.get("session"), "caller": seg.get("caller"), "shape": seg.get("shape"), "start": foundation.parse_dt(seg["start"]), "end": foundation.parse_dt(seg["end"]), "rate": seg.get("rate")})
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def _overlap_cu(start: datetime, end: datetime, rate, period_start: datetime, period_end: datetime) -> float:
    if rate is None:
        return 0.0
    seconds = (min(end, period_end) - max(start, period_start)).total_seconds()
    return rate * seconds / 3600.0 if seconds > 0 else 0.0


def attributed_spend(repo: Path, now_dt: datetime | None = None) -> dict:
    """This repo's own lease burn for the current UTC day and month:
    {"day_spent", "month_spent", "sessions": {name: {...}},
    "callers": {caller: {...}}}, derived from the segment journal
    plus live records' running segments (see the accounting note
    above). A segment counts toward the session and caller it was
    written with; a caller-less segment counts toward its session
    and the totals only."""
    now = now_dt or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    totals = {"day": 0.0, "month": 0.0}
    per_session: dict[str, dict] = {}
    per_caller: dict[str, dict] = {}

    def add(session, caller, start, end, rate):
        d = _overlap_cu(start, end, rate, day_start, now)
        m = _overlap_cu(start, end, rate, month_start, now)
        if not d and not m:
            return
        totals["day"] += d
        totals["month"] += m
        if session:
            entry = per_session.setdefault(session, {"day_spent": 0.0, "month_spent": 0.0})
            entry["day_spent"] += d
            entry["month_spent"] += m
        if caller:
            entry = per_caller.setdefault(caller, {"day_spent": 0.0, "month_spent": 0.0})
            entry["day_spent"] += d
            entry["month_spent"] += m

    for seg in _journal_segments(repo):
        add(seg["session"], seg["caller"], seg["start"], seg["end"], seg["rate"])
    for rec in records.all_records(repo):
        try:
            start = foundation.parse_dt(rec["acquired_at"])
        except (KeyError, ValueError):
            continue
        rate = rec.get("rate_cu_per_hour")
        if rate is None and rec.get("shape"):
            rate = catalog.shape_rate(repo, rec["shape"])
        add(rec.get("session"), rec.get("caller"), start, now, rate)
    for bucket in (per_session, per_caller):
        for entry in bucket.values():
            entry["day_spent"] = round(entry["day_spent"], 2)
            entry["month_spent"] = round(entry["month_spent"], 2)
    return {"day_spent": round(totals["day"], 2), "month_spent": round(totals["month"], 2), "sessions": per_session, "callers": per_caller}


def budget_check(colab, repo: Path, cfg: dict, shape: dict, hours: float | None = None, caps_override: dict | None = None, now_dt: datetime | None = None, standing_lease: bool = False, exclude_session: str | None = None) -> dict:
    """Project this lease's window cost against the daily/monthly
    budgets. Spend is attributed (see the accounting note above):
    day/month figures are this repo's own lease burn from the
    segment journal plus live records — balance movements the
    repo did not cause (external usage, credit expirations) never
    enter them. The balance itself remains a floor: a window whose
    projected cost exceeds the whole balance refuses
    ("balance-insufficient"), so a real credit loss still cannot be
    overspent. The projected window is `hours` when given (the
    acquire's effective window), else the config max_hours.

    Pricing is per shape: the shape entry's rate_cu_per_hour (the
    catalog's measured rate for the shape). A shape whose rate is
    null has an unconfirmed rate: no projection is fabricated —
    the check returns ok with "rate_unconfirmed": true and a null
    projection.

    `caps_override` (a grant's snapshot caps, served shape) replaces
    the config caps for this check; without it the live config caps
    price the window, exactly as before the envelope.

    `standing_lease` marks the standing duty's lease: it
    additionally answers to standing.daily_cu_cap, checked against
    the standing caller's own day spend (reason "standing-budget");
    no other lease is ever refused by that sub-cap.

    Concurrency: every other live lease's remaining window (rate x
    time to its deadline) is already committed burn, added to this
    window's projection for the floor and the caps and reported as
    "committed_projected" — concurrent windows are priced as the
    sum they commit, so two windows must not each pass a guard
    their sum would fail. The other leases' elapsed burn is already
    inside day/month spent via their live records. For the daily
    check the committed sum is clipped to the current UTC day —
    the same clipping day_spent gets (owner ruling 2026-11-07):
    a multi-day window charges only today's portion against
    today's cap, and each later day re-prices as it arrives. The
    balance floor and the monthly check keep the full windows:
    a window may not promise more than the account holds, and
    the month is the monthly window's own horizon. Returns
    {"ok": bool, ...}."""
    try:
        usage_res = colab("usage", timeout=120)
    except Exception:  # noqa: BLE001
        # The usage read rides the stall-prone egress proxy: a timeout
        # is a refusal (the guard cannot price without a balance),
        # never a crash out of acquire — same rule as the heartbeat
        # and keepalive bounds.
        return {"ok": False, "reason": "usage-unavailable"}
    if "colab auth required" in (usage_res.stderr or ""):
        # The wrapper guard's stop-and-surface signal (2026-11-01):
        # a dead credential is its own refusal reason, never a parse
        # failure — the panel and the reaper both key on it.
        return {"ok": False, "reason": "auth-required"}
    usage = usage_res.stdout
    balance = foundation.parse_balance(usage)
    if balance is None:
        return {"ok": False, "reason": "usage-unparseable", "usage": usage.strip()[-200:]}
    rate = shape.get("rate_cu_per_hour")
    window = float(hours) if hours is not None else float(cfg.get("max_hours", 8))
    projected = rate * window if rate is not None else None
    # Envelope pricing (two-tier envelope plan): a caps override —
    # a grant's snapshot, in the served caps shape — replaces the
    # live config caps for the checks below, key by key; a null in
    # the snapshot is a real no-cap, exactly as in config.
    def cap(key, live):
        if caps_override is not None and key in caps_override:
            return caps_override[key]
        return live
    spend = attributed_spend(repo, now_dt)
    day_spent = spend["day_spent"]
    month_spent = spend["month_spent"]
    if projected is None:
        return {"ok": True, "balance": balance, "projected": None, "rate_unconfirmed": True, "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
    now = now_dt or datetime.now(timezone.utc)
    committed = projected + _committed_projection(repo, now, exclude_session)
    committed_extra = round(committed - projected, 2)
    if committed > balance:
        # The floor: caps throttle the repo's own burn, but no
        # window may promise more than the account actually holds.
        out = {"ok": False, "reason": "balance-insufficient", "projected": round(projected, 2), "balance": balance, "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
        if committed_extra:
            out["committed_projected"] = committed_extra
        return out
    daily_cap = cap("daily", cfg.get("daily_cu_budget"))
    monthly_cap = cap("monthly", cfg.get("monthly_cu_budget"))
    if daily_cap is not None and day_spent + committed > daily_cap:
        # Retry the comparison with other windows clipped to the
        # current UTC day before refusing on the daily cap: the
        # unclipped sum stays in the verdict as committed_full so
        # the overage's composition is never hidden.
        day_end = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        committed_day = projected + _committed_projection(repo, now, exclude_session, clip_end=day_end)
        if day_spent + committed_day > daily_cap:
            out = {"ok": False, "reason": "daily-budget", "day_spent": round(day_spent, 2), "projected": round(projected, 2), "cap": daily_cap, "balance": balance}
            clipped_extra = round(committed_day - projected, 2)
            if clipped_extra:
                out["committed_projected"] = clipped_extra
            if committed_extra != clipped_extra:
                out["committed_full"] = committed_extra
            return out
    if monthly_cap is not None and month_spent + committed > monthly_cap:
        out = {"ok": False, "reason": "monthly-budget", "month_spent": round(month_spent, 2), "projected": round(projected, 2), "cap": monthly_cap, "balance": balance}
        if committed_extra:
            out["committed_projected"] = committed_extra
        return out
    if standing_lease:
        standing_cfg = cfg.get("standing", {})
        standing_cap = cap("standing_daily", standing_cfg.get("daily_cu_cap"))
        standing_spent = spend["callers"].get(catalog.standing_caller(repo), {}).get("day_spent", 0.0)
        if standing_cap is not None and standing_spent + projected > float(standing_cap):
            return {"ok": False, "reason": "standing-budget", "standing_day_spent": round(standing_spent, 2), "projected": round(projected, 2), "cap": standing_cap, "balance": balance}
    out = {"ok": True, "balance": balance, "projected": round(projected, 2), "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
    if committed_extra:
        out["committed_projected"] = committed_extra
    return out


def _committed_projection(repo: Path, now_dt: datetime, exclude_session: str | None = None, clip_end: datetime | None = None) -> float:
    """Every other live lease's remaining committed window cost:
    rate x the time left to its record's deadline, summed. Zero for
    an expired window or an unconfirmed rate (nothing fabricated).
    The lease being priced excludes itself (exclude_session) — its
    own window is the projection it rides beside. `clip_end` caps
    each window at that instant (the daily check passes the end
    of the current UTC day, so a multi-day window charges only
    the portion that falls inside today)."""
    total = 0.0
    for rec in records.all_records(repo):
        if exclude_session is not None and rec.get("session") == exclude_session:
            continue
        try:
            end = foundation.parse_dt(rec["deadline"])
        except (KeyError, ValueError):
            continue
        if clip_end is not None and end > clip_end:
            end = clip_end
        remaining_h = (end - now_dt).total_seconds() / 3600.0
        if remaining_h <= 0:
            continue
        rate = rec.get("rate_cu_per_hour")
        if rate is None and rec.get("shape"):
            rate = catalog.shape_rate(repo, rec["shape"])
        if rate is not None:
            total += rate * remaining_h
    return total
