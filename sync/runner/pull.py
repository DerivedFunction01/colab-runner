#!/usr/bin/env python3
"""The pull path: declared-output downloads at every checkpoint
cadence and once more as the authoritative final pull, plus the
terminal finalize the foreground poll loop and --poll share.
"""

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone

from .foundation import REPO, RUNS, colab, now, transfer
from .records import TERMINAL_STATES, mark_terminal, save_record


def _stamp_pull_error(rec: dict, name: str, msg: str) -> None:
    """One failed pull attempt, recorded twice over: the
    human-readable last_pull_error the record has always
    carried, and the per-name pull_failures map finalize
    partitions on — a name with a fresh failure this pass
    whose file probes present remotely is a collection
    shortfall, not an absence."""
    rec["last_pull_error"] = msg
    rec.setdefault("pull_failures", {})[name] = msg

# E6: the file channel's measured single-file reliability ceiling
# (AGENTS.md, file channel) — a declared output larger than this
# moves by the chunked route, never as one download.
SINGLE_FILE_CEILING_BYTES = 50 * 1024 * 1024


def pull_outputs(rec: dict, job: dict, session: str, remote_out: str, reader_state: dict | None = None) -> list:
    """Download the declared outputs to results/<run_id>/.

    Runs at every checkpoint cadence while the job runs — the
    manifest's checkpoint_pull_seconds is a pull cadence, not
    only a poll cadence (2026-11-07: the venue relay starved
    because outputs landed only at completion, so a 12 h job's
    log never reached the local reader waiting on it) — and once
    more after the loop as the authoritative final pull. A failed
    or timed-out download is noted on the record and never breaks
    the poll loop; a missing remote file (the job has not written
    it yet) simply pulls nothing.

    Change detection (E6): the reader reports each output's
    size/mtime; an output whose stat still equals the stat its
    last successful pull recorded is skipped — a long job's
    stable outputs are no longer re-moved whole at every cadence.
    An output above the single-file ceiling moves via
    transfer.chunked_download (32 MB parts, whole-file SHA-256
    verified) instead of one plain download.

    Entries accumulate on the record (rec["pulled"], one per
    path, latest pull winning) and the accumulated list is what
    this returns: with the skip above, a per-call list would
    leave the terminal record reporting only the outputs the
    final call happened to move — usually none.

    Shrink guard: every pull lands on a staging sibling first,
    and when the new content would replace a non-empty held
    file with FEWER bytes, the held copy is rotated aside under
    a stamped sibling name before the staging file moves in,
    with the rotation noted on the pull entry and the record.
    A remote output that restarts smaller must not silently
    erase the history already pulled (2026-11-08: after a
    wrapper resume, the venue keeper's pulled log shrank to its
    first line and the accumulated record was gone — the
    forensic read had to fall back to the venue-side lease
    log). Equal-or-growing pulls keep the single-file behavior.
    """
    pulled_now = []
    stats = (reader_state or {}).get("outputs") or {}
    stored = rec.setdefault("output_stats", {})
    for name in job.get("outputs", []):
        dest = REPO / "results" / rec["run_id"] / name
        stat = stats.get(name)
        if stat is not None and stored.get(name) == stat and dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.with_name(dest.name + ".pulling")
        entry = None
        if stat is not None and stat["size"] > SINGLE_FILE_CEILING_BYTES:
            res = transfer.chunked_download(session, f"{remote_out}/{name}", staging)
            if not res.get("ok"):
                _stamp_pull_error(rec, name, f"chunked pull of {name} failed at {now()}: {res.get('stage', 'unknown stage')}")
                staging.unlink(missing_ok=True)
                continue
            entry = {"path": name, "bytes": res["bytes"], "sha256": res["sha256"], "route": "chunked"}
        else:
            try:
                colab("download", "-s", session, f"/content/{remote_out}/{name}", str(staging), timeout=600)
            except subprocess.TimeoutExpired:
                _stamp_pull_error(rec, name, f"checkpoint pull of {name} timed out at {now()}")
                staging.unlink(missing_ok=True)
                continue
            except (SystemExit, OSError) as exc:
                # The download channel refusing or failing is a
                # failed pull, never a landed one: whatever file
                # the attempt left behind is not the output.
                _stamp_pull_error(rec, name, f"checkpoint pull of {name} failed at {now()}: {exc}")
                staging.unlink(missing_ok=True)
                continue
            if staging.exists():
                entry = {"path": name, "bytes": staging.stat().st_size, "sha256": hashlib.sha256(staging.read_bytes()).hexdigest(), "route": "single"}
                if stat is not None and entry["bytes"] != stat["size"]:
                    # Pull truthfulness: a landed size that
                    # disagrees with the reader's stat for the
                    # output is not a pull — a failed or partial
                    # download must not be certified as the
                    # artifact (2026-11-11: a completed job's
                    # 1,762-byte heartbeat was pulled as a 0-byte
                    # entry and the run read complete on it). The
                    # stored stat stays unset, so the next cadence
                    # retries instead of skipping on a false match.
                    _stamp_pull_error(rec, name, f"pull of {name} landed {entry['bytes']} bytes against a remote size of {stat['size']} at {now()}")
                    staging.unlink(missing_ok=True)
                    continue
        if entry is None:
            continue
        if dest.exists() and 0 < dest.stat().st_size > entry["bytes"]:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rotated = dest.with_name(f"{name}.rotated-{stamp}")
            suffix = 2
            while rotated.exists():
                rotated = dest.with_name(f"{name}.rotated-{stamp}-{suffix}")
                suffix += 1
            dest.rename(rotated)
            entry["rotated"] = {"to": rotated.name, "bytes": rotated.stat().st_size}
            rec["last_pull_rotation"] = f"{name} rotated to {rotated.name} at {now()}"
        staging.replace(dest)
        pulled_now.append(entry)
        if stat is not None:
            stored[name] = stat
    if pulled_now:
        rec["last_checkpoint_pull_at"] = now()
        # Accumulate into the record's pulled list, one entry
        # per path, latest pull winning: the terminal record
        # and the printed result report every output the run
        # landed, whichever cadence moved it.
        by_path = {entry["path"]: entry for entry in rec.get("pulled", [])}
        for entry in pulled_now:
            by_path[entry["path"]] = entry
        rec["pulled"] = list(by_path.values())
    return rec.get("pulled", [])


