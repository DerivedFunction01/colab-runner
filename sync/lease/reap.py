import contextlib
import fcntl
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, sessions, records, spend, access, keepalive, grants, listeners, renewal, lifecycle, identity

# --- Cycle budget ---------------------------------------------------------------
# Local why: the hook bounds a reap pass at 420 s and kills the
# wait; the pass itself carried no budget, so at peak channel load
# its sequential furniture (identity exec, freshness download,
# keepalive launch, kernel pulse, listener census — per record)
# summed past the bound and the cycle died mid-flight
# (2026-11-13, three exit-124s). Lifecycle legs always survived
# only because they happen to run first inside each record. The
# budget makes that ordering structural: lifecycle for every
# record completes before any furniture starts, furniture runs
# under a remaining-budget check that skips-and-reports instead
# of being killed, and keepalive launches execute as their own
# deferred phase. The default leaves the hook 60 s for its other
# legs and process startup.

_REAP_BUDGET_DEFAULT_S = 360.0


def _budget_s() -> float:
    raw = os.environ.get("COLAB_LEASE_REAP_BUDGET_S")
    try:
        return float(raw) if raw else _REAP_BUDGET_DEFAULT_S
    except ValueError:
        return _REAP_BUDGET_DEFAULT_S


class _CycleBudget:
    """One pass's monotonic budget. `check` is the furniture
    gate: True while time remains; on exhaustion it records the
    skipped item's name and returns False, so a skip is always
    reported, never silent."""

    def __init__(self, budget_s: float):
        self.budget_s = budget_s
        self._started = time.monotonic()
        self.skipped: list[str] = []

    def remaining(self) -> float:
        return self.budget_s - (time.monotonic() - self._started)

    def check(self, what: str) -> bool:
        if self.remaining() > 0:
            return True
        self.skipped.append(what)
        return False

    def report(self) -> dict:
        return {"budget_s": self.budget_s, "spent_s": round(time.monotonic() - self._started, 1), "skipped": self.skipped}


