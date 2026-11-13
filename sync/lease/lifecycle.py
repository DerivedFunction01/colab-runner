import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import foundation, catalog, callers, gate, sessions, records, spend, access, keepalive, grants, orphans, renewal, health, provisioning, identity

def acquire(colab, repo: Path, shape_id: str, now_dt: datetime | None = None, hours: float | None = None, max_hours_cap: float | None = None, via: str | None = None, caller: str | None = None, duty: bool = False, standing: bool = False, create: bool = False, prefer_session: str | None = None) -> dict:
    """Adopt a live lease at this shape or create one. The only
    entry point that ever creates a session under the lease model.
    A session is shared capacity, not a caller's possession: any
    caller may use an existing available (live, unexpired) lease
    at the shape — its own first, then an unstamped one, then any
    other caller's. The record keeps its creator's caller stamp as
    history; what attribution needs is who called, and every use
    by another caller is journaled as a claim (below), never a
    restamp. A fresh lease is created only when no available lease
    at the shape can be used, or when the call is a capacity duty
    (duty/standing): a demand the hook consumes and the standing
    duty exist to add or hold a lease of their own, so they adopt
    only their own (or an unstamped) record and otherwise create.
    An explicit create (create=True — the panel's Start) never
    adopts at all: starting is creating a parallel lease, and
    using the current one is extend's job.

    `hours` sets this lease's window: the deadline is now + hours
    and the budget projection prices that window, exactly as given —
    an explicit window is the operator's instruction, never silently
    shortened. Without `hours` the window is the config max_hours.
    A caller may pass `max_hours_cap` to clamp its own windows (the
    standing duty passes its session cap); that parameter is the
    only clamp.

    Envelope attribution (two-tier envelope plan): when a grant is
    active and this shape is inside its scope, a created or
    adopted record is stamped with the grant's id (`envelope_id` =
    its granted_at). A created record also carries the requester
    tag `via` when the caller supplies one (a demand request's tag,
    "panel", "runner", "standing"); an adopted record keeps the
    via of whoever created it. While a grant is active, an
    out-of-scope creation carries neither stamp — attribution is
    envelope-scoped, so a lease outside the envelope is
    indistinguishable from an ordinary lease.

    Caller attribution (caller-attribution plan): `caller` is the
    resolved thread claim from the wrapper seam (the seams
    validate; the core stamps what it is given). A created record
    carries it; an adopted record keeps its creator's caller,
    filling the field only when the record predates attribution
    and the adopting call supplies one. A use by a different
    caller is journaled as a claim on the session — attribution
    records who called; it never gates use.

    Session pin (funnel-hardening, 2026-11-11): `prefer_session`
    names one lease the call must adopt — the runner's --session
    seam, for lanes that must target a specific session (a gate
    that cannot execute on the session the deadline rule would
    pick). A pin is an instruction, never a hint: the named
    record is adopted when it is alive and unexpired at the
    shape, and every other outcome is a `pin-unavailable`
    refusal naming the reason — no silent substitution, and the
    named record is never cleared by a pin that cannot use it.
    An unreadable session list still reads acquire-unknown.

    Launch health (same plan): within each caller tier the pool
    prefers sessions the launch channel has not recently failed
    on (sync/lease/health.py) — the deadline rule alone kept
    adopting the one session whose upload probes were timing
    out. Health demotes; it never reorders tiers and never
    blocks: an all-degraded pool still adopts, flagged
    `health_degraded` on the result."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    shapes = catalog.catalog_shapes(repo, colab)
    if shape_id not in shapes:
        return {"action": "unknown-shape", "shape": shape_id}
    shape = shapes[shape_id]
    grant = grants.active_grant(repo, now_dt)
    in_scope = grants.shape_in_grant(repo, grant, shape_id)
    at_shape = [r for r in records.all_records(repo) if r.get("shape") == shape_id]
    if create:
        # An explicit create mints a parallel lease: the adoption
        # pool is empty by construction, so the create path below
        # runs with its budget guard and shape gate unchanged.
        pool = []
    elif duty or standing:
        # Capacity duties adopt only their own (or an unstamped)
        # record; every other call may use any available lease.
        pool = [r for r in at_shape if r.get("caller") in (caller, None)]
    else:
        pool = at_shape
    # Preference: the caller's own record, then an unstamped one
    # (adoptable by the first caller to ask — the adoption stamps
    # it, below), then any other caller's; within a tier, sessions
    # the launch channel recently failed on rank below healthy
    # ones; latest deadline breaks the remaining ties.
    health_map = health.launch_health(repo, now_dt)
    pool.sort(key=lambda r: (caller is not None and r.get("caller") == caller, r.get("caller") is None, not health_map.get(r.get("session"), {}).get("degraded", False), r.get("deadline") or ""), reverse=True)
    if prefer_session is not None:
        if create:
            return {"action": "pin-unavailable", "session": prefer_session, "reason": "a pin names a lease to adopt; it cannot combine with create"}
        # A pin narrows the pool to the named record or refuses:
        # validated here, adopted by the loop below. A dead or
        # expired pin refuses here precisely so the loop's
        # clearing branch can never destroy the named record
        # under a pin that asked for it by name; an unknown state
        # falls through to the loop, whose first check returns
        # acquire-unknown untouched.
        pinned = next((r for r in records.all_records(repo) if r.get("session") == prefer_session), None)
        if pinned is None:
            return {"action": "pin-unavailable", "session": prefer_session, "reason": "no lease record for the named session"}
        if pinned.get("shape") != shape_id:
            return {"action": "pin-unavailable", "session": prefer_session, "reason": f"named session is shape {pinned.get('shape')}, not {shape_id}"}
        pin_state = sessions.session_state(colab, prefer_session)
        if pin_state != "unknown" and not (pin_state == "alive" and now_dt < foundation.parse_dt(pinned["deadline"])):
            return {"action": "pin-unavailable", "session": prefer_session, "reason": f"named session is {pin_state} or past its deadline", "record": pinned}
        pool = [pinned]
    rec = None
    for cand in pool:
        session = cand["session"]
        state = sessions.session_state(colab, session)
        if state == "unknown":
            # Never stop, delete, or recreate on an unreadable session
            # list: on 2026-11-01 an auth-dead CLI made a live lease
            # read as dead through the old boolean check and its
            # record was destroyed under a healthy session (the
            # 2026-10-29 incident's class). The record stays exactly
            # as it is; a later call retries.
            return {"action": "acquire-unknown", "record": cand, "session": session}
        if now_dt < foundation.parse_dt(cand["deadline"]) and state == "alive":
            rec = cand
            break
        # Not usable. The caller's own (or an unstamped) record is
        # cleared here exactly as before, so its window reopens
        # for the create below; another caller's dead or expired
        # lease is left for its owner and the reaper — using a
        # session never destroys someone else's record.
        if cand.get("caller") in (caller, None):
            if state == "absent" and now_dt < foundation.parse_dt(cand["deadline"]):
                # The reap's confirmation discipline, shared
                # stamp: an unconfirmed absence clears nothing —
                # the candidate is skipped with its record
                # intact (2026-11-13: one unpinned fire cleared
                # every panel record that flickered). An expired
                # candidate needs no confirmation: its deadline
                # is record-local truth, not a reading.
                since = foundation.absent_since(repo, session)
                if since is None or (now_dt - since).total_seconds() < foundation.ABSENT_CONFIRM_S:
                    foundation.stamp_absent(repo, session, since or now_dt, now_dt)
                    continue
                foundation.clear_absent_stamp(repo, session)
            sessions.stop_session_safe(colab, session)  # expired or confirmed dead: clear before recreating
            if sessions.session_state(colab, session) == "alive":
                # The stop did not take: keep the record of a session that
                # is still billing (release()'s rule, same reason).
                return {"action": "acquire-stop-failed", "record": cand, "session": session}
            spend.append_spend_segment(repo, cand, now_dt or datetime.now(timezone.utc))
            records.delete_record(repo, session)
    if rec is not None:
        session = rec["session"]
        rec["last_used_at"] = now_dt.isoformat()
        newly_attributed = in_scope and rec.get("envelope_id") != grant["granted_at"]
        if newly_attributed:
            rec["envelope_id"] = grant["granted_at"]
        if caller and not rec.get("caller"):
            rec["caller"] = caller
        records.save_record(repo, rec)
        if newly_attributed:
            grants._note_envelope_lease_started(repo, rec)
        if keepalive._capture_session_host(repo, rec, "adopted", spool_on_first=False):
            records.save_record(repo, rec)
        fresh, _age = keepalive.keepalive_fresh(colab, repo, session)
        ka = {"keepalive": "ok"} if fresh else keepalive.ensure_keepalive(colab, repo, session)
        out = {"action": "adopted", "record": rec, **ka}
        # The provisioning ensure is per-session-lifetime and
        # reads the stamp itself: a verdict-ok stamp no-ops, an
        # attempt inside its retry window reads pending, and a
        # failed attempt past its window retries here. The call
        # must stay unconditional — an earlier no-stamp guard
        # at this site made the retry unreachable for any
        # registered session, so a first failed stamp was
        # terminal for the session's life (specimen: ember
        # r1794417373's lock-contended failure, 2026-11-11).
        out.update(provisioning.ensure_provisioning(colab, repo, session))
        # The same first-registration moment captures the
        # runtime identity (a no-op once a stamp carries one):
        # the baseline the reap cycle's replacement check
        # compares against.
        if not identity.read_stamp(repo, session):
            out.update(identity.ensure_identity(colab, repo, session, "adopted"))
        if health_map.get(session, {}).get("degraded"):
            out["health_degraded"] = True
        if caller and rec.get("caller") not in (None, caller):
            # Riding another caller's lease: the use is journaled
            # as this caller's claim on the session; the record's
            # creator stamp is untouched.
            callers.append_caller_claim(repo, session, caller, "acquire")
            out["riding"] = True
            out["owner_caller"] = rec.get("caller")
        return out
    effective_hours = float(hours) if hours is not None else float(cfg.get("max_hours", 8))
    if max_hours_cap is not None:
        # Only a caller passing its own cap clamps the window. Until
        # 2026-11-01 the config max_hours clamped every acquire, so
        # an ordered 24 h window silently came up as 8 h.
        effective_hours = min(effective_hours, float(max_hours_cap))
    # In-scope acquires price against the envelope's snapshot caps,
    # never the live config (Step 3): the grant tap authorized those
    # numbers. Out-of-scope and no-grant pricing is unchanged.
    caps_override = grants.grant_caps(grant) if in_scope else None
    budget = spend.budget_check(colab, repo, cfg, shape, hours=effective_hours, caps_override=caps_override, now_dt=now_dt, standing_lease=standing)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    # Session shape gate: the creation branch only — adopting
    # or reusing a live session above is never gated. `duty`
    # marks the lease duty creating on a demand (auto_acquire
    # is its only passer): the shape check still applies, the
    # agent creation denial does not.
    refusal = gate.enforce_session_admission(repo, shape_id, via=via, shape=shape, seam="acquire", caller=caller, duty=duty, standing=standing)
    if refusal is not None:
        return refusal
    session = sessions.generate_session_name(repo, colab)
    args = ["new", "-s", session] + catalog._session_shape_args(shape)
    r = colab(*args, timeout=600)
    if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
        return {"action": "create-failed", "error": (r.stderr or r.stdout).strip()[-300:]}
    if r.returncode == 0 and gate.session_gate_policy(repo)["enabled"]:
        # Post-start shape assertion: the shape the CLI was
        # handed is not proof of the shape the server assigned
        # (the 2026-11-04 Standard session bound under a
        # High-RAM profile name). Only a fresh create is
        # asserted — the "already" path above is a reuse, which
        # is never gated. A proven mismatch is stopped and
        # refused: no lease record is written for a session of
        # the wrong shape. The assertion is part of the gate: a
        # policy disabled in config stands it down too.
        mismatch = sessions.created_shape_mismatch(session, shape) if sessions.session_state(colab, session) == "alive" else None
        if mismatch is not None:
            stop = sessions.stop_session_safe(colab, session)
            gate.append_gate_journal(repo, {"kind": "shape-mismatch", "seam": "acquire", "shape": shape_id, "declared_high_mem": mismatch["declared_high_mem"], "actual_class": mismatch["actual_class"], "stop": stop})
            return {**mismatch, "shape": shape_id, "stop": stop}
    deadline = now_dt + timedelta(hours=effective_hours)
    rec = {
        "session": session,
        "shape": shape_id,
        "shape_spec": {"gpu": shape.get("gpu"), "tpu": shape.get("tpu"), "high_mem": bool(shape.get("high_mem"))},
        "rate_cu_per_hour": shape.get("rate_cu_per_hour"),
        "acquired_at": now_dt.isoformat(),
        "deadline": deadline.isoformat(),
        "last_used_at": now_dt.isoformat(),
        "jobs": [],
        "budget_at_acquire": budget,
    }
    if in_scope:
        rec["envelope_id"] = grant["granted_at"]
    if via and (grant is None or in_scope):
        rec["via"] = via
    if caller:
        rec["caller"] = caller
    if effective_hours > renewal.SESSION_CAP_HOURS:
        # A window longer than one session's life is a window lease
        # from birth (lease-extension plan): the reaper renews the
        # session under this same deadline.
        rec["renew"] = True
    records.save_record(repo, rec)
    ka = keepalive.ensure_keepalive(colab, repo, session)
    prov = provisioning.ensure_provisioning(colab, repo, session)
    ident = identity.ensure_identity(colab, repo, session, "acquire")
    pulse_result = keepalive.kernel_pulse(colab, repo, session)
    rec["last_pulse_at"] = now_dt.isoformat()
    rec["last_pulse_result"] = pulse_result
    records.save_record(repo, rec)
    host_changed = keepalive._capture_session_host(repo, rec, "acquire", spool_on_first=True)
    if host_changed:
        records.save_record(repo, rec)
    out = {"action": "created", "record": rec, "pulse": pulse_result, **ka, **prov, **ident}
    if host_changed:
        out["host_changed"] = True
        out["host"] = rec["host"]
    if rec.get("renew"):
        kit = renewal.install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            records.save_record(repo, rec)
    if in_scope:
        grants._note_envelope_lease_started(repo, rec)
    return out


def extend(colab, repo: Path, session: str, hours: float, now_dt: datetime | None = None, caller: str | None = None) -> dict:
    """Move one lease's deadline later by `hours` without touching
    its session (plan .plans/2026-11-01-lease-extension.md). The
    budget guard prices only the delta (the shape's rate x hours)
    before anything changes; a refusal changes nothing. A
    successful extension sets renew: true — the lease becomes a
    window lease and the reaper renews its session as needed to
    reach the new end — and, while the session is alive, installs
    the renewal kit if it is not already installed.

    Refusals: extend-no-lease (no record for the session, or the
    session is confirmed absent on a non-window lease — the lease
    is over and a fresh start is a new lease), extend-unknown
    (session state unreadable; never act on an unreadable list),
    extend-standing-managed (the standing lease's lifetime belongs
    to the grant), refused-budget."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    rec = records.load_record(repo, session)
    if rec is None:
        return {"action": "extend-no-lease"}
    if rec.get("standing") and grants.grant_active(grants.load_grant(repo), now_dt):
        return {"action": "extend-standing-managed"}
    diag: dict = {}
    state = sessions.session_state(colab, session, diag)
    if state == "unknown":
        return {"action": "extend-unknown", "session": session, "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent" and not rec.get("renew"):
        return {"action": "extend-no-lease", "session": session}
    shape = catalog.catalog_shapes(repo).get(rec.get("shape"))
    if shape is None:
        shape = {"id": rec.get("shape"), "rate_cu_per_hour": rec.get("rate_cu_per_hour"), **renewal._shape_spec_for(repo, rec.get("shape"), rec)}
    budget = spend.budget_check(colab, repo, cfg, shape, hours=float(hours), now_dt=now_dt, standing_lease=bool(rec.get("standing")), exclude_session=session)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    old_deadline = rec["deadline"]
    new_deadline = (foundation.parse_dt(old_deadline) + timedelta(hours=float(hours))).isoformat()
    rec["deadline"] = new_deadline
    rec["renew"] = True
    if caller and not rec.get("caller"):
        # Caller attribution: an extension by the owning thread
        # fills a pre-attribution record's caller; an attributed
        # record keeps its creator's caller (as on adopt).
        rec["caller"] = caller
    # An operator extension is the latest action on the
    # deadline: it clears any operator shorten stamp, so the
    # extend guard stops skipping the lease (the stamp means
    # the operator pulled the deadline in on purpose;
    # extending says otherwise). The stamp is never a cap.
    for key in ("shortened_at", "shortened_by", "shortened_from"):
        rec.pop(key, None)
    records.save_record(repo, rec)
    out = {"action": "extended", "session": session, "shape": rec.get("shape"), "hours": float(hours), "old_deadline": old_deadline, "new_deadline": new_deadline, "record": rec, "budget": budget}
    if caller:
        out["caller"] = caller
    if state == "alive" and rec.get("kit") != "installed":
        kit = renewal.install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            records.save_record(repo, rec)
    return out


def extend_grant(colab, repo: Path, hours: float, now_dt: datetime | None = None) -> dict:
    """Move the two-tier grant's `until` later by `hours`; the
    standing duty follows the new end on its own. Only the standing
    lease's delta is priced (its shape's rate x hours) — against
    the standing sub-cap and the global caps; the burst is neither
    extended nor charged. An absent or expired grant refuses
    (grant-inactive): a new window is a new grant."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    grant = grants.load_grant(repo)
    if not grants.grant_active(grant, now_dt):
        return {"action": "grant-inactive"}
    shape_id = catalog.standing_shape(repo)
    shape = catalog.catalog_shapes(repo).get(shape_id)
    standing_rec = grants.standing_record(repo)
    if shape is None:
        shape = {"id": shape_id, "rate_cu_per_hour": standing_rec.get("rate_cu_per_hour") if standing_rec else catalog.shape_rate(repo, shape_id), **renewal._shape_spec_for(repo, shape_id, standing_rec)}
    budget = spend.budget_check(colab, repo, cfg, shape, hours=float(hours), now_dt=now_dt, standing_lease=True, exclude_session=standing_rec.get("session") if standing_rec else None)
    if not budget["ok"]:
        return {"action": "refused-budget", "budget": budget}
    old_until = grant["until"]
    grant["until"] = (foundation.parse_dt(old_until) + timedelta(hours=float(hours))).isoformat()
    p = grants.grant_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(grant, indent=2))
    return {"action": "grant-extended", "hours": float(hours), "old_until": old_until, "new_until": grant["until"], "grant": grant, "budget": budget}


def adopt_orphan(colab, repo: Path, session: str, deadline=None, hours: float | None = None, caller: str | None = None, shape: str | None = None, now_dt: datetime | None = None) -> dict:
    """Bring one orphaned session back under the lease: plant a
    real record for the live session from its derived evidence,
    or at a deadline (and, when the class evidence is ambiguous,
    a shape) the operator plants explicitly. The record carries
    the acquire field shape plus the adoption provenance
    (adopted, adopted_at, deadline_source), so the reaper, the
    guard, and the budget treat it as an ordinary lease from
    here. Caller validation happens at the wrapper seam
    (caller_claim_error there); the core stamps the creator the
    evidence names and journals the operator's claim as a ride
    when the two differ, exactly as acquire's adoption path
    does. The restore is exempt from price-check when it
    restores the evidenced window — it creates no new
    commitment; a planted deadline beyond the evidenced one
    prices only the delta, and with no evidence the delta is
    measured from now."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    if records.load_record(repo, session) is not None:
        return {"action": "adopt-has-record", "session": session}
    # A torn record file for this session read as no record
    # above (its contents cannot yield one). Quarantine it
    # now (E1) — renamed aside, bytes preserved — so the
    # adoption and its save never reason about the torn file
    # in place.
    records.quarantine_torn(repo, session=session)
    if session.startswith("bridge-"):
        return {"action": "adopt-bridge-managed", "session": session}
    handover = foundation.load_json(foundation.var_dir(repo) / "lease-handover.json", None)
    if isinstance(handover, dict) and handover.get("child_session") == session:
        return {"action": "adopt-handover-pending", "session": session}
    live = sessions.live_sessions(colab, repo)
    if live is None:
        return {"action": "adopt-unknown", "session": session}
    entry = next((e for e in live if e.get("session") == session), None)
    if entry is None:
        return {"action": "adopt-absent", "session": session}
    census = orphans.derive_orphan(repo, entry)
    evidenced_deadline = orphans._dt_or_none(census.get("derived_deadline"))

    # Shape resolution (owner ruling on the plan's open
    # question): an evidenced shape is exact and is never
    # overridden; the operator's --shape is consulted only
    # when derivation cannot evidence one, and is validated
    # against the catalog; otherwise the live class must name
    # exactly one candidate, or the adoption refuses with the
    # ambiguity as evidence.
    shapes = catalog.catalog_shapes(repo, colab)
    if census.get("shape_evidenced"):
        shape_id = census["shape"]
        if shape is not None and shape != shape_id:
            return {"action": "adopt-shape-conflict", "session": session, "evidenced_shape": shape_id, "shape": shape}
    elif shape is not None:
        if shape not in shapes:
            return {"action": "adopt-shape-unknown", "session": session, "shape": shape}
        shape_id = shape
    elif census.get("shape"):
        shape_id = census["shape"]
    else:
        return {"action": "adopt-shape-ambiguous", "session": session, "shape_class": entry.get("shape_class"), "shape_candidates": census.get("shape_candidates", [])}
    shape_entry = shapes.get(shape_id)
    if shape_entry is None:
        shape_entry = {"id": shape_id, "rate_cu_per_hour": catalog.shape_rate(repo, shape_id), **(census.get("shape_spec") or renewal._shape_spec_for(repo, shape_id))}

    if deadline is not None:
        target = orphans._dt_or_none(deadline) if isinstance(deadline, str) else deadline
        if target is not None and target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        source = "manual"
    elif hours is not None:
        target = now_dt + timedelta(hours=float(hours))
        source = "hours"
    elif evidenced_deadline is not None:
        target = evidenced_deadline
        source = census.get("deadline_source") or "renewal-config"
    else:
        return {"action": "adopt-no-deadline", "session": session, "evidence": census}
    if target is None:
        return {"action": "adopt-no-deadline", "session": session, "evidence": census}
    if evidenced_deadline is not None and target < evidenced_deadline:
        # Restore rule, not an invariant: adoption puts back
        # the lease the evidence describes, it never edits it.
        # An earlier deadline would quietly rewrite the lease
        # being restored; the operator adopts at the evidenced
        # deadline and shortens explicitly afterward if that
        # is what they mean.
        return {"action": "adopt-deadline-shortens", "session": session, "deadline": target.isoformat(), "evidenced_deadline": evidenced_deadline.isoformat(), "deadline_source": census.get("deadline_source")}
    if target <= now_dt:
        return {"action": "adopt-deadline-past", "session": session, "deadline": target.isoformat(), "deadline_source": source, "derived_deadline": census.get("derived_deadline"), "derived_source": census.get("deadline_source")}
    budget = None
    base = evidenced_deadline if evidenced_deadline is not None else now_dt
    if target > base:
        delta_hours = (target - base).total_seconds() / 3600.0
        budget = spend.budget_check(colab, repo, cfg, shape_entry, hours=delta_hours, now_dt=now_dt, exclude_session=session)
        if not budget["ok"]:
            return {"action": "refused-budget", "budget": budget}

    diag: dict = {}
    state = sessions.session_state(colab, session, diag)
    if state == "unknown":
        return {"action": "adopt-unknown", "session": session, "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent":
        return {"action": "adopt-absent", "session": session}

    acquired_at = census.get("acquired_at") or now_dt.isoformat()
    record_caller = census.get("creator_caller") or caller
    rec: dict = {
        "session": session,
        "shape": shape_id,
        "shape_spec": census.get("shape_spec") or renewal._shape_spec_for(repo, shape_id),
        "rate_cu_per_hour": catalog.shape_rate(repo, shape_id),
        "acquired_at": acquired_at,
        "deadline": target.isoformat(),
        "last_used_at": now_dt.isoformat(),
        # Seed from the census's pruned entries only: adoption
        # restores live work, it does not import registrations
        # whose runs already ended.
        "jobs": [entry["id"] for entry in census.get("jobs_in_flight_entries") or []],
        "adopted": True,
        "adopted_at": now_dt.isoformat(),
        "deadline_source": source,
    }
    if record_caller:
        rec["caller"] = record_caller
    acquired_dt = orphans._dt_or_none(acquired_at) or now_dt
    if (target - acquired_dt).total_seconds() > renewal.SESSION_CAP_HOURS * 3600 or census.get("kit_installed"):
        # The window outlives any one runtime, or the kit is
        # already installed in the session: either way the
        # renewal driver owns the successor line.
        rec["renew"] = True
    if census.get("kit_installed"):
        rec["kit"] = "installed"
    if budget is not None:
        rec["budget_at_adopt"] = budget
    if records.load_record(repo, session) is not None:
        # Re-checked at the last moment, as acquire does: a
        # record that appeared mid-adoption wins.
        return {"action": "adopt-has-record", "session": session}
    records.save_record(repo, rec)
    if keepalive._capture_session_host(repo, rec, "adopted", spool_on_first=False):
        records.save_record(repo, rec)
    # The restored lease's runtime identity baselines here,
    # as at any first registration (the result is not
    # surfaced: the adoption's own output is census-shaped).
    identity.ensure_identity(colab, repo, session, "adopt")
    if caller and caller != record_caller:
        callers.append_caller_claim(repo, session, caller, "adopt")
    access.clear_unattributed(repo, session)
    out: dict = {"action": "adopted", "record": rec, "deadline_source": source}
    if budget is not None:
        out["budget"] = budget
    if rec.get("renew") and not census.get("kit_installed"):
        kit = renewal.install_renewal_kit(colab, repo, rec, now_dt)
        out["kit"] = kit
        if kit.get("kit") == "installed":
            rec["kit"] = "installed"
            records.save_record(repo, rec)
    # An orphan's adoption is a first registration too: fire
    # the provisioning ensure beside it (the ensure reads the
    # stamp itself and no-ops, pends, or retries as it decides).
    out.update(provisioning.ensure_provisioning(colab, repo, session))
    return out


def shorten(colab, repo: Path, session: str, deadline=None, hours: float | None = None, caller: str | None = None, now_dt: datetime | None = None) -> dict:
    """Move one lease's deadline earlier: the operator's
    explicit edit, extend's mirror. The target is an absolute
    --deadline or the current deadline minus --hours; it must
    land strictly earlier than the current deadline (a later
    target is extend's job, refused here by name) and strictly
    in the future (a past target is a release wearing a
    deadline's clothes). No budget price-check: shortening
    only reduces the committed projection. On success the
    record is stamped (shortened_at, shortened_by,
    shortened_from): while the stamp stands, the extend guard
    skips the lease — it never re-extends what the operator
    deliberately pulled in. The stamp is not a cap: a later
    operator extend clears it (extend's save deletes the
    stamp), and the operator's latest action wins. The
    renewal kit is reinstalled when the record carries one,
    so the kit's planted config learns the new deadline; a
    kit failure is reported in the verdict and the move
    stands, as with extend."""
    now_dt = now_dt or datetime.now(timezone.utc)
    cfg = foundation.load_config(repo)
    if not cfg.get("enabled"):
        return {"action": "lease-disabled"}
    rec = records.load_record(repo, session)
    if rec is None:
        return {"action": "shorten-no-lease", "session": session}
    if rec.get("standing") and grants.grant_active(grants.load_grant(repo), now_dt):
        return {"action": "shorten-standing-managed", "session": session}
    diag: dict = {}
    state = sessions.session_state(colab, session, diag)
    if state == "unknown":
        return {"action": "shorten-unknown", "session": session, "read_error": diag.get("read_error"), "attempts": diag.get("attempts")}
    if state == "absent" and not rec.get("renew"):
        return {"action": "shorten-no-lease", "session": session}
    if (deadline is None) == (hours is None):
        return {"action": "shorten-no-target", "session": session}
    current = foundation.parse_dt(rec["deadline"])
    if deadline is not None:
        target = orphans._dt_or_none(deadline) if isinstance(deadline, str) else deadline
        if target is not None and target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    else:
        target = current - timedelta(hours=float(hours))
    if target is None or target >= current:
        return {"action": "shorten-not-earlier", "session": session, "deadline": rec["deadline"], "target": target.isoformat() if target else None}
    if target <= now_dt:
        return {"action": "shorten-deadline-past", "session": session, "deadline": rec["deadline"], "target": target.isoformat()}
    old_deadline = rec["deadline"]
    rec["deadline"] = target.isoformat()
    rec["shortened_at"] = now_dt.isoformat()
    rec["shortened_by"] = caller
    rec["shortened_from"] = old_deadline
    records.save_record(repo, rec)
    out: dict = {"action": "shortened", "session": session, "shape": rec.get("shape"), "old_deadline": old_deadline, "new_deadline": rec["deadline"], "record": rec}
    if hours is not None:
        out["hours"] = float(hours)
    if caller:
        out["caller"] = caller
    if state == "alive" and rec.get("kit") == "installed":
        out["kit"] = renewal.install_renewal_kit(colab, repo, rec, now_dt)
    return out


def _job_record(repo: Path, run_id: str | None = None) -> dict | None:
    """The record a job call belongs to: the one already tracking the
    run, else the most recently used lease (existing callers invoke
    this immediately after acquiring theirs, and acquire stamps
    last_used_at)."""
    recs = records.all_records(repo)
    if not recs:
        return None
    if run_id is not None:
        for rec in recs:
            if run_id in rec.get("jobs", []):
                return rec
    return max(recs, key=lambda r: r.get("last_used_at", ""))


def touch_job(repo: Path, run_id: str, now_dt: datetime | None = None, session: str | None = None) -> None:
    rec = records.load_record(repo, session) if session is not None else _job_record(repo, run_id)
    if not rec:
        return
    now_dt = now_dt or datetime.now(timezone.utc)
    if run_id not in rec.setdefault("jobs", []):
        rec["jobs"].append(run_id)
    rec["last_used_at"] = now_dt.isoformat()
    records.save_record(repo, rec)


def finish_job(repo: Path, run_id: str, now_dt: datetime | None = None, session: str | None = None) -> None:
    rec = records.load_record(repo, session) if session is not None else _job_record(repo, run_id)
    if not rec:
        return
    now_dt = now_dt or datetime.now(timezone.utc)
    rec["jobs"] = [j for j in rec.get("jobs", []) if j != run_id]
    rec["last_used_at"] = now_dt.isoformat()
    records.save_record(repo, rec)


def release(colab, repo: Path, reason: str = "manual", session: str | None = None, now_dt: datetime | None = None) -> dict:
    """Release one lease, by session. Without a session: the most
    recently used record — the historical single-lease behaviour
    in a world that usually holds one lease the operator means."""
    rec = records.load_record(repo, session)
    if not rec:
        return {"action": "no-lease"}
    # Release guard (lease-extension plan): re-read the record
    # immediately before stopping. An extension that landed after the
    # caller's read moves the on-disk deadline into the future, and
    # the extension wins — abort rather than stop a lease whose end
    # just moved.
    fresh = records.load_record(repo, rec["session"])
    if fresh is not None and fresh.get("deadline") and rec.get("deadline"):
        try:
            moved = foundation.parse_dt(fresh["deadline"]) > foundation.parse_dt(rec["deadline"])
        except ValueError:
            moved = False
        if moved:
            return {"action": "release-aborted", "reason": reason, "guard": "deadline-moved", "deadline": fresh["deadline"], "record": fresh}
    outcome = sessions.stop_session_safe(colab, rec["session"])
    if outcome == "stop-failed":
        # Keep the record: the session may still be billing, and the
        # reaper must keep seeing (and retrying) this lease rather than
        # forgetting a live assignment.
        return {"action": "release-failed", "reason": reason, "stop": outcome, "record_kept": True}
    spend.append_spend_segment(repo, rec, now_dt or datetime.now(timezone.utc))
    records.delete_record(repo, rec["session"])
    grants._note_envelope_lease_released(repo, fresh if fresh and fresh.get("envelope_id") else rec, reason)
    return {"action": "released", "reason": reason, "stop": outcome, "was": {k: rec.get(k) for k in ("session", "shape", "caller", "acquired_at", "deadline")}}


def ungrant(colab, repo: Path) -> dict:
    """Remove the grant and release the standing lease it sanctioned."""
    grants.clear_grant(repo)
    rec = grants.standing_record(repo)
    if rec is None:
        return {"action": "ungranted", "standing": {"action": "no-lease"}}
    rel = release(colab, repo, reason="ungrant", session=rec["session"])
    return {"action": "ungranted", "standing": rel}