# Freshness skew for misplaced collection: a root file whose
# mtime is within this many seconds BEFORE the run's started
# stamp still counts as written during the run (fs
# granularity, and the started stamp is taken locally a few
# seconds before the payload's first write).
MISPLACED_SKEW_S = 120


def misplaced_outputs(session: str, missing: list, started_epoch: "float | None" = None) -> list:
    """Where the missing declared outputs actually sit, when
    they sit at the payload's cwd root: one bounded exec stats
    each missing name there and returns [{name, path, bytes,
    mtime, fresh}] for the ones present. The specimen class
    (2026-11-11: gate scripts writing cwd-relative paths
    landed their outputs at /content while their out dirs held
    only harness files, and the finalize record said only
    "missing") is usually a lane-script addressing slip, and
    naming where the file actually is turns a downstream hunt
    into a one-line fix. Freshness is judged session-side
    against the run's own start (its record's started stamp,
    passed in as an epoch): a file this run wrote is fresh, a
    leftover from an earlier run is stale — the root
    accumulates strays across runs, and the distinction is
    what finalize's collection rides. No started stamp, no
    freshness: the entry is named only. A failed probe yields
    []: the probe must never break a finalize."""
    if not missing:
        return []
    probe = (
        "import json, os\n"
        "BASE = '/content' if os.getcwd() == '/content' else os.getcwd()\n"
        f"NAMES = {json.dumps(list(missing))}\n"
        f"ref = {started_epoch!r}\n"
        "found = []\n"
        "for name in NAMES:\n"
        "    p = os.path.join(BASE, name)\n"
        "    if os.path.isfile(p):\n"
        "        st = os.stat(p)\n"
        f"        found.append({{'name': name, 'path': p, 'bytes': st.st_size, 'mtime': st.st_mtime, 'fresh': ref is not None and st.st_mtime >= ref - {MISPLACED_SKEW_S}}})\n"
        "print(json.dumps(found))\n"
    )
    try:
        payload = REPO / "var" / "misplaced-probe.py"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text(probe)
        r = colab("exec", "-s", session, "--timeout", "60", "-f", str(payload), timeout=90)
    except Exception:  # noqa: BLE001
        return []
    if getattr(r, "returncode", 1) != 0:
        return []
    # The real CLI's exec stdout carries a preamble (its
    # update-check notice opens with "[colab]"), so the probe's
    # JSON is read as the last line that parses as a list —
    # slicing from the first "[" reads the preamble and the
    # naming silently comes back empty (2026-11-11 live
    # specimen: both outputs present at /content, probe JSON
    # in the output, record unnamed).
    found = None
    for line in reversed(r.stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, list):
            found = parsed
            break
    if found is None:
        return []
    wanted = set(missing)
    return [e for e in found if isinstance(e, dict) and e.get("name") in wanted]


