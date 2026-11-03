#!/usr/bin/env python3
"""The Colab lease: one shared session per profile, treated as a VM.

All session-lifecycle logic for the lease model lives here (plan:
.plans/2026-10-29-colab-lease-model.md). The module is written against an
injected `colab` callable with bin/colab-run's signature —
`colab(*args, timeout=...) -> CompletedProcess` — so the fake CLI tests it
with no network. bin/colab-lease is the thin CLI; bin/colab-run and
bin/colab-check call acquire/finish_job; the reaper hook calls reap and,
when enabled in config, auto_acquire.

State: one record per profile, var/lease-<profile>.json (override the
var dir with $COLAB_RUNNER_VAR, used by tests), with a read fallback to
the legacy var/lease.json for a profile whose record still lives there
— a save migrates that profile's record to its per-profile path. A
two-tier grant lives in var/grant.json. The budget guard's spend is
attributed: completed session segments are journaled to
var/lease-spend-journal.jsonl and live records contribute their
running segment, so spend counts only this repo's own leases.
Config:
config/lease.json + profiles from config/profiles.json. Nothing here
prints file contents from the transient registry; the sessions binding
is read only for its endpoint on the release fallback path.
"""

import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


def var_dir(repo: Path) -> Path:
    return Path(os.environ.get("COLAB_RUNNER_VAR", repo / "var"))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def load_config(repo: Path) -> dict:
    return load_json(repo / "config" / "lease.json", {"enabled": False})


def load_profiles(repo: Path) -> dict:
    return load_json(repo / "config" / "profiles.json", {"profiles": {}}).get("profiles", {})


def catalog_profiles(repo: Path, colab=None) -> dict:
    """The full spec catalog: the configured profiles merged with the
    dynamic CLI shapes they do not cover (see _cli_shapes), plus the
    derived parallel-instance profiles whose toggles are on (see the
    parallel-instances section below). Callers without a colab
    callable get the configured profiles plus whatever the cache
    holds."""
    configured = load_profiles(repo)
    shapes, _err = _cli_shapes(repo, colab)
    merged = {**configured, **_dynamic_profiles(shapes, configured)} if shapes else dict(configured)
    return _with_parallel_instances(repo, merged)


# --- The dynamic spec catalog -------------------------------------------------
# The picker's option set is NOT hand-listed: the shapes Colab offers
# are parsed from `colab new --help` (the supported GPU/TPU variants
# and the high-RAM ignore list), cached in var/cli-catalog.json for a
# day. config/profiles.json holds only the named workload profiles
# (roles, sessions, measured rates); any CLI shape they do not cover
# is served as a dynamic profile with an unconfirmed (null) rate.
# A parse failure never breaks the lease: the configured profiles
# still serve, and state() surfaces the failure as catalog_error.

CLI_CATALOG_TTL_S = 24 * 3600
DYNAMIC_PROFILE_TIMEOUT_S = 18000  # the configured accelerator profiles' timeout


def parse_cli_shapes(help_text: str) -> dict | None:
    """The accelerator space from `colab new --help`: {"gpu": [...],
    "tpu": [...], "no_high_mem": [...]}. The help is a wrapped,
    box-drawn table, so the text is flattened before matching. None
    when either variant list is absent — a format change must shrink
    the catalog loudly (catalog_error), never silently invent shapes."""
    flat = re.sub(r"\s+", " ", help_text.replace("│", " "))

    def variants(kind: str) -> list[str] | None:
        m = re.search(kind + r" accelerator variant\. Supported: ([^.]+)\.", flat)
        if not m:
            return None
        out = [v for v in re.split(r"[,\s]+", m.group(1)) if v]
        return out or None

    gpu, tpu = variants("GPU"), variants("TPU")
    if gpu is None or tpu is None:
        return None
    m = re.search(r"single shape \(([^)]+)\)", flat)
    no_high_mem = [v for v in re.split(r"[,\s]+", m.group(1)) if v] if m else []
    return {"gpu": gpu, "tpu": tpu, "no_high_mem": no_high_mem}


def _catalog_cache_path(repo: Path) -> Path:
    return var_dir(repo) / "cli-catalog.json"


def _cli_shapes(repo: Path, colab=None):
    """(shapes, error): the parsed CLI space, from the day-cache when
    fresh, else by running `colab new --help` through the injected
    callable. Without a callable only the cache can serve. A failed
    refresh falls back to any cache and reports the failure; with no
    cache at all the dynamic catalog is empty and the error stands."""
    cache = load_json(_catalog_cache_path(repo), None)
    cached = None
    if isinstance(cache, dict) and cache.get("gpu") and cache.get("tpu"):
        cached = {"gpu": cache["gpu"], "tpu": cache["tpu"], "no_high_mem": cache.get("no_high_mem", [])}
        try:
            age = (datetime.now(timezone.utc) - parse_dt(cache["fetched_at"])).total_seconds()
        except Exception:  # noqa: BLE001
            age = CLI_CATALOG_TTL_S + 1
        if age <= CLI_CATALOG_TTL_S:
            return cached, None
    if colab is None:
        return cached, None
    try:
        r = colab("new", "--help", timeout=60)
        shapes = parse_cli_shapes(r.stdout or "") if r.returncode == 0 else None
        if shapes is None:
            raise ValueError("unparseable `colab new --help` output")
    except Exception as exc:  # noqa: BLE001
        if cached is not None:
            return cached, f"cli catalog refresh failed ({exc}); serving the cached shape list"
        return None, f"cli catalog unavailable ({exc}); serving configured profiles only"
    try:
        p = _catalog_cache_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), **shapes}, indent=2))
    except OSError:
        pass
    return shapes, None


def _dynamic_profiles(shapes: dict, configured: dict) -> dict:
    """Profiles for every CLI shape the configured catalog does not
    already cover (same accelerator + high_mem = covered). Ids and
    labels are mechanical; the rate is null (unconfirmed) until an
    on-account lease measures one — never fabricated."""
    covered = {(p.get("gpu"), p.get("tpu"), bool(p.get("high_mem"))) for p in configured.values()}
    out: dict = {}

    def add(pid, label, gpu, tpu, high_mem):
        if pid in configured or pid in out or (gpu, tpu, high_mem) in covered:
            return
        out[pid] = {"label": label, "gpu": gpu, "tpu": tpu, "high_mem": high_mem,
                    "rate_cu_per_hour": None, "session": pid, "timeout": DYNAMIC_PROFILE_TIMEOUT_S}

    for gpu in shapes["gpu"]:
        for hm in (False, True):
            if hm and gpu in shapes["no_high_mem"]:
                continue
            add(gpu.lower() + ("-highram" if hm else ""), gpu + (" High-RAM" if hm else ""), gpu, None, hm)
    for tpu in shapes["tpu"]:
        for hm in (False, True):
            if hm and tpu in shapes["no_high_mem"]:
                continue
            add("tpu-" + tpu.lower() + ("-highram" if hm else ""), "TPU " + tpu + (" High-RAM" if hm else ""), None, tpu, hm)
    for hm in (False, True):
        add("cpu" + ("-highram" if hm else ""), "CPU" + (" High-RAM" if hm else ""), None, None, hm)
    return out


def default_profile(repo: Path) -> str:
    """The default profile id, resolved only from
    config/profiles.json's `default_profile` — executable code
    carries no workload-name fallback. A config without the key is
    a configuration error and fails loudly here rather than
    silently naming a workload."""
    name = load_json(repo / "config" / "profiles.json", {}).get("default_profile")
    if not name:
        raise ValueError("config/profiles.json has no default_profile")
    return name


# --- Parallel instances ---------------------------------------------------------
# A low-tier CPU profile may run ONE extra session next to its primary
# (plan: .plans/2026-11-03-parallel-cpu-sessions.md). The instance is a
# derived profile — base id + "2", base session + "2", same shape and
# measured rate, its own per-profile record — so every profile-keyed
# path (acquire, reap, keepalive, spend, state, panel start/stop)
# covers it with no parallel code path. Eligibility is a backend
# determination, served ready by specs(): CPU shapes only. GPU and
# TPU shapes are excluded — per-account accelerator concurrency is
# unverified and accelerator rates are unmeasured. The toggle is per
# base profile in config/lease.json ("parallel": {profile: true}),
# default off; toggling on registers the instance in the catalog and
# starts nothing, and toggling off refuses while the instance lease
# exists (the toggle never stops a session).


def parallel_eligible(profile: dict) -> bool:
    """CPU shapes only: no GPU, no TPU (see the section note above)."""
    return not profile.get("gpu") and not profile.get("tpu")


def instance_profile_id(base_profile: str) -> str:
    return base_profile + "2"


def parallel_toggles(repo: Path) -> dict:
    raw = load_config(repo).get("parallel")
    return dict(raw) if isinstance(raw, dict) else {}


def parallel_enabled(repo: Path, profile_name: str) -> bool:
    return bool(parallel_toggles(repo).get(profile_name))


def _with_parallel_instances(repo: Path, profiles: dict) -> dict:
    """Add the derived instance profile for every toggled-on eligible
    base in `profiles`. An instance never derives further (one extra
    per profile), and a pre-existing profile under the instance id
    wins — derivation never shadows a configured profile."""
    toggles = parallel_toggles(repo)
    if not toggles:
        return profiles
    out = dict(profiles)
    for base_id, profile in profiles.items():
        if profile.get("parallel_of") or not toggles.get(base_id) or not parallel_eligible(profile):
            continue
        inst_id = instance_profile_id(base_id)
        if inst_id in out:
            continue
        inst = dict(profile)
        inst["session"] = (profile.get("session") or base_id) + "2"
        inst["label"] = f"{profile.get('label') or base_id} (parallel)"
        inst["parallel_of"] = base_id
        out[inst_id] = inst
    return out


def parallel_sibling(repo: Path, profile_name: str) -> str | None:
    """The other half of a parallel pair: for an instance profile its
    base, for a base profile its registered instance, else None."""
    catalog = catalog_profiles(repo)
    profile = catalog.get(profile_name)
    if profile is None:
        return None
    if profile.get("parallel_of"):
        return profile["parallel_of"]
    inst_id = instance_profile_id(profile_name)
    return inst_id if inst_id in catalog else None


def set_parallel(repo: Path, profile_name: str, enabled: bool) -> dict:
    """Set one base profile's parallel toggle in config/lease.json —
    the panel's per-profile switch (the budget caps' sibling seam:
    validated here in the core, written atomically as a tmp sibling +
    rename in the file's compact sorted style, every other key
    preserved). Enabling registers the derived instance profile in
    the served catalog; it starts nothing.

    Refusals: parallel-invalid (config unreadable, or the parallel
    section is not an object), parallel-unknown-profile (not in the
    catalog), parallel-ineligible (an accelerator shape, or the
    profile is itself an instance), parallel-instance-live
    (disabling while the instance lease exists)."""
    path = repo / "config" / "lease.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"action": "parallel-invalid", "error": "config/lease.json is unreadable"}
    if not isinstance(cfg, dict):
        return {"action": "parallel-invalid", "error": "config/lease.json is not a JSON object"}
    raw = cfg.get("parallel")
    if raw is not None and not isinstance(raw, dict):
        return {"action": "parallel-invalid", "error": "config parallel section is not an object"}
    profile = catalog_profiles(repo).get(profile_name)
    if profile is None:
        return {"action": "parallel-unknown-profile", "profile": profile_name}
    inst_id = instance_profile_id(profile_name)
    if profile.get("parallel_of") or not parallel_eligible(profile):
        return {"action": "parallel-ineligible", "profile": profile_name}
    if not enabled and load_record(repo, inst_id) is not None:
        return {"action": "parallel-instance-live", "profile": profile_name, "instance": inst_id}
    previous = bool((raw or {}).get(profile_name))
    toggles = dict(raw or {})
    if enabled:
        toggles[profile_name] = True
    else:
        toggles.pop(profile_name, None)
    cfg["parallel"] = toggles
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, path)
    return {"action": "parallel-updated", "profile": profile_name, "enabled": bool(enabled), "instance": inst_id, "previous": previous}