def _reap_lifecycle(colab, repo: Path, cfg: dict, rec: dict, grant: dict | None, now_dt: datetime, state: str, diag: dict) -> tuple[dict, bool]:
    """One record's lifecycle legs, given its state verdict:
    the pre-two-tier reap logic (hard deadline, idle shutdown,
    death-clearing, renewal) plus grant-expiry enforcement for
    standing leases — a standing lease whose grant has lapsed
    (or vanished) is released even before its own deadline.
    Returns (result, kept): kept is True when the lease survives
    and its liveness furniture is still owed."""
    if state == "unknown":
        return {"action": "kept-unknown", "session": rec["session"], "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}, False
    if state == "absent":
        if rec.get("renew") and not rec.get("standing") and now_dt < foundation.parse_dt(rec["deadline"]):
            # Window lease whose session died mid-window: renew it
            # instead of clearing (lease-extension plan). A failed
            # renewal keeps the record — the next cycle retries, and
            # the window's end still releases normally below once it
            # passes. Standing records never take this branch.
            renewed = renewal._attempt_renewal(colab, repo, cfg, rec, state, now_dt)
            if renewed is not None:
                foundation.clear_absent_stamp(repo, rec["session"])
                return {"action": "renewed", "session": rec["session"], **renewed}, False
            return {"action": "renewal-failed", "session": rec["session"]}, False
        # Deletion needs the absence confirmed across passes: a
        # single absent read is as often the session list's
        # flicker as the runtime's death (2026-11-13: adopted
        # records were cleared within one cycle of a false
        # absent while their sessions lived and worked). The
        # first absent read stamps and keeps; a later pass still
        # absent past the confirmation window clears; any live
        # read resets the clock (below).
        since = foundation.absent_since(repo, rec["session"])
        if since is None or (now_dt - since).total_seconds() < foundation.ABSENT_CONFIRM_S:
            foundation.stamp_absent(repo, rec["session"], since or now_dt, now_dt)
            return {"action": "kept-absent-unconfirmed", "session": rec["session"], "absent_since": (since or now_dt).isoformat()}, False
        foundation.clear_absent_stamp(repo, rec["session"])
        spend.append_spend_segment(repo, rec, now_dt)
        grants._note_envelope_lease_released(repo, rec, "cleared-dead")
        records.delete_record(repo, rec["session"])
        return {"action": "cleared-dead", "session": rec["session"]}, False
    foundation.clear_absent_stamp(repo, rec["session"])
    if rec.get("standing") and not grants.grant_active(grant, now_dt):
        rel = lifecycle.release(colab, repo, reason="grant-expired", session=rec["session"], now_dt=now_dt)
        return {"action": "released-standing-expired", "stop": rel.get("stop"), "was": rel.get("was")}, False
    if now_dt >= foundation.parse_dt(rec["deadline"]):
        rel = lifecycle.release(colab, repo, reason="deadline", session=rec["session"], now_dt=now_dt)
        return {"action": "released-deadline", "stop": rel.get("stop"), "was": rel.get("was")}, False
    idle = cfg.get("idle_shutdown_minutes")
    # The idle test reads the pruned projection, not the raw
    # registrations: a record whose jobs all ended without a
    # finish_job is idle-eligible, not pinned forever by ghosts.
    live = records.live_jobs(repo, rec)
    if idle is not None and not live:
        if now_dt - foundation.parse_dt(rec["last_used_at"]) > timedelta(minutes=float(idle)):
            rel = lifecycle.release(colab, repo, reason="idle", session=rec["session"], now_dt=now_dt)
            return {"action": "released-idle", "stop": rel.get("stop"), "was": rel.get("was")}, False
    renewal_failed = False
    if rec.get("renew") and not rec.get("standing"):
        # Renewal threshold: the session's age passes the platform cap
        # minus the overlap margin while the window is still open.
        if now_dt - foundation.parse_dt(rec["acquired_at"]) >= timedelta(hours=renewal.SESSION_CAP_HOURS) - renewal.RENEWAL_MARGIN:
            renewed = renewal._attempt_renewal(colab, repo, cfg, rec, state, now_dt)
            if renewed is not None:
                return {"action": "renewed", "session": rec["session"], **renewed}, False
            renewal_failed = True  # the session still lives; the kept path below maintains it
    out = {"action": "kept", "session": rec["session"], "deadline": rec["deadline"], "jobs": [entry["id"] for entry in live]}
    if renewal_failed:
        out["renewal"] = "failed"
    return out, True


def _reap_furniture(colab, repo: Path, cfg: dict, rec: dict, out: dict, now_dt: datetime) -> bool:
    """One kept record's liveness furniture: the runtime
    identity check, the keepalive freshness read, and the
    kernel pulse. Returns True when the keepalive read found
    the loop stale and a launch is owed — the launch itself is
    the caller's act (the pass defers it to its own phase; a
    stalled launch then starves nothing)."""
    # Runtime identity, on this pass's cadence: a runtime
    # replaced under this lease's name surfaces here (the
    # check spools the event and re-stamps; an unreadable
    # probe is unknown, never a verdict). Only a confirmed
    # replacement rides the result — the notable-outcome
    # precedent of the renewal flag above.
    ident = identity.check_identity(colab, repo, rec, now_dt)
    if ident.get("identity") == "replaced":
        out["runtime_identity"] = "replaced"
        out["runtime_jobs"] = ident.get("jobs", [])
        out["runtime_runs"] = ident.get("runs", [])
    launch_owed = False
    if cfg.get("keepalive", {}).get("enabled"):
        fresh, age = keepalive.keepalive_fresh(colab, repo, rec["session"])
        if fresh:
            out["keepalive"] = "ok"
            out["heartbeat_age_s"] = round(age, 1)
        else:
            launch_owed = True
        pulse_every = float(cfg["keepalive"].get("pulse_seconds", 600))
        last_pulse = rec.get("last_pulse_at")
        due = last_pulse is None or (now_dt - foundation.parse_dt(last_pulse)).total_seconds() >= pulse_every
        if due:
            result = keepalive.kernel_pulse(colab, repo, rec["session"])
            rec["last_pulse_at"] = now_dt.isoformat()
            rec["last_pulse_result"] = result
            records.save_record(repo, rec)
            out["pulse"] = result
        else:
            out["pulse"] = "not-due"
    return launch_owed


def _reap_record(colab, repo: Path, cfg: dict, rec: dict, grant: dict | None, now_dt: datetime) -> dict:
    """One record's whole reap — lifecycle, furniture, and an
    inline keepalive launch — over its own state read. The
    pass composes the same legs in phases under the cycle
    budget; this composition remains for callers that reap a
    single record directly."""
    diag: dict = {}
    state = sessions.session_state(colab, rec["session"], diag, attempts=sessions._REAP_READ_ATTEMPTS, timeout=sessions._REAP_READ_TIMEOUT_S)
    out, kept = _reap_lifecycle(colab, repo, cfg, rec, grant, now_dt, state, diag)
    if kept:
        if _reap_furniture(colab, repo, cfg, rec, out, now_dt):
            out["keepalive"] = keepalive.ensure_keepalive(colab, repo, rec["session"]).get("keepalive")
    return out


_REAP_PRIORITY = ("released-deadline", "cleared-dead", "released-standing-expired", "released-idle", "renewed", "renewal-failed", "kept-absent-unconfirmed", "kept-unknown", "kept")


@contextlib.contextmanager
def _reap_lock(repo: Path):
    """One reap pass at a time. The hook fires reap on a fixed
    poll and kills its wait at a timeout, but the reap process
    itself used to run on — the next poll then started a second
    pass over the same records, and two passes renewing the same
    lease in parallel minted duplicate successor leases
    (2026-11-08). The lock is an flock on a var/ file: held only
    while a pass runs, released by the OS when the holder dies
    (a kill included), so it can never go stale."""
    path = foundation.var_dir(repo) / "reap.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def reap(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    with _reap_lock(repo) as held:
        if not held:
            return {"action": "already-running", "records": []}
        return _reap_pass(colab, repo, now_dt)


def _reap_pass(colab, repo: Path, now_dt: datetime | None = None) -> dict:
    """Hook duty: reap every lease record. The top-level
    action/fields are the most consequential per-record result —
    or the sole record's, in a one-lease world — so the hook's
    parsing (kept/none/released-deadline/cleared-dead) keeps its
    meaning; every record's outcome is also listed under
    "records".

    The pass runs in phases under the cycle budget: one shared
    listing (the snapshot) classifies every record and feeds
    unattributed detection — which therefore still runs before
    any release, against the records as the pass found them,
    and costs no second read; then every record's lifecycle
    legs complete, unconditionally; then the furniture phases
    (per-record identity/keepalive/pulse, the deferred
    keepalive launches, the listener census) run under the
    budget's remaining time, skipping-and-reporting what does
    not fit."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    recs = records.all_records(repo)
    budget = _CycleBudget(_budget_s())
    diag: dict = {}
    snapshot = sessions.list_snapshot(colab, diag, attempts=sessions._REAP_READ_ATTEMPTS, timeout=sessions._REAP_READ_TIMEOUT_S)
    live = sessions.live_sessions(colab, repo, text=snapshot) if snapshot is not None else None
    # Unattributed detection runs even with no records at all —
    # that is precisely the world where a stray matters.
    unattributed = access.detect_unattributed(colab, repo, now_dt, live=live)
    if not recs:
        return {"action": "none", "records": [], "unattributed": unattributed, "budget": budget.report()}
    grant = grants.load_grant(repo)
    for rec in recs:
        # Host reconcile (initiation-time approval follow-up,
        # 2026-11-04): a birth capture is not the last word — the
        # record's first saves and the capture save are seconds
        # apart (keepalive + pulse run between them), and a
        # concurrent writer holding a pre-capture copy (a keepwarm
        # pulse, a reap save) restores a hostless record; an
        # adopted lease ran exactly that race and the reaper's
        # approval gate stayed blind to its host for hours while
        # the acquire output reported the capture fine.
        # Re-derive the host from the local binding every pass: a
        # fill is silent (the birth event already spooled), a real
        # change spools as origin "reconcile".
        if keepalive._capture_session_host(repo, rec, "reconcile", spool_on_first=False):
            records.save_record(repo, rec)
    results = []
    kept_pairs = []
    for rec in recs:
        state = sessions.state_in_snapshot(snapshot, rec["session"])
        out, kept = _reap_lifecycle(colab, repo, cfg, rec, grant, now_dt, state, diag)
        entry = {"session": rec.get("session"), **out}
        results.append(entry)
        if kept:
            kept_pairs.append((rec, entry))
    launch_requests = []
    for rec, entry in kept_pairs:
        if not budget.check(f"furniture:{rec['session']}"):
            entry["furniture"] = "skipped-budget"
            continue
        if _reap_furniture(colab, repo, cfg, rec, entry, now_dt):
            launch_requests.append((rec, entry))
    for rec, entry in launch_requests:
        if not budget.check(f"keepalive-launch:{rec['session']}"):
            # The next cycle's freshness read finds the loop
            # still stale and asks again — a deferral costs one
            # cycle, never the lease.
            entry["keepalive"] = "launch-deferred"
            continue
        entry["keepalive"] = keepalive.ensure_keepalive(colab, repo, rec["session"]).get("keepalive")
    # Listener census (standing hygiene, 2026-11-12): throttled
    # per session inside maybe_collect; the collector stamps its
    # own failures and never raises, and this guard keeps even a
    # collector defect out of the reap's path.
    for rec in recs:
        if not budget.check(f"listeners:{rec['session']}"):
            continue
        try:
            listeners.maybe_collect(colab, repo, rec, now_dt)
        except Exception:  # noqa: BLE001
            pass
    top = results[0] if len(results) == 1 else None
    if top is None:
        for action in _REAP_PRIORITY:
            top = next((r for r in results if r["action"] == action), None)
            if top is not None:
                break
    out = dict(top)
    out["records"] = results
    out["unattributed"] = unattributed
    out["budget"] = budget.report()
    return out