def _epoch(value) -> "float | None":
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _output_names(entries) -> list:
    """Declared-output entries reduced to the local names the
    outputs land under. Manifests declare two shapes: plain
    names, and mapped objects ({"local": name, "remote":
    path, ...}) — the local name is the basename a payload
    writes and the pull lands, so it is the name that can
    collide at a shared root. Any other shape is skipped: an
    advisory accounting must degrade, never crash (first
    version hashed the raw entries and a mapped neighbour
    manifest crashed a consumer's launch client-side,
    2026-11-12)."""
    names = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("local"), str):
                names.append(entry["local"])
    return names


def _manifest_outputs(job_name) -> list:
    """A job's declared output names from its manifest, []
    when the manifest cannot be read — an unreadable neighbour
    job must never break this run's finalize or launch."""
    try:
        manifest = json.loads((REPO / "jobs" / str(job_name) / "job.json").read_text())
    except Exception:  # noqa: BLE001
        return []
    return _output_names(manifest.get("outputs"))


def normalize_manifest_outputs(job) -> None:
    """Reduce a loaded manifest's declared outputs to plain
    local names, in place, once at load — so every downstream
    consumer (finalize, pull, the launch advisory) sees the
    string contract whether the manifest declared plain names
    or mapped entries. Mapped outputs carry no pull semantics
    of their own (no consumer ever implemented the remote /
    archive fields); the local name is the whole contract."""
    if isinstance(job.get("outputs"), list):
        job["outputs"] = _output_names(job["outputs"])


def manifest_shape_error(job) -> "str | None":
    """A loud-refusal verdict for the manifest fields that have
    no tolerance semantics: uploads and args are consumed as
    plain strings (a path to upload, an argv entry to quote),
    and a mapped entry there crashed a launch mid-flight in
    the wild (2026-11-12, dict-shaped uploads). Returns an
    error naming the field and the offending entry, or None
    when the shapes are the contract's."""
    for field in ("uploads", "args"):
        entries = job.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return f"manifest {field} must be a list of strings, got {type(entries).__name__}"
        for entry in entries:
            if not isinstance(entry, str):
                return f"manifest {field} entry is not a string: {entry!r}"
    return None


def _session_records(session: str, exclude_run_id: str) -> list:
    """Every other run record on the session (any state), read
    best-effort: a corrupt or vanishing neighbour record is
    skipped, never fatal to this run's accounting."""
    others = []
    try:
        paths = sorted(RUNS.glob("*.json"))
    except OSError:
        return others
    for path in paths:
        try:
            other = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if isinstance(other, dict) and other.get("run_id") != exclude_run_id and other.get("session") == session:
            others.append(other)
    return others


def output_name_overlaps(session: str, run_id: str, outputs: list) -> list:
    """Launch-time naming risk: this job's declared basenames
    that a LIVE run on the same session also declares, as
    [{name, shared_with}]. Declared outputs pulled from the
    per-run out dir can never collide — but a payload writing
    a declared name cwd-relative lands it at the session's
    shared root, where two live writers of one generic name
    interleave silently. The warning names the risk at launch,
    while a rename is still cheap; it blocks nothing."""
    own = _output_names(outputs)
    if not own:
        return []
    warnings = []
    for other in _session_records(session, run_id):
        try:
            if other.get("state") in ("complete", "failed", "orphaned"):
                continue
            shared = set(own) & set(_manifest_outputs(other.get("job")))
        except Exception:  # noqa: BLE001 — advisory: a malformed neighbour record degrades to no warning, never a launch failure
            continue
        for name in sorted(shared):
            warnings.append({"name": name, "shared_with": other.get("run_id")})
    return warnings