def record_path(repo: Path) -> Path:
    """The legacy single-lease record path. Current records live per
    profile (profile_record_path); this file is only the migration
    fallback."""
    return var_dir(repo) / "lease.json"


def profile_record_path(repo: Path, profile: str) -> Path:
    return var_dir(repo) / f"lease-{profile}.json"


def _valid_record(data) -> dict | None:
    return data if isinstance(data, dict) and data.get("session") else None


def _load_profile_record(repo: Path, profile: str) -> dict | None:
    """One profile's record: its per-profile file first, then the legacy
    var/lease.json when that file holds this profile's record (the
    pre-two-tier layout — lease #8 was live on it at migration time, and
    the first post-landing reap must still see it)."""
    rec = _valid_record(load_json(profile_record_path(repo, profile), None))
    if rec is not None and rec.get("profile") == profile:
        return rec
    legacy = _valid_record(load_json(record_path(repo), None))
    if legacy is not None and legacy.get("profile") == profile:
        return legacy
    return None


def all_records(repo: Path) -> list[dict]:
    """Every lease record on disk; a per-profile file wins over the
    legacy file for the same profile. Order: config profile order,
    then any remaining profiles by name."""
    by_profile: dict[str, dict] = {}
    legacy = _valid_record(load_json(record_path(repo), None))
    if legacy is not None and legacy.get("profile"):
        by_profile[legacy["profile"]] = legacy
    var = var_dir(repo)
    if var.exists():
        for path in sorted(var.glob("lease-*.json")):
            if path.name in ("lease-spend.json", "lease-request.json"):
                continue
            rec = _valid_record(load_json(path, None))
            if rec is not None and rec.get("profile"):
                by_profile[rec["profile"]] = rec
    order = list(load_profiles(repo).keys())
    return [by_profile[p] for p in order if p in by_profile] + [by_profile[p] for p in sorted(by_profile) if p not in order]


def load_record(repo: Path, profile: str | None = None) -> dict | None:
    """With a profile: that profile's record (legacy fallback included).
    Without: the historical single-record reading kept for existing
    callers — the default profile's record when one exists, else the
    sole record when exactly one lease is on disk, else None."""
    if profile is not None:
        return _load_profile_record(repo, profile)
    recs = all_records(repo)
    if not recs:
        return None
    default = default_profile(repo)
    for rec in recs:
        if rec.get("profile") == default:
            return rec
    return recs[0] if len(recs) == 1 else None


def save_record(repo: Path, rec: dict | None, profile: str | None = None) -> None:
    """Write (or, for None, delete) one profile's record at its
    per-profile path. A save migrates: when the legacy file holds this
    profile's record it is removed, so the fallback can never resurrect
    a stale copy; a delete clears both locations for the same reason."""
    profile = profile or (rec.get("profile") if rec else None) or default_profile(repo)
    p = profile_record_path(repo, profile)
    legacy = record_path(repo)
    legacy_rec = _valid_record(load_json(legacy, None))
    legacy_holds = legacy_rec is not None and legacy_rec.get("profile") == profile
    if rec is None:
        p.unlink(missing_ok=True)
        if legacy_holds:
            legacy.unlink(missing_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, indent=2))
        if legacy_holds:
            legacy.unlink(missing_ok=True)


def parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def parse_balance(usage_text: str) -> float | None:
    m = re.search(r"balance:?\s*([0-9]+(?:\.[0-9]+)?)", usage_text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def session_state(colab, session: str) -> str:
    """Tri-state: "alive" (listed), "absent" (list obtained, name not in
    it), "unknown" (the CLI call failed or returned nothing). Unknown must
    never be treated as dead: on 2026-10-29 the lease record vanished
    while its session lived, and a hiccup-reads-as-death path is the
    standing suspect class. Callers reconcile only on "absent"."""
    try:
        out = colab("sessions", timeout=120).stdout
    except Exception:  # noqa: BLE001
        return "unknown"
    if not out or not out.strip():
        return "unknown"
    if "Enter the authorization code" in out:
        # The CLI's interactive login prompt is not a session list: on
        # 2026-11-01 it read as "absent" here and a live lease's record
        # was deleted under it. The bin/colab-run guard now stops the
        # prompt from ever reaching callers; this is the same rule at
        # the seam, for any path that bypasses the wrapper.
        return "unknown"
    return "alive" if session in out else "absent"


def session_alive(colab, session: str) -> bool:
    return session_state(colab, session) == "alive"


# --- Attributed spend accounting ----------------------------------------------
# Spend is attributed to this repo's own leases, never inferred from
# the account balance. The old accounting (removed 2026-11-01) read
# day/month spend as balance-baseline deltas, so every balance
# movement booked as repo spend — external Colab usage, and credit
# expirations like the ~66 CU step that day, which produced a
# day_spent of 71.17 against a 15 CU cap and refused a 0.08 CU lobby
# start with 699 CU in the bank. The CLI's usage output carries no
# per-source breakdown, so attribution is built from what the repo
# itself knows: a session burns its profile's rate for exactly the
# time it is assigned, and every assignment is a lease record.
#
# Completed assignments are journaled as segments
# (var/lease-spend-journal.jsonl: profile, session, start, end,
# rate) at the seams that end them — release, renewal swap,
# confirmed-dead clear. Live records contribute their running
# segment at read time. Day/month figures split segments at the
# period boundary. A segment journaled at a confirmed-dead clear
# ends at the reap that confirmed it, so a death detected late
# overcounts by at most the detection lag (conservative, bounded).
# Segments completed before this accounting landed were never
# journaled and are not reconstructed: counters are exact from the
# first full UTC day after landing. An assignment with no record
# (an orphan) burns uncounted — the orphan sweep is its cure, as
# before. A profile with an unconfirmed (null) rate contributes
# nothing: no number is fabricated for it.


def spend_journal_path(repo: Path) -> Path:
    return var_dir(repo) / "lease-spend-journal.jsonl"


def _profile_rate(repo: Path, profile_name: str | None):
    """A profile's catalog rate (may be None = unconfirmed)."""
    if profile_name is None:
        return None
    profile = catalog_profiles(repo).get(profile_name)
    return profile.get("rate_cu_per_hour") if isinstance(profile, dict) else None


def append_spend_segment(repo: Path, rec: dict, end_dt: datetime) -> bool:
    """Journal the completed segment of the session `rec` currently
    names: [rec.acquired_at, end_dt] at the profile's rate. Called
    at every seam that ends a session's assignment under a record.
    Best-effort — a failed journal write never breaks the lifecycle
    action it records."""
    try:
        start = parse_dt(rec["acquired_at"])
    except (KeyError, ValueError):
        return False
    if end_dt <= start:
        return False
    line = {"profile": rec.get("profile"), "session": rec.get("session"), "start": rec["acquired_at"], "end": end_dt.isoformat(), "rate": _profile_rate(repo, rec.get("profile"))}
    try:
        p = spend_journal_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        return True
    except OSError:
        return False


def _journal_segments(repo: Path) -> list[dict]:
    out = []
    try:
        lines = spend_journal_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            seg = json.loads(line)
            out.append({"profile": seg.get("profile"), "start": parse_dt(seg["start"]), "end": parse_dt(seg["end"]), "rate": seg.get("rate")})
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
    {"day_spent", "month_spent", "profiles": {name: {"day_spent",
    "month_spent"}}}, derived from the segment journal plus live
    records' running segments (see the accounting note above)."""
    now = now_dt or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    totals = {"day": 0.0, "month": 0.0}
    per: dict[str, dict] = {}

    def add(profile, start, end, rate):
        d = _overlap_cu(start, end, rate, day_start, now)
        m = _overlap_cu(start, end, rate, month_start, now)
        if not d and not m:
            return
        totals["day"] += d
        totals["month"] += m
        entry = per.setdefault(profile, {"day_spent": 0.0, "month_spent": 0.0})
        entry["day_spent"] += d
        entry["month_spent"] += m

    for seg in _journal_segments(repo):
        add(seg["profile"], seg["start"], seg["end"], seg["rate"])
    for rec in all_records(repo):
        try:
            start = parse_dt(rec["acquired_at"])
        except (KeyError, ValueError):
            continue
        add(rec.get("profile"), start, now, _profile_rate(repo, rec.get("profile")))
    for entry in per.values():
        entry["day_spent"] = round(entry["day_spent"], 2)
        entry["month_spent"] = round(entry["month_spent"], 2)
    return {"day_spent": round(totals["day"], 2), "month_spent": round(totals["month"], 2), "profiles": per}


def budget_check(colab, repo: Path, cfg: dict, profile: dict, hours: float | None = None, profile_name: str | None = None, caps_override: dict | None = None, now_dt: datetime | None = None) -> dict:
    """Project this lease's window cost against the daily/monthly
    budgets. Spend is attributed (see the accounting note above):
    day/month/profile figures are this repo's own lease burn from
    the segment journal plus live records — balance movements the
    repo did not cause (external usage, credit expirations) never
    enter them. The balance itself remains a floor: a window whose
    projected cost exceeds the whole balance refuses
    ("balance-insufficient"), so a real credit loss still cannot be
    overspent. The projected window is `hours` when given (the
    acquire's effective window), else the config max_hours. The
    standing (lobby) profile additionally answers to
    standing.daily_cu_cap (reason "standing-budget"); that sub-cap
    is only ever checked against the standing profile, so a burst
    acquire can never be refused by it.

    Pricing is per profile: the profile's own rate_cu_per_hour (the
    spec catalog in config/profiles.json) wins; a profile without one
    falls back to the config rate map by tier. A profile whose rate is
    explicitly null has an unconfirmed rate: no projection is
    fabricated — the check returns ok with "rate_unconfirmed": true
    and a null projection.

    `caps_override` (a grant's snapshot caps, served shape) replaces
    the config caps for this check; without it the live config caps
    price the window, exactly as before the envelope.

    Parallel instances: when the profile has a parallel sibling with
    a live record, the sibling's remaining window (rate x time to
    its deadline) is added to this window's projection for the
    floor and the daily/monthly caps, and reported as
    "sibling_projected" — the pair is priced as the sum it commits.
    Returns {"ok": bool, ...}."""
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
    balance = parse_balance(usage)
    if balance is None:
        return {"ok": False, "reason": "usage-unparseable", "usage": usage.strip()[-200:]}
    if "rate_cu_per_hour" in profile:
        rate = profile.get("rate_cu_per_hour")
    else:
        rate = cfg.get("rate_cu_per_hour", {}).get("high_mem" if profile.get("high_mem") else "standard", 0.26)
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
    profile_day_spent = spend["profiles"].get(profile_name, {}).get("day_spent", 0.0) if profile_name is not None else None
    if projected is None:
        out = {"ok": True, "balance": balance, "projected": None, "rate_unconfirmed": True, "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
        if profile_day_spent is not None:
            out["profile_day_spent"] = round(profile_day_spent, 2)
        return out
    # Parallel instances: the sibling instance's remaining window is
    # already committed burn, so it prices into this guard alongside
    # the new window — two concurrent windows must not each pass a
    # guard that their sum would fail. The sibling's elapsed burn is
    # already inside day/month spent via its live record.
    sibling_projected = _sibling_projection(repo, profile_name, now_dt or datetime.now(timezone.utc)) if profile_name is not None else 0.0
    committed = projected + sibling_projected
    if committed > balance:
        # The floor: caps throttle the repo's own burn, but no
        # window may promise more than the account actually holds.
        out = {"ok": False, "reason": "balance-insufficient", "projected": round(projected, 2), "balance": balance, "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
        if sibling_projected:
            out["sibling_projected"] = round(sibling_projected, 2)
        return out
    daily_cap = cap("daily", cfg.get("daily_cu_budget"))
    monthly_cap = cap("monthly", cfg.get("monthly_cu_budget"))
    if daily_cap is not None and day_spent + committed > daily_cap:
        out = {"ok": False, "reason": "daily-budget", "day_spent": round(day_spent, 2), "projected": round(projected, 2), "cap": daily_cap, "balance": balance}
        if sibling_projected:
            out["sibling_projected"] = round(sibling_projected, 2)
        return out
    if monthly_cap is not None and month_spent + committed > monthly_cap:
        out = {"ok": False, "reason": "monthly-budget", "month_spent": round(month_spent, 2), "projected": round(projected, 2), "cap": monthly_cap, "balance": balance}
        if sibling_projected:
            out["sibling_projected"] = round(sibling_projected, 2)
        return out
    standing_cfg = cfg.get("standing", {})
    if profile_name is not None and profile_name == standing_cfg.get("profile", "lobby"):
        standing_cap = cap("standing_daily", standing_cfg.get("daily_cu_cap"))
        if standing_cap is not None and profile_day_spent is not None and profile_day_spent + projected > float(standing_cap):
            return {"ok": False, "reason": "standing-budget", "profile": profile_name, "profile_day_spent": round(profile_day_spent, 2), "projected": round(projected, 2), "cap": standing_cap, "balance": balance}
    out = {"ok": True, "balance": balance, "projected": round(projected, 2), "day_spent": round(day_spent, 2), "month_spent": round(month_spent, 2)}
    if sibling_projected:
        out["sibling_projected"] = round(sibling_projected, 2)
    if profile_day_spent is not None:
        out["profile_day_spent"] = round(profile_day_spent, 2)
    return out


def _sibling_projection(repo: Path, profile_name: str, now_dt: datetime) -> float:
    """The parallel sibling's remaining committed window cost: its
    catalog rate x the time left to its record's deadline. Zero when
    there is no sibling, no sibling record, an expired sibling
    window, or an unconfirmed sibling rate (nothing fabricated)."""
    sibling = parallel_sibling(repo, profile_name)
    if sibling is None:
        return 0.0
    rec = load_record(repo, sibling)
    if rec is None:
        return 0.0
    try:
        remaining_h = (parse_dt(rec["deadline"]) - now_dt).total_seconds() / 3600.0
    except (KeyError, ValueError):
        return 0.0
    if remaining_h <= 0:
        return 0.0
    rate = _profile_rate(repo, sibling)
    return rate * remaining_h if rate is not None else 0.0


def local_sessions_path() -> Path:
    """The local CLI's session-bindings file. Its contents are never
    printed or logged: the release fallback reads one endpoint from it,
    and a driver-1 handover merges a child binding into it."""
    return Path.home() / ".config" / "colab-cli" / "sessions.json"


def stop_session_safe(colab, session: str) -> str:
    """Stop a session; never raise. `colab stop` hangs on a wedged kernel
    (measured), so fall back to a direct server-side unassign through the
    CLI's own library using the endpoint in the local binding."""
    try:
        r = colab("stop", "-s", session, timeout=120)
        if r.returncode == 0 and not session_alive(colab, session):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        binding = load_json(local_sessions_path(), {})
        endpoint = binding[session]["endpoint"]
        venv_py = Path.home() / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / "python"
        env = dict(os.environ)
        env["UNASSIGN_ENDPOINT"] = endpoint
        for key in ("NO_PROXY", "no_proxy"):
            suffix = "google.com,googleapis.com,googleusercontent.com,colab.dev"
            env[key] = f"{env[key]},{suffix}" if env.get(key) else suffix
        subprocess.run(
            [str(venv_py), "-c", "import os; from colab_cli.common import state; state.client.unassign(os.environ['UNASSIGN_ENDPOINT'])"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        return "unassigned-fallback"
    except Exception:  # noqa: BLE001
        return "stop-failed"


# The heartbeat read is deliberately short-bounded: a healthy download
# of the tiny keepalive log completes in ~2 s (measured 2026-11-01),
# but the file channel can stall indefinitely on the egress proxy's
# IPv4 endpoint (same day's diagnosis). A stall must degrade the age
# to unknown (None) — never wedge status/state reads behind it.
HEARTBEAT_READ_TIMEOUT_S = 10


def heartbeat_age(colab, repo: Path, session: str) -> float | None:
    """Seconds since the keepalive's last heartbeat, via the file channel.
    None when there is no readable heartbeat (loop never ran, log gone,
    or the download failed) — including a download that stalls past
    HEARTBEAT_READ_TIMEOUT_S, which reads as unknown, not as an error."""
    dest = var_dir(repo) / "keepalive-tail.log"
    try:
        r = colab("download", "-s", session, "/content/keepalive.log", str(dest), timeout=HEARTBEAT_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return None
        lines = dest.read_text(errors="replace").strip().split("\n")
        ts = json.loads(lines[-1])["ts"]
        return max(0.0, datetime.now(timezone.utc).timestamp() - float(ts))
    except Exception:  # noqa: BLE001
        return None


# The keepalive launch is bounded and non-raising for the same reason
# as the heartbeat read: its upload rides the file channel that can
# stall indefinitely (2026-11-01: a 300 s upload stall failed the
# auto-acquire that had already created lease #9, then failed reap
# itself and starved the kernel pulse behind it). A failed launch is
# a result ("launch-failed"), never an exception — the lease stands,
# the pulse still fires, and the next reap retries the launch.
KEEPALIVE_UPLOAD_TIMEOUT_S = 30
KEEPALIVE_LAUNCH_TIMEOUT_S = 90


# Renewal (plan .plans/2026-11-01-lease-extension.md): a record's
# deadline is a window end. A session dies at the platform cap (~12 h
# from its own start) or early, so a window may outlive several
# sessions. A renew-flagged record is renewed by the reaper when its
# session dies mid-window or its age passes the renewal threshold
# (cap minus the overlap margin). The renewal kit lives in the VM at
# RENEWAL_KIT_REMOTE; its install rides the same stall-prone file
# channel as the keepalive, so its timeouts are bounded the same way
# and a failed install is a result, never an exception.
SESSION_CAP_HOURS = 12.0
RENEWAL_MARGIN = timedelta(minutes=30)
KIT_UPLOAD_TIMEOUT_S = 30
KIT_LAUNCH_TIMEOUT_S = 90
HANDOVER_READ_TIMEOUT_S = 30
RENEWAL_KIT_REMOTE = "/content/renewal-kit"
BRIDGE_KIT_REMOTE = "/content/bridge-kit"
BRIDGE_REMOTE = "/content/bridge"
BRIDGE_PROTOCOL = 1
BRIDGE_READ_TIMEOUT_S = 10
BRIDGE_INSTALL_COOLDOWN_S = 600
BRIDGE_HEARTBEAT_STALE_S = 45


def ensure_keepalive(colab, repo: Path, session: str) -> dict:
    """Launch the detached keepalive loop on the session (single-instance
    on the VM side; safe to call repeatedly). The loop exists because an
    idle Colab runtime is reaped — the first away lease died at ~3 h —
    so a leased session must never sit truly idle (operator ruling, 2026-10-30).
    Never raises: a stalled or failed launch returns launch-failed and
    the caller carries on (the reaper retries on its next cycle)."""
    cfg = load_config(repo).get("keepalive", {})
    if not cfg.get("enabled"):
        return {"keepalive": "disabled"}
    try:
        colab("upload", "-s", session, str(repo / "snippets" / "keepalive.py"), "/content/keepalive.py", timeout=KEEPALIVE_UPLOAD_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"keepalive": "launch-failed", "stage": "upload"}
    launcher = var_dir(repo) / "launch-keepalive.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    max_cycles = os.environ.get("KEEPALIVE_MAX_CYCLES", "0")
    interval = os.environ.get("KEEPALIVE_INTERVAL_SECONDS", str(cfg.get("interval_seconds", 60)))
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        f"env.update({{'COLAB_RELEASE_TAG': 'keepalive', 'KEEPALIVE_MAX_CYCLES': '{max_cycles}', 'KEEPALIVE_INTERVAL_SECONDS': '{interval}'}})\n"
        "subprocess.Popen(['nohup', 'python3', 'keepalive.py'], stdout=open('keepalive.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('keepalive launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KEEPALIVE_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"keepalive": "launch-failed", "stage": "launch"}
    return {"keepalive": "launched" if r.returncode == 0 else "launch-failed", "stdout_tail": r.stdout.strip()[-120:]}


def keepalive_fresh(colab, repo: Path, session: str) -> tuple[bool, float | None]:
    cfg = load_config(repo).get("keepalive", {})
    age = heartbeat_age(colab, repo, session)
    return (age is not None and age <= float(cfg.get("stale_seconds", 600))), age


def kernel_pulse(colab, repo: Path, session: str) -> str:
    """One trivial execution in the session's kernel. The detached
    keepalive loop cannot produce kernel activity, and lease #2 died at
    ~6 h with the loop beating perfectly — so the reaper pulses the
    kernel itself (operator ruling, 2026-10-30). Returns ok | busy | failed; a
    local timeout reads as busy, because a kernel that cannot take the
    pulse is a kernel already executing, which is the activity being
    proven. The pulse body is an inert assignment."""
    pulse = var_dir(repo) / "lease-pulse.py"
    pulse.parent.mkdir(parents=True, exist_ok=True)
    pulse.write_text("__lease_pulse__ = 'ok'\nprint('lease pulse')\n")
    try:
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(pulse), timeout=90)
        return "ok" if r.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        return "busy"
    except Exception:  # noqa: BLE001
        return "failed"


def install_renewal_kit(colab, repo: Path, rec: dict, now_dt: datetime | None = None) -> dict:
    """Install the renewal kit in a renew-flagged lease's session
    (driver 1 of the lease-extension plan): snippets/renewal.py plus
    its guard, the window config, and the account token — staged under
    /content/renewal-kit and placed by the snippet's own --setup step,
    which builds the CLI venv in-VM and moves the token to its
    canonical path at mode 600. Token contents are never printed,
    logged, or written anywhere but the two canonical locations (the
    exposure is the plan's stated design property, proven 2026-11-01).

    Bounded and non-raising like the keepalive launch it rides beside:
    a stalled or failed step returns {"kit": "install-failed", ...} —
    the lease stands, and the reaper's driver 2 covers a kit that
    never comes up. {"kit": "installed"} means the files were
    delivered and the setup launch was accepted; the snippet's own
    ready marker in-VM is the deeper proof."""
    session = rec["session"]
    token_src = Path.home() / ".config" / "colab-cli" / "token.json"
    if not token_src.exists():
        return {"kit": "install-failed", "stage": "token-missing"}
    profile = load_profiles(repo).get(rec.get("profile"), {})
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    cfg_local = var / f"renewal-config-{rec.get('profile')}.json"
    cfg_local.write_text(json.dumps({
        "profile": rec.get("profile"),
        "session": session,
        "deadline": rec.get("deadline"),
        "acquired_at": rec.get("acquired_at"),
        "cap_seconds": SESSION_CAP_HOURS * 3600,
        "margin_seconds": RENEWAL_MARGIN.total_seconds(),
        "high_mem": bool(profile.get("high_mem")),
        "gpu": profile.get("gpu"),
    }, indent=2))
    if not _ensure_remote_dirs(colab, repo, session, [RENEWAL_KIT_REMOTE], "renewal"):
        return {"kit": "install-failed", "stage": "mkdir"}
    uploads = [
        (str(repo / "snippets" / "renewal.py"), f"{RENEWAL_KIT_REMOTE}/renewal.py"),
        (str(repo / "snippets" / "_remote_guard.py"), f"{RENEWAL_KIT_REMOTE}/_remote_guard.py"),
        (str(token_src), f"{RENEWAL_KIT_REMOTE}/token.json"),
        (str(cfg_local), f"{RENEWAL_KIT_REMOTE}/renewal.json"),
    ]
    for src, dest in uploads:
        try:
            r = colab("upload", "-s", session, src, dest, timeout=KIT_UPLOAD_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return {"kit": "install-failed", "stage": "upload"}
        if r.returncode != 0:
            return {"kit": "install-failed", "stage": "upload"}
    launcher = var / f"launch-renewal-kit-{rec.get('profile')}.py"
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        "env['COLAB_RELEASE_TAG'] = 'renewal-kit'\n"
        f"subprocess.Popen(['nohup', 'python3', '{RENEWAL_KIT_REMOTE}/renewal.py', '--setup', '--config', '{RENEWAL_KIT_REMOTE}/renewal.json'], stdout=open('{RENEWAL_KIT_REMOTE}/setup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('renewal kit setup launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KIT_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"kit": "install-failed", "stage": "launch"}
    if r.returncode != 0:
        return {"kit": "install-failed", "stage": "launch"}
    return {"kit": "installed"}


def acquire(colab, repo: Path, profile_name: str, now_dt: datetime | None = None, hours: float | None = None, max_hours_cap: float | None = None, via: str | None = None) -> dict:
    """Adopt the live lease for this profile or create it. The only entry
    point that ever creates a session under the lease model. Works for
    any profile in the spec catalog; each profile's lease lives in its
    own record, so tiers coexist.

    `hours` sets this lease's window: the deadline is now + hours
    and the budget projection prices that window, exactly as given —
    an explicit window is the operator's instruction, never silently
    shortened. Without `hours` the window is the config max_hours.
    A caller may pass `max_hours_cap` to clamp its own windows (the
    standing duty passes its session cap); that parameter is the
    only clamp.

    Envelope attribution (two-tier envelope plan): when a grant is
    active and this profile is inside its scope, a created or
    adopted record is stamped with the grant's id (`envelope_id` =
    its granted_at). A created record also carries the requester
    tag `via` when the caller supplies one (a demand request's tag,
    "panel", "runner", "standing"); an adopted record keeps the
    via of whoever created it. While a grant is active, an
    out-of-scope creation carries neither stamp — attribution is
    envelope-scoped, so a lease outside the envelope is
    indistinguishable from an ordinary lease.

    Adopt-foreign (parallel instances): an instance profile with no
    record whose session already exists live — created outside the
    lease, e.g. by a direct CLI call — is adopted instead of created:
    the budget check prices a fresh window from now, the record is
    written against the existing session, and keepalive + pulse
    prove it, with no `new` and no `stop` issued against it. An
    unreadable session list refuses (acquire-unknown), never acts."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    profiles = catalog_profiles(repo, colab)
    if profile_name not in profiles:
        return {"action": "unknown-profile", "profile": profile_name}
    profile = profiles[profile_name]
    session = profile["session"]
    grant = active_grant(repo, now_dt)
    in_scope = profile_in_grant(grant, profile_name)
    rec = load_record(repo, profile_name)
    if rec:
        state = session_state(colab, session)
        if state == "unknown":
            # Never stop, delete, or recreate on an unreadable session
            # list: on 2026-11-01 an auth-dead CLI made a live lease
            # read as dead through the old boolean check and its
            # record was destroyed under a healthy session (the
            # 2026-10-29 incident's class). The record stays exactly
            # as it is; a later call retries.
            return {"action": "acquire-unknown", "record": rec, "session": session}
        if now_dt < parse_dt(rec["deadline"]) and state == "alive":
            rec["last_used_at"] = now_dt.isoformat()
            newly_attributed = in_scope and rec.get("envelope_id") != grant["granted_at"]
            if newly_attributed:
                rec["envelope_id"] = grant["granted_at"]
            save_record(repo, rec)
            if newly_attributed:
                _note_envelope_lease_started(repo, rec)
            fresh, _age = keepalive_fresh(colab, repo, session)
            ka = {"keepalive": "ok"} if fresh else ensure_keepalive(colab, repo, session)
            return {"action": "adopted", "record": rec, **ka}
        stop_session_safe(colab, session)  # expired or confirmed dead: clear before recreating
        if session_state(colab, session) == "alive":
            # The stop did not take: keep the record of a session that
            # is still billing (release()'s rule, same reason).
            return {"action": "acquire-stop-failed", "record": rec, "session": session}
        append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
        save_record(repo, None, profile=profile_name)
    effective_hours = float(hours) if hours is not None else float(cfg.get("max_hours", 8))
    if max_hours_cap is not None:
        # Only a caller passing its own cap clamps the window. Until
        # 2026-11-01 the config max_hours clamped every acquire, so
        # an ordered 24 h window silently came up as 8 h.
        effective_hours = min(effective_hours, float(max_hours_cap))
    # In-scope acquires price against the envelope's snapshot caps,
    # never the live config (Step 3): the grant tap authorized those
    # numbers. Out-of-scope and no-grant pricing is unchanged.
    caps_override = grant_caps(grant) if in_scope else None
    budget = budget_check(colab, repo, cfg, profile, hours=effective_hours, profile_name=profile_name, caps_override=caps_override, now_dt=now_dt)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    # Adopt-foreign: an instance profile whose session is already
    # live takes the record below without a `new` (see the docstring).
    adopted_foreign = False
    if profile.get("parallel_of"):
        fstate = session_state(colab, session)
        if fstate == "unknown":
            return {"action": "acquire-unknown", "session": session}
        adopted_foreign = fstate == "alive"
    if not adopted_foreign:
        args = ["new", "-s", session] + _session_shape_args(profile)
        r = colab(*args, timeout=600)
        if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
            return {"action": "create-failed", "error": (r.stderr or r.stdout).strip()[-300:]}
    deadline = now_dt + timedelta(hours=effective_hours)
    rec = {
        "profile": profile_name,
        "session": session,
        "acquired_at": now_dt.isoformat(),
        "deadline": deadline.isoformat(),
        "last_used_at": now_dt.isoformat(),
        "jobs": [],
        "budget_at_acquire": budget,
    }
    if adopted_foreign:
        rec["adopted_foreign"] = True
    if in_scope:
        rec["envelope_id"] = grant["granted_at"]
    if via and (grant is None or in_scope):
        rec["via"] = via
    if effective_hours > SESSION_CAP_HOURS:
        # A window longer than one session's life is a window lease
        # from birth (lease-extension plan): the reaper renews the
        # session under this same deadline.
        rec["renew"] = True
    save_record(repo, rec)
    ka = ensure_keepalive(colab, repo, session)
    pulse_result = kernel_pulse(colab, repo, session)
    rec["last_pulse_at"] = now_dt.isoformat()
    rec["last_pulse_result"] = pulse_result
    save_record(repo, rec)
    out = {"action": "adopted-foreign" if adopted_foreign else "created", "record": rec, "pulse": pulse_result, **ka}
    if rec.get("renew"):
        kit = install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            save_record(repo, rec)
    if in_scope:
        _note_envelope_lease_started(repo, rec)
    return out


def extend(colab, repo: Path, profile_name: str, hours: float, now_dt: datetime | None = None) -> dict:
    """Move a lease's deadline later by `hours` without touching its
    session (plan .plans/2026-11-01-lease-extension.md). The budget
    guard prices only the delta (the profile's rate x hours) before
    anything changes; a refusal changes nothing. A successful
    extension sets renew: true — the lease becomes a window lease and
    the reaper renews its session as needed to reach the new end —
    and, while the session is alive, installs the renewal kit if it
    is not already installed.

    Refusals: extend-no-lease (no record, or the session is confirmed
    absent on a non-window lease — the lease is over and a fresh start
    is a new lease), extend-unknown (session state unreadable; never
    act on an unreadable list), extend-standing-managed (the lobby's
    lifetime belongs to the grant), refused-budget."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    rec = load_record(repo, profile_name)
    if rec is None:
        return {"action": "extend-no-lease"}
    standing_profile = cfg.get("standing", {}).get("profile", "lobby")
    if profile_name == standing_profile and (rec.get("standing") or grant_active(load_grant(repo), now_dt)):
        return {"action": "extend-standing-managed"}
    state = session_state(colab, rec["session"])
    if state == "unknown":
        return {"action": "extend-unknown", "session": rec["session"]}
    if state == "absent" and not rec.get("renew"):
        return {"action": "extend-no-lease", "session": rec["session"]}
    profile = catalog_profiles(repo)[profile_name]
    budget = budget_check(colab, repo, cfg, profile, hours=float(hours), profile_name=profile_name, now_dt=now_dt)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    old_deadline = rec["deadline"]
    new_deadline = (parse_dt(old_deadline) + timedelta(hours=float(hours))).isoformat()
    rec["deadline"] = new_deadline
    rec["renew"] = True
    save_record(repo, rec)
    out = {"action": "extended", "profile": profile_name, "hours": float(hours), "old_deadline": old_deadline, "new_deadline": new_deadline, "record": rec, "budget": budget}
    if state == "alive" and rec.get("kit") != "installed":
        kit = install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            save_record(repo, rec)
    return out


def extend_grant(colab, repo: Path, hours: float, now_dt: datetime | None = None) -> dict:
    """Move the two-tier grant's `until` later by `hours`; the
    standing duty follows the new end on its own. Only the lobby's
    delta is priced (lobby rate x hours) — against the standing
    sub-cap and the global caps; the burst is neither extended nor
    charged. An absent or expired grant refuses (grant-inactive): a
    new window is a new grant."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    grant = load_grant(repo)
    if not grant_active(grant, now_dt):
        return {"action": "grant-inactive"}
    lobby_profile = grant.get("lobby_profile") or cfg.get("standing", {}).get("profile", "lobby")
    profile = load_profiles(repo)[lobby_profile]
    budget = budget_check(colab, repo, cfg, profile, hours=float(hours), profile_name=lobby_profile, now_dt=now_dt)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    old_until = grant["until"]
    grant["until"] = (parse_dt(old_until) + timedelta(hours=float(hours))).isoformat()
    p = grant_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=2))
    return {"action": "grant-extended", "hours": float(hours), "old_until": old_until, "new_until": grant["until"], "grant": grant, "budget": budget}


def _job_record(repo: Path, run_id: str | None = None) -> dict | None:
    """The record a job call belongs to: the one already tracking the
    run, else the most recently used lease (existing callers invoke
    this immediately after acquiring theirs, and acquire stamps
    last_used_at)."""
    recs = all_records(repo)
    if not recs:
        return None
    if run_id is not None:
        for rec in recs:
            if run_id in rec.get("jobs", []):
                return rec
    return max(recs, key=lambda r: r.get("last_used_at", ""))


def touch_job(repo: Path, run_id: str, now_dt: datetime | None = None, profile: str | None = None) -> None:
    rec = load_record(repo, profile) if profile is not None else _job_record(repo, run_id)
    if not rec:
        return
    now_dt = now_dt or datetime.now(timezone.utc)
    if run_id not in rec.setdefault("jobs", []):
        rec["jobs"].append(run_id)
    rec["last_used_at"] = now_dt.isoformat()
    save_record(repo, rec)


def finish_job(repo: Path, run_id: str, now_dt: datetime | None = None, profile: str | None = None) -> None:
    rec = load_record(repo, profile) if profile is not None else _job_record(repo, run_id)
    if not rec:
        return
    now_dt = now_dt or datetime.now(timezone.utc)
    rec["jobs"] = [j for j in rec.get("jobs", []) if j != run_id]
    rec["last_used_at"] = now_dt.isoformat()
    save_record(repo, rec)


def release(colab, repo: Path, reason: str = "manual", profile: str | None = None, now_dt: datetime | None = None) -> dict:
    """Release one profile's lease. Without a profile: the default
    profile's record, or the sole record when only one lease exists —
    the historical single-lease behaviour."""
    rec = load_record(repo, profile) if profile is not None else load_record(repo)
    if not rec:
        return {"action": "no-lease"}
    # Release guard (lease-extension plan): re-read the record
    # immediately before stopping. An extension that landed after the
    # caller's read moves the on-disk deadline into the future, and
    # the extension wins — abort rather than stop a lease whose end
    # just moved.
    fresh = load_record(repo, rec["profile"])
    if fresh is not None and fresh.get("deadline") and rec.get("deadline"):
        try:
            moved = parse_dt(fresh["deadline"]) > parse_dt(rec["deadline"])
        except ValueError:
            moved = False
        if moved:
            return {"action": "release-aborted", "reason": reason, "guard": "deadline-moved", "deadline": fresh["deadline"], "record": fresh}
    outcome = stop_session_safe(colab, rec["session"])
    if outcome == "stop-failed":
        # Keep the record: the session may still be billing, and the
        # reaper must keep seeing (and retrying) this lease rather than
        # forgetting a live assignment.
        return {"action": "release-failed", "reason": reason, "stop": outcome, "record_kept": True}
    append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
    save_record(repo, None, profile=rec["profile"])
    _note_envelope_lease_released(repo, fresh if fresh and fresh.get("envelope_id") else rec, reason)
    return {"action": "released", "reason": reason, "stop": outcome, "was": {k: rec.get(k) for k in ("profile", "session", "acquired_at", "deadline")}}


def _session_shape_args(profile: dict) -> list[str]:
    return (["--high-mem"] if profile.get("high_mem") else []) + (["--gpu", profile["gpu"]] if profile.get("gpu") else []) + (["--tpu", profile["tpu"]] if profile.get("tpu") else [])


def _swap_session(colab, repo: Path, rec: dict, new_session: str, acquired_at: str, now_dt: datetime) -> None:
    """The record half of a renewal, shared by both drivers: only the
    session and the session's acquired_at change — deadline, jobs,
    and profile are the window's and stay put. The keepalive is
    relaunched on the new session and the kernel pulsed, as at
    acquire."""
    append_spend_segment(repo, rec, now_dt)  # the old session's segment closes here
    rec["session"] = new_session
    rec["acquired_at"] = acquired_at
    rec["last_used_at"] = now_dt.isoformat()
    save_record(repo, rec)
    ensure_keepalive(colab, repo, new_session)
    rec["last_pulse_at"] = now_dt.isoformat()
    rec["last_pulse_result"] = kernel_pulse(colab, repo, new_session)
    save_record(repo, rec)


def _adopt_handover(colab, repo: Path, cfg: dict, rec: dict, now_dt: datetime) -> dict | None:
    """Renewal driver 1 (nested): pull the handover file the in-VM
    renewal kit wrote on the parent session, merge the child's
    binding into the local sessions file programmatically (contents
    never printed), swap the record onto the child, and stop the
    parent once the child answers. Returns the renewal detail, or
    None when there is no usable handover (yet)."""
    dest = var_dir(repo) / "lease-handover.json"
    try:
        r = colab("download", "-s", rec["session"], "/content/lease-handover.json", str(dest), timeout=HANDOVER_READ_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 or not dest.exists():
        return None
    data = load_json(dest, None)
    if not isinstance(data, dict):
        return None
    child = data.get("child_session")
    binding = data.get("binding")
    if not child or not isinstance(binding, dict) or child not in binding:
        return None
    sessions_file = local_sessions_path()
    local = load_json(sessions_file, {})
    if not isinstance(local, dict):
        local = {}
    local[child] = binding[child]
    try:
        sessions_file.parent.mkdir(parents=True, exist_ok=True)
        sessions_file.write_text(json.dumps(local, indent=2))
        os.chmod(sessions_file, 0o600)
    except OSError:
        return None
    if session_state(colab, child) != "alive":
        return None  # binding merged; the child does not answer yet — retry next cycle
    old_session = rec["session"]
    rec["kit"] = "installed"  # the kit installs itself in the child
    _swap_session(colab, repo, rec, child, data.get("minted_at") or now_dt.isoformat(), now_dt)
    stop_session_safe(colab, old_session)
    return {"driver": 1, "old_session": old_session, "new_session": child}


def _renew_fresh_session(colab, repo: Path, cfg: dict, rec: dict, now_dt: datetime) -> dict | None:
    """Renewal driver 2 (reaper fallback): create a fresh session for
    the profile against the same window end and swap the record onto
    it. No budget check — the window was priced at acquire/extend
    time and renewal never re-prices it. The fresh session gets the
    renewal kit, so the next renewal can be driver 1. The old session
    is stopped once the record has swapped."""
    # The catalog, not the configured profiles alone: a parallel
    # instance's shape (and its renewal session naming) derives from
    # its base through the derived catalog entry.
    profile = catalog_profiles(repo).get(rec.get("profile"), {})
    base = profile.get("session") or rec["session"]
    new_session = f"{base}-r{int(now_dt.timestamp())}"
    try:
        r = colab(*(["new", "-s", new_session] + _session_shape_args(profile)), timeout=600)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
        return None
    old_session = rec["session"]
    _swap_session(colab, repo, rec, new_session, now_dt.isoformat(), now_dt)
    kit = install_renewal_kit(colab, repo, rec, now_dt)
    if kit.get("kit") == "installed":
        rec["kit"] = "installed"
        save_record(repo, rec)
    if old_session != new_session:
        stop_session_safe(colab, old_session)
    return {"driver": 2, "old_session": old_session, "new_session": new_session}


def reaper_events_path(repo: Path) -> Path:
    return var_dir(repo) / "reaper-events.jsonl"


def append_reaper_event(repo: Path, event: dict) -> bool:
    """Spool a core-side lifecycle event for the companion server to
    drain into the UI outbox. The outbox has exactly one writer (the
    server, which owns seq allocation), so reap() never writes it
    directly. Best-effort: a failed spool write never breaks reap —
    the reap result itself still carries the outcome."""
    try:
        p = reaper_events_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        return True
    except OSError:
        return False


def grant_ledger_path(repo: Path) -> Path:
    return var_dir(repo) / "grant-ledger.jsonl"


def append_grant_ledger(repo: Path, event: dict) -> bool:
    """Append one envelope lifecycle event to var/grant-ledger.jsonl
    (two-tier envelope plan): grant started, lease attributed,
    renewed, released, grant ended. Best-effort like the reaper
    spool — a failed ledger write never breaks the lifecycle action
    it records."""
    try:
        p = grant_ledger_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
        return True
    except OSError:
        return False


def load_grant_ledger(repo: Path) -> list[dict]:
    out = []
    try:
        lines = grant_ledger_path(repo).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _note_envelope_lease_started(repo: Path, rec: dict) -> None:
    """Record an envelope attribution in the ledger, and spool the
    envelope-lease-started event the companion drains into the UI
    outbox (the drain is kind-generic; the panel renders this kind)."""
    append_grant_ledger(repo, {"kind": "lease-attributed", "envelope_id": rec.get("envelope_id"), "profile": rec.get("profile"), "via": rec.get("via"), "session": rec.get("session"), "deadline": rec.get("deadline")})
    append_reaper_event(repo, {"kind": "envelope-lease-started", "profile": rec.get("profile"), "envelope_id": rec.get("envelope_id"), "via": rec.get("via")})


def _note_envelope_lease_released(repo: Path, rec: dict, reason: str) -> None:
    if not rec.get("envelope_id"):
        return
    append_grant_ledger(repo, {"kind": "lease-released", "envelope_id": rec.get("envelope_id"), "profile": rec.get("profile"), "via": rec.get("via"), "reason": reason})
    append_reaper_event(repo, {"kind": "envelope-lease-released", "profile": rec.get("profile"), "envelope_id": rec.get("envelope_id"), "reason": reason})


def _attempt_renewal(colab, repo: Path, cfg: dict, rec: dict, state: str, now_dt: datetime) -> dict | None:
    """One renewal attempt for a window lease. Driver 1 (handover
    adoption) is preferred when the kit is installed; driver 2 is the
    fallback — immediately when the session is gone or no kit is
    installed, and at the session cap when a kit is installed but no
    handover has landed. While the parent is alive and below its cap,
    a missing handover means the in-VM kit may still be minting: wait
    for the next cycle rather than race it into a second child.
    Never raises; None means retry next cycle."""
    try:
        result = None
        if state == "alive" and rec.get("kit") == "installed":
            result = _adopt_handover(colab, repo, cfg, rec, now_dt)
            if result is None and now_dt - parse_dt(rec["acquired_at"]) < timedelta(hours=SESSION_CAP_HOURS):
                return None
        if result is None:
            result = _renew_fresh_session(colab, repo, cfg, rec, now_dt)
        if result is not None:
            append_reaper_event(repo, {"kind": "lease-renewed", "profile": rec.get("profile"), **result})
            if rec.get("envelope_id"):
                append_grant_ledger(repo, {"kind": "lease-renewed", "envelope_id": rec.get("envelope_id"), "profile": rec.get("profile"), **result})
        return result
    except Exception:  # noqa: BLE001
        return None


def _reap_record(colab, repo: Path, cfg: dict, rec: dict, grant: dict | None, now_dt: datetime) -> dict:
    """One record's reap: the pre-two-tier reap logic (hard deadline,
    idle shutdown, death-clearing, keepalive relaunch + kernel pulse)
    plus grant-expiry enforcement for standing leases — a standing
    lease whose grant has lapsed (or vanished) is released even before
    its own deadline."""
    profile = rec.get("profile")
    state = session_state(colab, rec["session"])
    if state == "unknown":
        return {"action": "kept-unknown", "session": rec["session"]}
    if state == "absent":
        if rec.get("renew") and not rec.get("standing") and now_dt < parse_dt(rec["deadline"]):
            # Window lease whose session died mid-window: renew it
            # instead of clearing (lease-extension plan). A failed
            # renewal keeps the record — the next cycle retries, and
            # the window's end still releases normally below once it
            # passes. Standing records never take this branch.
            renewed = _attempt_renewal(colab, repo, cfg, rec, state, now_dt)
            if renewed is not None:
                return {"action": "renewed", "session": rec["session"], **renewed}
            return {"action": "renewal-failed", "session": rec["session"]}
        append_spend_segment(repo, rec, now_dt)
        _note_envelope_lease_released(repo, rec, "cleared-dead")
        save_record(repo, None, profile=profile)
        return {"action": "cleared-dead", "session": rec["session"]}
    if rec.get("standing") and not grant_active(grant, now_dt):
        rel = release(colab, repo, reason="grant-expired", profile=profile, now_dt=now_dt)
        return {"action": "released-standing-expired", "stop": rel.get("stop"), "was": rel.get("was")}
    if now_dt >= parse_dt(rec["deadline"]):
        rel = release(colab, repo, reason="deadline", profile=profile, now_dt=now_dt)
        return {"action": "released-deadline", "stop": rel.get("stop"), "was": rel.get("was")}
    idle = cfg.get("idle_shutdown_minutes")
    if idle is not None and not rec.get("jobs"):
        if now_dt - parse_dt(rec["last_used_at"]) > timedelta(minutes=float(idle)):
            rel = release(colab, repo, reason="idle", profile=profile, now_dt=now_dt)
            return {"action": "released-idle", "stop": rel.get("stop"), "was": rel.get("was")}
    renewal_failed = False
    if rec.get("renew") and not rec.get("standing"):
        # Renewal threshold: the session's age passes the platform cap
        # minus the overlap margin while the window is still open.
        if now_dt - parse_dt(rec["acquired_at"]) >= timedelta(hours=SESSION_CAP_HOURS) - RENEWAL_MARGIN:
            renewed = _attempt_renewal(colab, repo, cfg, rec, state, now_dt)
            if renewed is not None:
                return {"action": "renewed", "session": rec["session"], **renewed}
            renewal_failed = True  # the session still lives; the kept path below maintains it
    out = {"action": "kept", "session": rec["session"], "deadline": rec["deadline"], "jobs": rec.get("jobs", [])}
    if renewal_failed:
        out["renewal"] = "failed"
    if cfg.get("keepalive", {}).get("enabled"):
        fresh, age = keepalive_fresh(colab, repo, rec["session"])
        if fresh:
            out["keepalive"] = "ok"
            out["heartbeat_age_s"] = round(age, 1)
        else:
            out["keepalive"] = ensure_keepalive(colab, repo, rec["session"]).get("keepalive")
        pulse_every = float(cfg["keepalive"].get("pulse_seconds", 600))
        last_pulse = rec.get("last_pulse_at")
        due = last_pulse is None or (now_dt - parse_dt(last_pulse)).total_seconds() >= pulse_every
        if due:
            result = kernel_pulse(colab, repo, rec["session"])
            rec["last_pulse_at"] = now_dt.isoformat()
            rec["last_pulse_result"] = result
            save_record(repo, rec)
            out["pulse"] = result
        else:
            out["pulse"] = "not-due"
    return out


_REAP_PRIORITY = ("released-deadline", "cleared-dead", "released-standing-expired", "released-idle", "renewed", "renewal-failed", "kept-unknown", "kept")


def reap(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty: reap every profile's lease record. The top-level
    action/fields are the default profile's per-record result — or the
    sole record's, in a one-lease world — so the hook's existing
    parsing (kept/none/released-deadline/cleared-dead for the default
    profile) keeps its meaning; every record's outcome is also listed
    under "records". With several non-default records and no default,
    the most consequential outcome wins the top level."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    recs = all_records(repo)
    if not recs:
        return {"action": "none", "records": []}
    grant = load_grant(repo)
    results = [{"profile": rec.get("profile"), **_reap_record(colab, repo, cfg, rec, grant, now_dt)} for rec in recs]
    top = next((r for r in results if r["profile"] == default_profile(repo)), None)
    if top is None and len(results) == 1:
        top = results[0]
    if top is None:
        for action in _REAP_PRIORITY:
            top = next((r for r in results if r["action"] == action), None)
            if top is not None:
                break
    out = {k: v for k, v in top.items() if k != "profile"}
    out["records"] = results
    return out


def status(colab, repo: Path, profile: str | None = None) -> dict:
    rec = load_record(repo, profile) if profile is not None else load_record(repo)
    if not rec:
        return {"action": "no-lease"}
    out = {"action": "active" if session_alive(colab, rec["session"]) else "recorded-but-dead", "record": rec}
    if out["action"] == "active" and load_config(repo).get("keepalive", {}).get("enabled"):
        age = heartbeat_age(colab, repo, rec["session"])
        out["keepalive_heartbeat_age_s"] = round(age, 1) if age is not None else None
    return out


def grant_path(repo: Path) -> Path:
    return var_dir(repo) / "grant.json"


def load_grant(repo: Path) -> dict | None:
    grant = load_json(grant_path(repo), None)
    return grant if isinstance(grant, dict) and grant.get("kind") == "two-tier" and grant.get("until") else None


def grant_active(grant: dict | None, now_dt: datetime) -> bool:
    return grant is not None and now_dt < parse_dt(grant["until"])


def grant_scope(cfg: dict, lobby_profile: str, burst_profile: str) -> dict:
    """The scope a grant sanctions (two-tier envelope plan): the
    profiles it covers and a snapshot of the budget caps in force at
    grant time, in the shape state serves caps in. The snapshot is
    the authorization the operator's tap granted — later config
    edits move future grants and ordinary leases, never this one."""
    profiles = [lobby_profile] if lobby_profile == burst_profile else [lobby_profile, burst_profile]
    return {"caps": _served_caps(cfg), "lobby_profile": lobby_profile, "burst_profile": burst_profile, "profiles": profiles}


def grant_profiles(grant: dict) -> list:
    """The profiles inside a grant's scope. Grants written before
    the envelope carried no scope block; their sanctioned profiles
    are the grant's own lobby/burst fields."""
    scope = grant.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("profiles"), list) and scope["profiles"]:
        return list(scope["profiles"])
    return [p for p in (grant.get("lobby_profile"), grant.get("burst_profile")) if p]


def grant_caps(grant: dict) -> dict | None:
    """The grant's snapshot caps, or None for a pre-envelope grant
    (no snapshot was taken, so live config caps still price it)."""
    scope = grant.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("caps"), dict):
        return scope["caps"]
    return None


def active_grant(repo: Path, now_dt: datetime) -> dict | None:
    grant = load_grant(repo)
    return grant if grant_active(grant, now_dt) else None


def profile_in_grant(grant: dict | None, profile_name: str) -> bool:
    return grant is not None and profile_name in grant_profiles(grant)


def write_grant(repo: Path, hours: float, burst_profile: str | None = None, now_dt: datetime | None = None) -> dict:
    """Record a two-tier grant: for the next `hours`, the standing duty
    keeps the lobby tier up alongside the burst tier. The burst is the
    default profile unless --burst names another catalog profile. The
    grant is an authorization envelope: its `granted_at` is its id and
    its `scope` snapshots the profiles and budget caps the grant tap
    sanctioned."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    lobby_profile = cfg.get("standing", {}).get("profile", "lobby")
    burst = burst_profile or default_profile(repo)
    grant = {
        "kind": "two-tier",
        "granted_at": now_dt.isoformat(),
        "until": (now_dt + timedelta(hours=float(hours))).isoformat(),
        "lobby_profile": lobby_profile,
        "burst_profile": burst,
        "scope": grant_scope(cfg, lobby_profile, burst),
    }
    p = grant_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=2))
    append_grant_ledger(repo, {"kind": "grant-started", "envelope_id": grant["granted_at"], "until": grant["until"], "scope": grant["scope"]})
    return {"action": "granted", "grant": grant}


def clear_grant(repo: Path) -> None:
    grant = load_grant(repo)
    if grant is not None:
        append_grant_ledger(repo, {"kind": "grant-ended", "envelope_id": grant.get("granted_at")})
    grant_path(repo).unlink(missing_ok=True)


def ungrant(colab, repo: Path) -> dict:
    """Remove the grant and release the lobby lease it sanctioned."""
    grant = load_grant(repo)
    lobby_profile = (grant or {}).get("lobby_profile") or load_config(repo).get("standing", {}).get("profile", "lobby")
    clear_grant(repo)
    rel = release(colab, repo, reason="ungrant", profile=lobby_profile)
    return {"action": "ungranted", "lobby": rel}


def standing_duty(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty for the two-tier grant. While a grant is active, keep
    the lobby (standing) lease live: acquire it when absent or dead,
    with a session deadline of min(grant end, now + standing
    .max_session_hours); a created standing lease is marked
    "standing": true on its record, plus "warm": "pending" when
    standing.warm_on_renew is set (the warm run itself is a later
    step). When the grant has expired, release the lobby if it is a
    standing lease, clear the grant, and report standing-expired.
    With no grant on file the duty is inert — an orphaned standing
    record is the reaper's to enforce."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    standing_cfg = cfg.get("standing", {})
    lobby_profile = standing_cfg.get("profile", "lobby")
    grant = load_grant(repo)
    if grant is None:
        return {"action": "no-grant"}
    rec = load_record(repo, lobby_profile)
    if not grant_active(grant, now_dt):
        out: dict = {"action": "standing-expired", "lobby": {"action": "no-lease"}}
        if rec is not None and rec.get("standing"):
            out["lobby"] = release(colab, repo, reason="grant-expired", profile=lobby_profile)
        elif rec is not None:
            out["lobby"] = {"action": "not-standing", "session": rec["session"]}
        clear_grant(repo)
        return out
    if rec is not None:
        if not rec.get("standing"):
            # A manually acquired lobby lease already satisfies "up";
            # it is not adopted into standing semantics.
            return {"action": "satisfied-live", "session": rec["session"], "standing": False}
        if now_dt < parse_dt(rec["deadline"]) and session_alive(colab, rec["session"]):
            return {"action": "standing-kept", "session": rec["session"], "deadline": rec["deadline"], "grant_until": grant["until"]}
    max_session_hours = float(standing_cfg.get("max_session_hours", 12))
    remaining_hours = (parse_dt(grant["until"]) - now_dt).total_seconds() / 3600.0
    result = acquire(colab, repo, lobby_profile, now_dt=now_dt, hours=min(remaining_hours, max_session_hours), max_hours_cap=max_session_hours, via="standing")
    action = result.get("action")
    if action == "created":
        new_rec = result["record"]
        new_rec["standing"] = True
        if standing_cfg.get("warm_on_renew"):
            new_rec["warm"] = "pending"
        save_record(repo, new_rec)
        return {"action": "standing-acquired", "record": new_rec, "warm": new_rec.get("warm"), "grant_until": grant["until"]}
    if action == "adopted":
        return {"action": "standing-kept", "session": result["record"]["session"], "deadline": result["record"]["deadline"], "grant_until": grant["until"]}
    if action == "refused-budget":
        return {"action": "standing-refused-budget", "budget": result.get("budget")}
    return {"action": "standing-failed", "error": result.get("error", action)}


def specs(repo: Path, colab=None) -> list[dict]:
    """The spec catalog for the UI's picker: every profile with the
    fields the UI renders (identity, label, session, shape, rate,
    timeout). The UI serves costs from this list only. The label is
    the display name from the catalog (a profile id is an internal
    name; the UI shows labels); a profile without one falls back to
    its id."""
    toggles = parallel_toggles(repo)
    out = []
    for profile_id, profile in catalog_profiles(repo, colab).items():
        entry = {
            "id": profile_id,
            "label": profile.get("label") or profile_id,
            "session": profile.get("session"),
            "gpu": profile.get("gpu"),
            "tpu": profile.get("tpu"),
            "high_mem": bool(profile.get("high_mem", False)),
            "rate_cu_per_hour": profile.get("rate_cu_per_hour"),
            "timeout": profile.get("timeout"),
        }
        # The parallel block is served ready (eligibility, toggle
        # state, instance id): the UI renders it and decides nothing.
        # An instance spec names its base and is itself ineligible —
        # one extra instance per profile.
        base = profile.get("parallel_of")
        if base:
            entry["parallel"] = {"eligible": False, "enabled": True, "instance": None, "parallel_of": base}
        else:
            entry["parallel"] = {"eligible": parallel_eligible(profile), "enabled": bool(toggles.get(profile_id)), "instance": instance_profile_id(profile_id)}
        out.append(entry)
    return out


def budget_snapshot(colab, repo: Path) -> dict:
    """Read-only budget numbers for `state`: the live balance and
    spend from the attributed accounting (segment journal + live
    records), never from balance deltas."""
    cfg = load_config(repo)
    try:
        balance = parse_balance(colab("usage", timeout=120).stdout)
    except Exception:  # noqa: BLE001
        balance = None
    day_spent = month_spent = standing_day_spent = None
    if balance is not None:
        spend = attributed_spend(repo)
        day_spent = spend["day_spent"]
        month_spent = spend["month_spent"]
        # The standing profile's day spend from the same derivation
        # the guard enforces against — the number served is the
        # number enforced.
        standing_profile = cfg.get("standing", {}).get("profile")
        standing_day_spent = spend["profiles"].get(standing_profile, {}).get("day_spent", 0.0) if standing_profile else 0.0
    return {
        "balance": balance,
        "day_spent": day_spent,
        "month_spent": month_spent,
        "standing_day_spent": standing_day_spent,
        "caps": _served_caps(cfg),
    }


def _served_caps(cfg: dict) -> dict:
    """The caps in the shape state serves: config's daily/monthly
    budgets plus the standing profile's daily sub-cap."""
    standing = cfg.get("standing")
    return {
        "daily": cfg.get("daily_cu_budget"),
        "monthly": cfg.get("monthly_cu_budget"),
        "standing_daily": standing.get("daily_cu_cap") if isinstance(standing, dict) else None,
    }


_UNSET = object()


def set_budget_caps(repo: Path, daily=_UNSET, monthly=_UNSET, standing_daily=_UNSET) -> dict:
    """Update the budget caps in config/lease.json — the operator's
    throttle, set from the panel (2026-11-01: the caps are the operator's to
    set, not a fixed system wall; he holds 700+ CU banked with no
    competing use for it). Each cap is a non-negative number or None
    (no cap — the budget guard's own semantics); an omitted argument
    leaves its cap untouched. Every other config key is preserved,
    and the write is atomic (tmp sibling + rename) in the file's own
    compact sorted style, so no reader sees a half-written config."""
    path = repo / "config" / "lease.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"action": "caps-invalid", "error": "config/lease.json is unreadable"}
    if not isinstance(cfg, dict):
        return {"action": "caps-invalid", "error": "config/lease.json is not a JSON object"}
    for name, value in (("daily", daily), ("monthly", monthly), ("standing_daily", standing_daily)):
        if value is _UNSET or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return {"action": "caps-invalid", "error": f"{name} cap must be a non-negative number or null"}
    standing = cfg.get("standing")
    if standing_daily is not _UNSET and standing is not None and not isinstance(standing, dict):
        return {"action": "caps-invalid", "error": "config standing section is not an object"}
    previous = _served_caps(cfg)

    def clean(v):
        return int(v) if isinstance(v, float) and v.is_integer() else v

    if daily is not _UNSET:
        cfg["daily_cu_budget"] = None if daily is None else clean(daily)
    if monthly is not _UNSET:
        cfg["monthly_cu_budget"] = None if monthly is None else clean(monthly)
    if standing_daily is not _UNSET:
        if not isinstance(standing, dict):
            standing = cfg["standing"] = {}
        standing["daily_cu_cap"] = None if standing_daily is None else clean(standing_daily)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, path)
    return {"action": "caps-updated", "caps": _served_caps(cfg), "previous": previous}


def auth_required(repo: Path) -> dict | None:
    """The wrapper guard's stop-and-surface marker
    (var/auth-required.json): present while the stored Colab
    credential is dead and a human re-authorization is the only way
    forward. Written and cleared by bin/colab-run's guard."""
    data = load_json(var_dir(repo) / "auth-required.json", None)
    return data if isinstance(data, dict) else None


def _envelope_block(colab, repo: Path, grant: dict | None, budget: dict) -> dict | None:
    """The envelope view state serves with an active grant (two-tier
    envelope plan): the grant, its scope, the live records attributed
    to it (envelope_id = the grant's granted_at), and spend versus
    the snapshot caps.

    Spend: per-profile figures come from the attributed accounting
    (segment journal + live records) — the profile's own burn in the
    period, shared between envelope and non-envelope work on that
    profile rather than metered per envelope. The totals are the
    budget snapshot's own numbers, the same ones the guard enforces
    against."""
    if grant is None:
        return None
    scope = grant.get("scope")
    if not isinstance(scope, dict):
        # Pre-envelope grant: synthesize the view from the grant's
        # own fields; its caps are the live ones (no snapshot was
        # taken), which grant_caps reports as None.
        scope = {"caps": _served_caps(load_config(repo)), "lobby_profile": grant.get("lobby_profile"), "burst_profile": grant.get("burst_profile"), "profiles": grant_profiles(grant)}
    caps = grant_caps(grant) or scope["caps"]
    attributed = [rec for rec in all_records(repo) if rec.get("envelope_id") == grant.get("granted_at")]
    balance = budget.get("balance")
    spend = attributed_spend(repo) if balance is not None else None
    profile_spend = []
    for rec in attributed:
        if spend is None or _profile_rate(repo, rec["profile"]) is None:
            day = month = None
        else:
            entry = spend["profiles"].get(rec["profile"], {})
            day = entry.get("day_spent", 0.0)
            month = entry.get("month_spent", 0.0)
        profile_spend.append({"profile": rec["profile"], "day_spent": day, "month_spent": month})
    return {
        "id": grant.get("granted_at"),
        "grant": grant,
        "scope": scope,
        "caps": caps,
        "records": attributed,
        "spend": {"balance": balance, "day_spent": budget.get("day_spent"), "month_spent": budget.get("month_spent"), "standing_day_spent": budget.get("standing_day_spent")},
        "profile_spend": profile_spend,
    }


# --- The lobby bridge (plan: .plans/2026-11-01-lobby-bridge.md) --------------
#
# The bridge is a command/output relay living in the standing lobby:
# an in-VM agent (snippets/bridge_agent.py) executes queued command
# files — OS commands in the lobby, or relayed into bridge-minted
# child sessions — and appends every outcome to a segmented log.
# The lease core installs the beachhead under an active grant,
# re-arms it when its heartbeat dies, ferries commands and the log
# mirror through var/, and folds bridge-child segments into the
# attributed spend journal (children hold no lease record, so
# without the fold-in their burn would be invisible to the guard).


def bridge_limits(cfg: dict) -> dict:
    raw = cfg.get("bridge", {}) if isinstance(cfg.get("bridge"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_children": int(raw.get("max_children", 3)),
        "child_idle_minutes": float(raw.get("child_idle_minutes", 30)),
        "child_max_hours": float(raw.get("child_max_hours", 4)),
        "command_timeout_seconds": float(raw.get("command_timeout_seconds", 300)),
    }


def bridge_command(verb: str, cid: str | None = None, **fields) -> dict:
    """Build a signed bridge command (protocol v1): the sha256 covers
    the canonical JSON of every other field, so a partially planted
    file never validates in-VM. Shared by the client and the tests;
    the agent re-derives the same hash."""
    import hashlib
    import uuid

    cmd = {"v": BRIDGE_PROTOCOL, "id": cid or uuid.uuid4().hex, "verb": verb, **fields}
    canon = json.dumps(cmd, sort_keys=True, separators=(",", ":"))
    cmd["sha256"] = hashlib.sha256(canon.encode()).hexdigest()
    return cmd


def _bridge_arm_path(repo: Path) -> Path:
    return var_dir(repo) / "bridge-arm.json"


def _bridge_arm(repo: Path, rec: dict) -> dict:
    """The arm record ties a bridge generation to one lobby session
    lifetime: a recycled or renewed lobby (new session or new
    acquired_at) starts a new generation; an agent death inside the
    same session re-installs under the same generation, so the
    agent resumes its log segments and registry."""
    import uuid

    arm = load_json(_bridge_arm_path(repo), None)
    if not isinstance(arm, dict) or arm.get("session") != rec.get("session") or arm.get("acquired_at") != rec.get("acquired_at"):
        arm = {"session": rec.get("session"), "acquired_at": rec.get("acquired_at"), "generation": uuid.uuid4().hex, "last_install_ts": 0}
        _bridge_arm_path(repo).parent.mkdir(parents=True, exist_ok=True)
        _bridge_arm_path(repo).write_text(json.dumps(arm, indent=1))
    return arm


def _bridge_arm_stamp(repo: Path, arm: dict, now_dt: datetime) -> None:
    arm["last_install_ts"] = now_dt.timestamp()
    _bridge_arm_path(repo).write_text(json.dumps(arm, indent=1))


def bridge_status_read(colab, repo: Path, session: str) -> dict | None:
    """The agent's status.json via one bounded download; None when
    unreadable (never installed, stalled channel, recycled VM) —
    unknown, never an error."""
    dest = var_dir(repo) / "bridge-status-dl.json"
    try:
        r = colab("download", "-s", session, f"{BRIDGE_REMOTE}/status.json", str(dest), timeout=BRIDGE_READ_TIMEOUT_S)
        if r.returncode != 0 or not dest.exists():
            return None
        data = json.loads(dest.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _ensure_remote_dirs(colab, repo: Path, session: str, dirs: list[str], tag: str) -> bool:
    """Create remote directories before uploading into them: the
    file channel 500s on an upload whose parent directory does not
    exist (bridge probe, 2026-11-01 — the same defect that failed
    the renewal kit's first installs at the upload stage). One
    bounded exec running os.makedirs in the session; False on any
    failure, and the caller reports its own stage."""
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    runner = var / f"mkdir-{tag}.py"
    runner.write_text("import os\n" + "".join(f"os.makedirs({d!r}, exist_ok=True)\n" for d in dirs) + "print('dirs ready')\n")
    try:
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(runner), timeout=KIT_LAUNCH_TIMEOUT_S)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def install_bridge_kit(colab, repo: Path, rec: dict, generation: str) -> dict:
    """Install the bridge beachhead into the lobby (the renewal kit's
    pattern): the agent snippet plus its guard, the bridge config
    (session, generation, limits, the full spec catalog for mints),
    and the account token, staged under /content/bridge-kit; the
    snippet's --setup places the token, builds the CLI venv, and
    spawns the agent. Bounded and non-raising: a failure is a result,
    and the duty retries after its cooldown."""
    session = rec["session"]
    token_src = Path.home() / ".config" / "colab-cli" / "token.json"
    if not token_src.exists():
        return {"bridge_install": "install-failed", "stage": "token-missing"}
    cfg = load_config(repo)
    profiles = {}
    for name, prof in catalog_profiles(repo, colab).items():
        profiles[name] = {"high_mem": bool(prof.get("high_mem")), "gpu": prof.get("gpu"), "tpu": prof.get("tpu"), "rate_cu_per_hour": prof.get("rate_cu_per_hour")}
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    cfg_local = var / "bridge-config.json"
    cfg_local.write_text(json.dumps({"session": session, "generation": generation, "limits": bridge_limits(cfg), "profiles": profiles}, indent=2))
    if not _ensure_remote_dirs(colab, repo, session, [BRIDGE_KIT_REMOTE, f"{BRIDGE_REMOTE}/queue", f"{BRIDGE_REMOTE}/log", f"{BRIDGE_REMOTE}/results"], "bridge"):
        return {"bridge_install": "install-failed", "stage": "mkdir"}
    uploads = [
        (str(repo / "snippets" / "bridge_agent.py"), f"{BRIDGE_KIT_REMOTE}/bridge_agent.py"),
        (str(repo / "snippets" / "_remote_guard.py"), f"{BRIDGE_KIT_REMOTE}/_remote_guard.py"),
        (str(token_src), f"{BRIDGE_KIT_REMOTE}/token.json"),
        (str(cfg_local), f"{BRIDGE_KIT_REMOTE}/bridge.json"),
    ]
    for src, dest in uploads:
        try:
            r = colab("upload", "-s", session, src, dest, timeout=KIT_UPLOAD_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return {"bridge_install": "install-failed", "stage": "upload"}
        if r.returncode != 0:
            return {"bridge_install": "install-failed", "stage": "upload"}
    launcher = var / "launch-bridge-kit.py"
    launcher.write_text(
        "import os, subprocess\n"
        "env = dict(os.environ)\n"
        "env['COLAB_RELEASE_TAG'] = 'bridge-kit'\n"
        f"subprocess.Popen(['nohup', 'python3', '{BRIDGE_KIT_REMOTE}/bridge_agent.py', '--setup', '--config', '{BRIDGE_KIT_REMOTE}/bridge.json'], stdout=open('{BRIDGE_KIT_REMOTE}/setup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True, env=env)\n"
        "print('bridge kit setup launched')\n"
    )
    try:
        r = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=KIT_LAUNCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"bridge_install": "install-failed", "stage": "launch"}
    if r.returncode != 0:
        return {"bridge_install": "install-failed", "stage": "launch"}
    return {"bridge_install": "installed"}


def _bridge_fold_stop(repo: Path, line: dict, folded: set) -> bool:
    """Fold one completed bridge-child segment into the attributed
    spend journal, once per child stop line (the ferry's folded set
    is the guard). The stop line carries profile + minted_at, which
    is everything the journal's segment writer needs."""
    if line.get("kind") != "stop" or not line.get("profile") or not line.get("minted_at") or not line.get("name"):
        return False
    key = f"{line.get('gen')}:{line.get('seq')}"
    if key in folded:
        return False
    folded.add(key)
    rec = {"profile": line["profile"], "session": line["name"], "acquired_at": line["minted_at"]}
    try:
        end = parse_dt(line["ts"])
    except Exception:  # noqa: BLE001
        return False
    append_spend_segment(repo, rec, end)
    return True


def _bridge_ferry(colab, repo: Path, session: str, status: dict, now_dt: datetime) -> dict:
    """One ferry pass: mirror new log lines (by generation, segment,
    byte offset — only complete lines advance the offset), fold
    child stop lines into the spend journal, drop outbox commands
    whose terminal line reached the mirror, and plant the rest.
    The outbox is the source of truth: a command leaves it only on
    acknowledgement, and a generation change re-plants everything
    unacknowledged. Bounded and non-raising throughout."""
    var = var_dir(repo)
    var.mkdir(parents=True, exist_ok=True)
    outbox = var / "bridge-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    mirror_path = var / "bridge-mirror.jsonl"
    fstate_path = var / "bridge-ferry.json"
    fstate = load_json(fstate_path, None)
    if not isinstance(fstate, dict):
        fstate = {}
    gen = status.get("gen")
    if fstate.get("gen") != gen:
        fstate = {"gen": gen, "segment": "bridge-000001.jsonl", "offset": 0, "planted": {}, "folded": []}
        with mirror_path.open("a") as fh:
            fh.write(json.dumps({"kind": "generation", "gen": gen, "ts": now_dt.isoformat()}) + "\n")
    folded = set(fstate.get("folded", []))
    planted = dict(fstate.get("planted", {}))

    def seg_no(name: str) -> int:
        try:
            return int(str(name)[7:13])
        except (TypeError, ValueError):
            return 1

    mirrored = 0
    acked: set[str] = set()
    cur = seg_no(fstate.get("segment"))
    last = seg_no(status.get("log_segment") or fstate.get("segment"))
    dest = var / "bridge-seg-dl.jsonl"
    while cur <= last:
        name = f"bridge-{cur:06d}.jsonl"
        try:
            r = colab("download", "-s", session, f"{BRIDGE_REMOTE}/log/{name}", str(dest), timeout=30)
        except Exception:  # noqa: BLE001
            break
        if r.returncode != 0 or not dest.exists():
            break
        data = dest.read_bytes()
        start = int(fstate.get("offset", 0)) if name == fstate.get("segment") else 0
        chunk = data[start:]
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            complete = chunk[: cut + 1].decode(errors="replace")
            with mirror_path.open("a") as fh:
                fh.write(complete)
            for raw in complete.splitlines():
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mirrored += 1
                if isinstance(line, dict):
                    if line.get("id"):
                        acked.add(line["id"])
                    _bridge_fold_stop(repo, line, folded)
            fstate["offset"] = start + cut + 1
        fstate["segment"] = name
        fstate["folded"] = sorted(folded)[-500:]
        fstate_path.write_text(json.dumps(fstate, indent=1))
        cur += 1

    dropped = 0
    for path in sorted(outbox.glob("*.json")):
        if path.stem in acked:
            path.unlink(missing_ok=True)
            planted.pop(path.stem, None)
            dropped += 1
    planted_now = 0
    for path in sorted(outbox.glob("*.json")):
        if planted.get(path.stem) == gen:
            continue
        try:
            r = colab("upload", "-s", session, str(path), f"{BRIDGE_REMOTE}/queue/{path.name}", timeout=BRIDGE_READ_TIMEOUT_S * 3)
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0:
            planted[path.stem] = gen
            planted_now += 1
    fstate["planted"] = planted
    fstate_path.write_text(json.dumps(fstate, indent=1))
    return {"ferry": "ok", "mirrored": mirrored, "acked": len(acked), "outbox_dropped": dropped, "planted": planted_now}


def bridge_duty(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty (rides the standing pass): while a grant is active,
    keep the bridge armed in the lobby — install or re-arm when the
    agent's heartbeat is missing or stale (cooldown-bounded), and
    run one ferry pass when it is live. Never raises: the bridge is
    an auxiliary surface and must not take the standing duty down."""
    now_dt = now_dt or datetime.now(timezone.utc)
    try:
        cfg = load_config(repo)
        if not bridge_limits(cfg)["enabled"]:
            return {"bridge": "disabled"}
        grant = active_grant(repo, now_dt)
        if grant is None:
            return {"bridge": "no-grant"}
        lobby_profile = grant.get("lobby_profile") or cfg.get("standing", {}).get("profile", "lobby")
        rec = load_record(repo, lobby_profile)
        if not rec:
            return {"bridge": "no-lobby"}
        session = rec["session"]
        arm = _bridge_arm(repo, rec)
        status = bridge_status_read(colab, repo, session)
        age = None
        if status is not None and status.get("gen") == arm["generation"] and status.get("ts"):
            try:
                age = max(0.0, now_dt.timestamp() - parse_dt(status["ts"]).timestamp())
            except Exception:  # noqa: BLE001
                age = None
        out: dict = {"bridge": "armed" if (age is not None and age <= BRIDGE_HEARTBEAT_STALE_S) else "down", "generation": arm["generation"][:8]}
        if age is not None:
            out["heartbeat_age_s"] = round(age)
        if out["bridge"] == "armed":
            out.update(_bridge_ferry(colab, repo, session, status, now_dt))
            if status.get("auth_down"):
                out["bridge"] = "auth-down"
            return out
        if now_dt.timestamp() - float(arm.get("last_install_ts", 0)) >= BRIDGE_INSTALL_COOLDOWN_S:
            out.update(install_bridge_kit(colab, repo, rec, arm["generation"]))
            _bridge_arm_stamp(repo, arm, now_dt)
        else:
            out["bridge_install"] = "cooldown"
        return out
    except Exception as exc:  # noqa: BLE001
        return {"bridge": "error", "error": type(exc).__name__}


def _bridge_block(colab, repo: Path) -> dict | None:
    """The served bridge state for the panel/artifact: armed, the
    heartbeat's age, live children, and the queue depth — all read
    from the agent's own status file, nothing derived client-side.
    None when no grant is active (the bridge has no home then)."""
    now_dt = datetime.now(timezone.utc)
    grant = active_grant(repo, now_dt)
    if grant is None:
        return None
    cfg = load_config(repo)
    lobby_profile = grant.get("lobby_profile") or cfg.get("standing", {}).get("profile", "lobby")
    rec = load_record(repo, lobby_profile)
    if not rec:
        return {"armed": False, "reason": "no-lobby"}
    status = bridge_status_read(colab, repo, rec["session"])
    if status is None:
        return {"armed": False, "reason": "no-status"}
    age = None
    try:
        age = round(max(0.0, now_dt.timestamp() - parse_dt(status["ts"]).timestamp()))
    except Exception:  # noqa: BLE001
        pass
    return {
        "armed": age is not None and age <= BRIDGE_HEARTBEAT_STALE_S,
        "generation": str(status.get("gen", ""))[:8],
        "heartbeat_age_s": age,
        "auth_down": bool(status.get("auth_down")),
        "children": status.get("children", []),
        "queue": status.get("queue", 0),
        "processed": status.get("processed", 0),
    }


def state_records(colab, repo: Path) -> dict:
    """Portion: every lease record's status. The per-record reads
    are independent and I/O-bound (session probes, heartbeat
    reads), so they run threaded — the portion's wall time is its
    slowest record, not their sum."""
    recs = all_records(repo)

    def entry(rec):
        e = status(colab, repo, profile=rec["profile"])
        e["profile"] = rec["profile"]
        e["warm"] = rec.get("warm")
        return e

    if len(recs) <= 1:
        records = [entry(rec) for rec in recs]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(recs))) as pool:
            records = list(pool.map(entry, recs))
    return {"records": records}


def state_specs(colab, repo: Path) -> dict:
    """Portion: the spec catalog (CLI shapes read from the day
    cache; a portion read never triggers a fresh catalog parse
    beyond what specs() itself already does)."""
    _shapes, catalog_error = _cli_shapes(repo, colab)
    out = {"specs": specs(repo, colab)}
    if catalog_error:
        out["catalog_error"] = catalog_error
    return out


def state_budget(colab, repo: Path) -> dict:
    """Portion: the budget snapshot, including the balance read."""
    return {"budget": budget_snapshot(colab, repo)}


def state_grant(colab, repo: Path, budget: dict | None = None) -> dict:
    """Portion: the two-tier grant and its envelope block. The
    envelope prices on a budget snapshot; composed reads pass
    theirs in so the balance is read once."""
    grant = load_grant(repo)
    if budget is None:
        budget = budget_snapshot(colab, repo)
    return {"grant": grant, "envelope": _envelope_block(colab, repo, grant, budget)}


def state_meta(colab, repo: Path) -> dict:
    """Portion: the bridge block and any outstanding auth need."""
    out = {"bridge": _bridge_block(colab, repo)}
    auth = auth_required(repo)
    if auth:
        out["auth_required"] = auth
    return out


def state(colab, repo: Path) -> dict:
    """The UI's composed read: every lease record's status, the
    two-tier grant, the spec catalog, and the budget snapshot,
    assembled from the portion reads above. All derivation lives
    here in the core; the UI only renders."""
    budget = budget_snapshot(colab, repo)
    out = {}
    out.update(state_records(colab, repo))
    out.update(state_specs(colab, repo))
    out["budget"] = budget
    out.update(state_grant(colab, repo, budget))
    out.update(state_meta(colab, repo))
    return out


def request_path(repo: Path) -> Path:
    return var_dir(repo) / "lease-request.json"


def write_request(repo: Path, reason: str = "", profile: str | None = None, now_dt: datetime | None = None, via: str | None = None, not_before: datetime | str | None = None) -> dict:
    """Record a demand for a lease (var/lease-request.json). Any driver
    may write it (bin/colab-lease request); the auto-acquire duty is the
    only consumer. `via` is the requester tag: auto_acquire copies it
    onto the lease record it creates, so a lease started by demand is
    attributable to whoever asked. `not_before` makes the demand a
    timed start: the duty leaves it untouched until its time comes
    (two-tier envelope plan — a timed start is data the hook consumes,
    never an agent-fired command)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    req = {"requested_at": now_dt.isoformat(), "reason": reason, "profile": profile}
    if via:
        req["via"] = via
    if not_before is not None:
        req["not_before"] = not_before.isoformat() if isinstance(not_before, datetime) else parse_dt(str(not_before)).isoformat()
    p = request_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(req, indent=2))
    return {"action": "requested", "request": req}


def load_request(repo: Path) -> dict | None:
    req = load_json(request_path(repo), None)
    return req if isinstance(req, dict) and req.get("requested_at") else None


def clear_request(repo: Path) -> None:
    request_path(repo).unlink(missing_ok=True)


def auto_enabled(cfg: dict) -> bool:
    """The config seam is config/lease.json's auto_acquire.enabled; the
    COLAB_RUNNER_AUTO_ACQUIRE env var overrides it (the test harness
    uses the override so it never edits the tracked config)."""
    env = os.environ.get("COLAB_RUNNER_AUTO_ACQUIRE")
    if env is not None:
        return env == "1"
    return bool(cfg.get("auto_acquire", {}).get("enabled"))


def auto_acquire(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty, inert unless auto-acquire is enabled: when a driver
    has left a demand request and no live lease exists, acquire one
    through acquire() — the same budget guard, keepalive launch, and
    kernel pulse as a manual acquire. The request is consumed when a
    lease is live or newly created; it is kept when acquisition was
    refused or failed, so a later poll retries. Makes no colab calls at
    all while disabled or when there is no demand."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = load_config(repo)
    if not auto_enabled(cfg):
        return {"action": "auto-disabled"}
    req = load_request(repo)
    if req is None:
        return {"action": "no-demand"}
    not_before = req.get("not_before")
    if not_before:
        try:
            due_at = parse_dt(str(not_before))
        except ValueError:
            due_at = None  # an unparseable stamp never wedges the demand
        if due_at is not None and now_dt < due_at:
            # A timed start that has not come due: the request is
            # neither consumed nor failed — it simply waits for a
            # later poll. No colab calls happen on this path.
            return {"action": "not-due", "not_before": not_before, "profile": req.get("profile")}
    profiles = catalog_profiles(repo, colab)
    profile_name = req.get("profile") or default_profile(repo)
    if profile_name not in profiles:
        return {"action": "auto-failed", "error": f"unknown profile {profile_name!r}"}
    rec = load_record(repo, profile_name)
    if rec:
        state = session_state(colab, rec["session"])
        if state == "alive":
            clear_request(repo)
            return {"action": "satisfied-live", "session": rec["session"]}
        if state == "unknown":
            return {"action": "skipped-unknown", "session": rec["session"]}
        append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
        save_record(repo, None, profile=profile_name)  # confirmed dead: clear before acquiring
    result = acquire(colab, repo, profile_name, now_dt=now_dt, via=req.get("via"))
    action = result.get("action")
    if action == "created":
        clear_request(repo)
        return {"action": "auto-acquired", "session": result["record"]["session"], "deadline": result["record"]["deadline"], "reason": req.get("reason", ""), "via": req.get("via")}
    if action == "adopted":
        clear_request(repo)
        return {"action": "satisfied-live", "session": result["record"]["session"]}
    if action == "refused-budget":
        return {"action": "skipped-budget", "budget": result.get("budget")}
    if action == "acquire-unknown":
        return {"action": "skipped-unknown", "session": result.get("session")}
    return {"action": "auto-failed", "error": result.get("error", action)}