def flag_contended_misplaced(rec: dict, session: str, misplaced: list) -> None:
    """Attribution guard on the misplaced fallback (defect
    routed 2026-11-12: two lanes' drives on one session both
    wrote drive-result.json cwd-relative; the first finalize
    collected the OTHER lane's content — fresh, correctly
    sized, wrong run). A fresh root file is attributable to
    this run only when no other run on the session both
    declares the name and was executing when the file was
    written (its window, skew-tolerant, holds the mtime).
    A contended entry is flagged in place and never
    auto-collected: the bytes stay named on the record for
    the owning lane to disambiguate, where a silent delivery
    would have certified the wrong lane's result."""
    fresh = [e for e in misplaced if e.get("fresh") and e.get("mtime") is not None]
    if not fresh:
        return
    others = _session_records(session, rec.get("run_id"))
    if not others:
        return
    now_epoch = time.time()
    for entry in fresh:
        for other in others:
            if entry["name"] not in _manifest_outputs(other.get("job")):
                continue
            start = _epoch(other.get("started_at"))
            if start is None:
                continue
            if other.get("state") in TERMINAL_STATES:
                # A terminal run's window is closed: it ends at
                # its evidence — finished_at, else the freshest
                # collector evidence, else its start — never at
                # now. The open-ended price let a lane's own dead
                # first attempt (failed, no finished_at stamped)
                # refuse its re-run's fresh outputs as contended
                # over an hour after the dead run's last evidence
                # (2026-11-12, Lane 10A specimen).
                end = _epoch(other.get("finished_at")) or _epoch(other.get("last_evidence_at")) or _epoch(other.get("collector_at")) or start
            else:
                end = now_epoch
            if start - MISPLACED_SKEW_S <= entry["mtime"] <= end + MISPLACED_SKEW_S:
                entry["contended"] = True
                entry["contended_with"] = other.get("run_id")
                break


def collect_misplaced(rec: dict, session: str, misplaced: list) -> list:
    """Pull the FRESH misplaced outputs into results/<run_id>/,
    under the same truthfulness rules as the out-dir pull
    (staging sibling, landed size must equal the probed size,
    shrink rotation, one record entry per path). Stale
    entries — leftovers an earlier run wrote — are never
    collected: delivering a prior run's file as this run's
    output would be a silent wrong result, strictly worse
    than a named absence. Returns the new pull entries
    (route "misplaced"); each collected entry is flagged on
    its misplaced entry in place."""
    collected = []
    for entry in misplaced:
        entry["collected"] = False
        if entry.get("contended"):
            # Attribution is genuinely ambiguous (two live
            # writers, one shared name): named on the record,
            # never delivered as this run's output.
            continue
        if not entry.get("fresh"):
            continue
        name = entry["name"]
        dest = REPO / "results" / rec["run_id"] / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.with_name(dest.name + ".pulling")
        new_entry = None
        if entry["bytes"] > SINGLE_FILE_CEILING_BYTES:
            res = transfer.chunked_download(session, name, staging)
            if res.get("ok"):
                new_entry = {"path": name, "bytes": res["bytes"], "sha256": res["sha256"], "route": "misplaced-chunked"}
            else:
                _stamp_pull_error(rec, name, f"misplaced pull of {name} failed at {now()}: chunked stage {res.get('stage', 'unknown stage')}")
                staging.unlink(missing_ok=True)
                continue
        else:
            try:
                # Addressed at the session root, as the out-dir
                # pull addresses its files (/content/<name>):
                # the probed path is where the probe found the
                # file, the download speaks session paths.
                colab("download", "-s", session, f"/content/{name}", str(staging), timeout=600)
            except Exception as exc:  # noqa: BLE001 — a failed pull is a noted absence, never a landed file
                _stamp_pull_error(rec, name, f"misplaced pull of {name} failed at {now()}: {exc}")
                staging.unlink(missing_ok=True)
                continue
            if staging.exists():
                landed = staging.stat().st_size
                if landed != entry["bytes"]:
                    _stamp_pull_error(rec, name, f"misplaced pull of {name} landed {landed} bytes against a probed size of {entry['bytes']} at {now()}")
                    staging.unlink(missing_ok=True)
                    continue
                new_entry = {"path": name, "bytes": landed, "sha256": hashlib.sha256(staging.read_bytes()).hexdigest(), "route": "misplaced"}
        if new_entry is None:
            continue
        if dest.exists() and 0 < dest.stat().st_size > new_entry["bytes"]:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rotated = dest.with_name(f"{name}.rotated-{stamp}")
            suffix = 2
            while rotated.exists():
                rotated = dest.with_name(f"{name}.rotated-{stamp}-{suffix}")
                suffix += 1
            dest.rename(rotated)
            new_entry["rotated"] = {"to": rotated.name, "bytes": rotated.stat().st_size}
            rec["last_pull_rotation"] = f"{name} rotated to {rotated.name} at {now()}"
        staging.replace(dest)
        entry["collected"] = True
        collected.append(new_entry)
    if collected:
        by_path = {e["path"]: e for e in rec.get("pulled", [])}
        for e in collected:
            by_path[e["path"]] = e
        rec["pulled"] = list(by_path.values())
    return collected


def finalize_run(rec: dict, job: dict, session: str, remote_out: str, reader_state: dict, usage_before, hold: bool = True) -> tuple[int, dict]:
    """The terminal tail, shared by the foreground poll loop and
    --poll: final pull, the payload's own exit code, the terminal
    record + its one outbox event. Returns (exit_code, result).

    The payload's checkpoint exit_code is the truth about the
    outcome (2026-11-05: a job-zero build whose init_venv.py
    failed recorded "complete" with a clean-looking pull; only a
    human reading the pulled report saw assertions_passed:
    false). Jobs whose checkpoints carry no exit_code fall back
    to the launch wrapper's exit stamp — the same truth for
    payloads that never write a checkpoint. Outputs are pulled
    regardless — reports and
    verdicts land locally either way."""
    # Per-pass failure evidence for the collection partition
    # below: only this pass's pull attempts may hold the run.
    rec["pull_failures"] = {}
    pulled = pull_outputs(rec, job, session, remote_out, reader_state)
    # Declared-vs-pulled accounting: a payload can exit 0 with
    # declared artifacts unwritten (or written outside its out
    # dir), and until this was computed the run read "complete"
    # with a short pull and no signal anywhere — the absence
    # was discovered by hand, downstream (2026-11-11: heartbeat
    # and checkpoint arrive at cadence, gate artifacts never
    # do). The payload's exit code stays the outcome truth;
    # the shortfall is recorded and returned, not fatal.
    missing = [name for name in job.get("outputs", []) if name not in {entry["path"] for entry in pulled}]
    # Misplaced naming + guarded collection: a missing output
    # that sits at the payload's cwd root is named with its
    # path and size beside the shortfall — the usual cause is
    # a payload writing cwd-relative paths instead of its out
    # dir. A FRESH one (written during this run, judged
    # against the run's own started stamp) is also collected
    # through the pull: five consumer runs on 2026-11-12 lost
    # or hand-recovered verdicts to cwd-root writes, one
    # outright. A STALE one — an earlier run's leftover — is
    # named only, never delivered as this run's output.
    started_epoch = None
    try:
        started_epoch = datetime.fromisoformat(str(rec.get("started_at"))).timestamp()
    except (TypeError, ValueError):
        started_epoch = None
    misplaced = misplaced_outputs(session, missing, started_epoch) if missing else []
    if misplaced:
        flag_contended_misplaced(rec, session, misplaced)
        collected = collect_misplaced(rec, session, misplaced)
        if collected:
            pulled = rec.get("pulled", [])
            missing = [name for name in missing if name not in {e["path"] for e in collected}]
        rec["misplaced_outputs"] = misplaced
    if missing:
        rec["missing_outputs"] = missing
    # Empty-output accounting (the reversed polarity of the
    # missing case): an output can be present in the pull and
    # still be wrong — 0 bytes where the reader sizes the remote
    # file above 0. With the pull-side size cross-check this is
    # belt-and-braces, but the terminal record names it rather
    # than reading clean (2026-11-11: a completed job's heartbeat
    # and checkpoint pulled as 0-byte entries).
    final_stats = (reader_state or {}).get("outputs") or {}
    empty = [
        entry["path"]
        for entry in pulled
        if entry["bytes"] == 0 and (final_stats.get(entry["path"]) or {}).get("size", 0) > 0
    ]
    if empty:
        rec["empty_outputs"] = empty
    # Collection hold: partition the shortfall. An output that
    # probes PRESENT remotely — a fresh misplaced entry, a
    # reader-sized out-dir file, a held 0-byte pull against a
    # non-zero remote — and whose collection failed on this
    # pass is not an absence: the payload wrote it and the
    # transfer failed, and a terminal close would freeze that
    # failure as the verdict (2026-11-14 specimen: a run closed
    # complete 8 s after its misplaced pull landed 0 bytes, all
    # three outputs probed fresh at /content/ and uncollected,
    # and a later --poll echoed complete because a terminal
    # record never retries). Hold the record non-terminal; the
    # next poll retries the pull, and the hold dissolves on its
    # own when the files collect or stop probing present.
    # Contended misplaced entries never hold: that file is
    # another run's to disambiguate, by the attribution rule.
    failures = rec.get("pull_failures") or {}
    present_uncollected = set(empty)
    for entry in misplaced:
        if entry.get("fresh") and not entry.get("contended") and not entry.get("collected") and entry["name"] in failures:
            present_uncollected.add(entry["name"])
    for name in missing:
        if (final_stats.get(name) or {}).get("size", 0) > 0 and name in failures:
            present_uncollected.add(name)
    present_uncollected &= set(missing) | set(empty)
    if present_uncollected and hold:
        rec["collection_pending"] = sorted(present_uncollected)
        rec.setdefault("collection_since", now())
        save_record(rec)
        result = {"run_id": rec["run_id"], "state": "collecting", "collection_pending": sorted(present_uncollected), "pulled": pulled}
        if missing:
            result["missing_outputs"] = missing
        if misplaced:
            result["misplaced_outputs"] = misplaced
        return 3, result
    if present_uncollected:
        # The caller declined the hold (the foreground loop's
        # bound expired): the close proceeds, but the record
        # names the collection failure as its own fact, apart
        # from the payload's outcome.
        rec["collection_failed"] = sorted(present_uncollected)
    rec.pop("collection_pending", None)
    rec.pop("collection_since", None)
    exit_code = (reader_state.get("checkpoint") or {}).get("exit_code")
    if exit_code is None:
        exit_code = reader_state.get("exit_stamp")
    try:
        usage_after = colab("usage", timeout=120).stdout.strip()
    except subprocess.TimeoutExpired:
        # Accounting context only: a usage read that times out
        # must never strand a run at its finalize step.
        usage_after = ""
    if exit_code not in (None, 0):
        mark_terminal(rec, "failed", finished_at=now(), error=f"job payload exited {exit_code}", pulled=pulled, usage_before=usage_before, usage_after=usage_after)
        result = {"run_id": rec["run_id"], "state": "failed", "error": rec["error"], "pulled": pulled}
    else:
        mark_terminal(rec, "complete", finished_at=now(), pulled=pulled, usage_before=usage_before, usage_after=usage_after)
        result = {"run_id": rec["run_id"], "state": "complete", "pulled": pulled}
    if missing:
        result["missing_outputs"] = missing
    if misplaced:
        result["misplaced_outputs"] = misplaced
    if empty:
        result["empty_outputs"] = empty
    if rec.get("collection_failed"):
        result["collection_failed"] = rec["collection_failed"]
    return (1 if result["state"] == "failed" else 0), result
