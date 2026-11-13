#!/usr/bin/env python3
"""Dispatch and the launch path: verb routing (--sweep / --poll
/ --wait never pay the launch path's costs), flag and claim
resolution, the bundle seed, lease acquire, uploads, the
confirmed detached launch, and the foreground poll loop.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .foundation import REPO, RUNS, TOKEN, colab, git_identity, lease, mirror, now, repo_registry, transfer
from .launch import orphan_notes, upload_one
from .pull import finalize_run, output_name_overlaps, pull_outputs
from .reader import _absence_grace_s, _launch_age_s, read_reader, register_poll_failure, write_reader
from .records import load_run_record, mark_terminal, save_record, stamp_collector
from .sweep import orphan_sweep
from .verbs import plant_git_identity, poll_run, wait_run


def main() -> int:
    args = sys.argv[1:]
    # Verbs that never launch dispatch first: --sweep reconciles
    # records, --poll reconciles one launched run, --wait reads
    # the local record only. None of them may pay the launch
    # path's costs (mirror restore, catalog, acquire).
    core_args = args[: args.index("--")] if "--" in args else list(args)
    if "--sweep" in core_args:
        findings, live_ok = orphan_sweep()
        print(json.dumps({"findings": findings, "live_list_unreadable": not live_ok}, indent=2))
        return 0
    if "--poll" in core_args:
        idx = core_args.index("--poll")
        if idx + 1 >= len(core_args):
            print("usage: colab-run --poll RUN_ID", file=sys.stderr)
            return 2
        return poll_run(core_args[idx + 1])
    if "--wait" in core_args:
        idx = core_args.index("--wait")
        if idx + 1 >= len(core_args):
            print("usage: colab-run --wait RUN_ID [--max SECONDS]", file=sys.stderr)
            return 2
        max_seconds = 120
        if "--max" in core_args:
            midx = core_args.index("--max")
            if midx + 1 >= len(core_args):
                print("usage: colab-run --wait RUN_ID [--max SECONDS]", file=sys.stderr)
                return 2
            try:
                max_seconds = int(core_args[midx + 1])
            except ValueError:
                print("colab-run --wait: --max must be an integer number of seconds", file=sys.stderr)
                return 2
        return wait_run(core_args[idx + 1], max_seconds)
    if "plant-git-identity" in core_args:
        def _flag(name: str) -> "str | None":
            return core_args[core_args.index(name) + 1] if name in core_args and core_args.index(name) + 1 < len(core_args) else None

        _session, _repo, _tree = _flag("--session"), _flag("--repo"), _flag("--tree")
        if not (_session and _repo and _tree):
            print("usage: colab-run plant-git-identity --session NAME --repo ID_OR_PATH --tree SESSION_TREE [--caller TAG]", file=sys.stderr)
            return 2
        return plant_git_identity(_session, _repo, _tree, _flag("--caller") or os.environ.get("COLAB_CALLER"))
    # Flags travel in pairs and may sit on either side of the job
    # name — extract them first and take the first remaining token
    # as the job. (Job-first-only parsing read a leading --caller
    # as a job name; it bit a consumer following shorthand in a
    # message, 2026-11-05.)
    resume = caller_flag = session_flag = None
    seed_flag = detach_flag = False
    extra_args = []
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--":
            # Everything after -- is the job's own argv (the repo
            # selection seam: env-build -- --repo <id> reaches
            # env_build.sh without a manifest per repo).
            extra_args = args[i + 1:]
            break
        if args[i] == "--seed":
            seed_flag = True
            i += 1
        elif args[i] == "--detach":
            detach_flag = True
            i += 1
        elif args[i] == "--resume" and i + 1 < len(args):
            resume = args[i + 1]
            i += 2
        elif args[i] == "--caller" and i + 1 < len(args):
            caller_flag = args[i + 1]
            i += 2
        elif args[i] == "--session" and i + 1 < len(args):
            session_flag = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1
    if not positional:
        print("usage: colab-run <job-name> [--resume RUN_ID] [--caller TAG] [--seed] [--detach] [--session NAME] [-- <job args>...]", file=sys.stderr)
        return 2
    job_name = positional[0]
    manifest_path = REPO / "jobs" / job_name / "job.json"
    if not manifest_path.exists():
        print(f"no job manifest at {manifest_path}", file=sys.stderr)
        return 2
    job = json.loads(manifest_path.read_text())
    # Caller claim: flag over the manifest's caller key over
    # $COLAB_CALLER; a presented but unregistered claim is refused
    # at claim time, before anything starts.
    claim_raw = caller_flag if caller_flag is not None else job.get("caller")
    claim_err = lease.caller_claim_error(REPO, flag=claim_raw, env=os.environ.get("COLAB_CALLER"))
    if claim_err:
        print(f"colab-run: {claim_err}", file=sys.stderr)
        return 2
    caller = lease.resolve_caller(REPO, flag=claim_raw, env=os.environ.get("COLAB_CALLER"))
    # Session pin: flag over the env seam, mirroring the caller
    # claim's precedence. A pin names the lease this run must
    # adopt (acquire refuses pin-unavailable rather than
    # substituting another session).
    pin_session = session_flag if session_flag is not None else os.environ.get("COLAB_RUN_SESSION")
    if not TOKEN.exists() and not os.environ.get("COLAB_BIN"):
        print("colab-run: STAGED — no colab login yet. Run ops/bootstrap_local.sh, then the one-time login (see README).", file=sys.stderr)
        return 2

    mirror("restore")

    # A manifest may name its shape; otherwise the config's
    # default_shape is the only fallback — no shape is hardcoded
    # here, and an unresolvable shape fails loudly.
    shape_id = job.get("shape") or lease.default_shape(REPO)
    shapes = lease.catalog_shapes(REPO, colab)
    if not shape_id or shape_id not in shapes:
        print(f"colab-run: job {job_name} names no usable shape (manifest shape or config default_shape required)", file=sys.stderr)
        return 2
    shape = shapes[shape_id]
    run_id = resume or f"{job_name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    rec = None
    if resume:
        # A resume continues the run on disk: its launch facts
        # (started_at above all) are the run's history and are
        # never re-stamped — the resume adds its own facts.
        # (2026-11-06: a resumed hold came back stamped with the
        # resume moment, erasing its launch time.)
        prior = RUNS / f"{run_id}.json"
        if prior.exists():
            rec = load_run_record(run_id)
            if rec is None:
                # A torn prior (a flap-killed fire's 0-byte
                # record) holds nothing to preserve: the resume
                # starts the run fresh rather than crashing
                # every later entry on the damaged file.
                print(f"colab-run: prior record for {run_id} is unreadable (torn); starting fresh", file=sys.stderr)
            else:
                # A resurrected run announces its NEW terminal state
                # afresh: the sweep's orphan event is history, not
                # this run's outcome.
                rec.pop("terminal_event_state", None)
                rec.pop("terminal_event_at", None)
                # A run that already launched keeps its launched
                # state across a resume: re-stamping "starting"
                # leaves confirmed-launch facts under a
                # pre-launch state when the resume dies before
                # its own launch (2026-11-11: an admincap record
                # froze at "starting" carrying launch_at and
                # launch_confirmed_at). The resume's own launch
                # re-stamps "running" when it lands; a record
                # that never launched re-stamps "starting" as
                # before.
                if rec.get("state") in ("polling", "running"):
                    rec.update(resumed=True, resumed_at=now())
                else:
                    rec.update(state="starting", resumed=True, resumed_at=now())
    if rec is None:
        rec = {"run_id": run_id, "job": job_name, "shape": shape_id, "state": "starting", "started_at": now(), "resumed": bool(resume)}
    if caller:
        rec["caller"] = caller
    save_record(rec)
    # Bundle seed (--seed — env-build's private-repo route): the
    # job's target repo reaches the VM as a snapshot bundle, never
    # as a VM-side clone. A private repo's plain HTTPS clone
    # cannot authenticate on the VM (no credential lives there by
    # design), so before launch the runner builds the bundle from
    # the registry entry's local checkout (the same builder
    # colab-check ships), uploads it, and seeds the dest from it
    # (sync/apply_bundle_remote.py's seed mode); the job then runs
    # with --skip-clone. Built here, before any session is held,
    # so a local build failure holds nothing.
    seed_ctx = None
    if seed_flag and not resume:
        def _extra(flag):
            return extra_args[extra_args.index(flag) + 1] if flag in extra_args and extra_args.index(flag) + 1 < len(extra_args) else None
        try:
            registry = repo_registry.load_local_registry(REPO)
            seed_repo_id, seed_entry = repo_registry.get_entry(registry, _extra("--repo"))
            seed_dest = repo_registry.variant_dest(seed_entry, _extra("--variant"))
        except ValueError as exc:
            rec.update(state="failed", error=f"bundle seed: {exc}")
            save_record(rec)
            print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
            return 1
        bundle_env = dict(os.environ)
        bundle_env["COLAB_REPO_SRC"] = str(Path(os.path.expanduser(seed_entry["local_path"])))
        bundle = subprocess.run(["bash", str(REPO / "sync" / "make_snapshot_bundle.sh")], capture_output=True, text=True, env=bundle_env)
        if bundle.returncode != 0:
            rec.update(state="failed", error=f"bundle seed: snapshot build failed: {bundle.stderr[-300:]}")
            save_record(rec)
            print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
            return 1
        seed_manifest = json.loads(bundle.stdout)
        # The git identity plant: the seeded tree carries the
        # source repo's own configured identity (its repo-local
        # user.name/user.email), never the forming shell's
        # inheritance. No complete pair at the source → no
        # plant, and the record says so.
        seed_identity = git_identity.repo_identity(seed_entry["local_path"])
        seed_ctx = {"repo": seed_repo_id, "dest": seed_dest, "clone_url": seed_entry["clone_url"], "bundle_path": Path(seed_manifest["bundle"]), "manifest": seed_manifest, "git_identity": seed_identity}
        rec["bundle_seed"] = {"repo": seed_repo_id, "dest": seed_dest, "sha256": seed_manifest["sha256"], "bytes": seed_manifest["bytes"], "snapshot_commit": seed_manifest["snapshot_commit"], "tree": seed_manifest["tree"], "git_identity": seed_identity}
        save_record(rec)
    try:
        usage_before = colab("usage", timeout=120).stdout.strip()
    except subprocess.TimeoutExpired:
        # Accounting context only — never a dead launcher.
        usage_before = ""
    # Lease model (sync/lease/): adopt-or-acquire the shared session and
    # leave it up at the end; the reaper owns its lifecycle. With the lease
    # disabled in config, fall back to the original per-job session.
    use_lease = bool(lease.load_config(REPO).get("enabled"))
    if detach_flag and not use_lease:
        # --detach leaves the job running with no poller; the
        # lease-disabled fallback's per-run session is stopped by
        # the poller's own finally, so a detached launch there
        # would kill the job it just started.
        rec.update(state="failed", error="--detach requires the lease path (lease.enabled); the fallback per-job session dies with the poller")
        save_record(rec)
        print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
        return 2
    if pin_session and not use_lease:
        # A pin names a lease; the fallback's per-run session is
        # run-named, so there is nothing to pin to.
        rec.update(state="failed", error="--session requires the lease path (lease.enabled); the fallback per-job session is run-named")
        save_record(rec)
        print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
        return 2
    session = run_id  # the lease-disabled fallback's per-run session
    if use_lease:
        if pin_session:
            rec["session_pin"] = pin_session
        got = lease.acquire(colab, REPO, shape_id, via="runner", caller=caller, prefer_session=pin_session)
        if got["action"] in ("refused-budget", "create-failed", "shape-refused", "shape-mismatch", "caller-start-refused", "unknown-shape", "pin-unavailable"):
            rec.update(state="failed", error=json.dumps(got)[:300])
            save_record(rec)
            # E1: if live sessions sit unrecorded at this shape,
            # the refusal may be the record loss talking — name
            # the state and the verb that restores it (stderr;
            # stdout stays the run's JSON result).
            for note in orphan_notes(shape_id, shape):
                print(note, file=sys.stderr)
            print(json.dumps({"run_id": run_id, "state": "failed", "lease": got}, indent=2))
            return 1
        rec["lease_action"] = got["action"]
        if got.get("health_degraded"):
            # Adopted anyway (every candidate was degraded):
            # the record says the launch channel has been
            # failing on this session, so a launch failure here
            # reads as a known condition, not a surprise.
            rec["health_degraded"] = True
        # The lease's session is whatever the acquire created or
        # adopted — a generated name, never a configured one.
        session = got["record"]["session"]
        rec["session"] = session
        lease.touch_job(REPO, run_id)
        save_record(rec)
    if not use_lease:
        rec["session"] = session
        save_record(rec)
        # Session shape gate for the fallback's own `new` below (the
        # lease path is gated inside acquire): a job run is an agent
        # actor unless the env claim says otherwise. A refusal fails
        # the run before any session is created.
        refusal = lease.enforce_session_admission(REPO, shape_id, via="runner", shape=shape, seam="runner", caller=caller)
        if refusal is not None:
            rec.update(state="failed", error=json.dumps(refusal)[:300])
            save_record(rec)
            print(json.dumps({"run_id": run_id, "state": "failed", "lease": refusal}, indent=2))
            return 1
    detached = False
    try:
        if not use_lease:
            new_args = ["new", "-s", session] + lease._session_shape_args(shape)
            r = colab(*new_args, timeout=600)
            if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
                rec.update(state="failed", error=(r.stderr or r.stdout).strip()[-300:])
                save_record(rec)
                return 1
        script = REPO / job["script"]
        # The remote script slot is scoped to this run: the bare
        # basename is one shared slot per session, and two jobs
        # whose scripts share a basename (every lane's run.py)
        # overwrote each other between upload and launch — a run
        # then executed a foreign payload with its own args
        # (2026-11-08 remainder lanes: an eraattr payload ran
        # under a rem20d launch). The payload never names its own
        # slot (the runner invokes it), so scoping is safe;
        # manifest uploads keep their contract names by contrast.
        remote_script = f"{run_id}-{script.name}"
        # Launch-phase stamps (2026-11-12): the window from the
        # record's creation to the state=running save below is
        # minutes of CLI round trips on a loaded session
        # (usage, uploads with verify probes, the seed), and a
        # launcher killed inside it — a caller's client timeout
        # is the recurring case — stranded its record at
        # "starting" with no trace of how far it got, so a slow
        # launch and a dead one read identically. The phase is
        # stamped as the launch advances; a stranded record
        # names its phase.
        rec["launch_phase"] = "uploading"
        save_record(rec)
        err = upload_one(session, script, remote_script, run_id)
        if err is not None:
            rec.update(state="failed", error=f"runner script upload: {err}")
            save_record(rec)
            print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
            return 1
        # Manifest-declared extra uploads land at /content/<name>
        # before launch: a shell_job payload names its script in
        # args (--script /content/env_build.sh) and the registry it
        # reads (/content/repos.json), and neither is the runner
        # script itself. Until this loop existed nothing uploaded
        # them, so env-build's shell_job refused in its first
        # second (2026-11-05) — a declared upload missing locally
        # fails the run here, never launches a job that cannot run.
        for extra in job.get("uploads", []):
            src = REPO / extra
            if not src.is_file():
                rec.update(state="failed", error=f"manifest upload missing locally: {extra}")
                save_record(rec)
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
            err = upload_one(session, src, src.name, run_id)
            if err is not None:
                rec.update(state="failed", error=f"manifest upload failed: {extra}: {err}")
                save_record(rec)
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
        seeded = False
        if seed_ctx is not None:
            # Move the bundle (chunked — it can exceed the
            # single-file ceiling), its manifest, and the apply
            # script, then seed the dest on the VM and verify the
            # manifest's tree in the seed's own output before any
            # launch: a seed that did not land is a failed run,
            # never a job launched against a missing tree.
            up = transfer.chunked_upload(session, seed_ctx["bundle_path"], seed_ctx["bundle_path"].name)
            if not up["ok"]:
                rec.update(state="failed", error=f"bundle seed: chunked upload failed: {json.dumps(up)[:300]}")
                save_record(rec)
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
            err = upload_one(session, Path(str(seed_ctx["bundle_path"]) + ".manifest.json"), seed_ctx["bundle_path"].name + ".manifest.json", run_id)
            if err is None:
                err = upload_one(session, REPO / "sync" / "apply_bundle_remote.py", "apply_bundle_remote.py", run_id)
            if err is not None:
                rec.update(state="failed", error=f"bundle seed: {err}")
                save_record(rec)
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
            _ident = seed_ctx.get("git_identity")
            _ident_args = (
                f",'--user-name',{_ident['name']!r},'--user-email',{_ident['email']!r}"
                if _ident
                else ""
            )
            seed_remote = REPO / "var" / f"seed-{run_id}.py"
            seed_remote.write_text(
                "import subprocess\n"
                f"r = subprocess.run(['python3','apply_bundle_remote.py','{seed_ctx['bundle_path'].name}','{seed_ctx['dest']}','--origin-url','{seed_ctx['clone_url']}'{_ident_args}], capture_output=True, text=True)\n"
                "print(r.stdout); print(r.stderr[-500:])\n"
            )
            try:
                sr = colab("exec", "-s", session, "--timeout", "1800", "-f", str(seed_remote), timeout=1900)
                seed_out = sr.stdout
            except subprocess.TimeoutExpired:
                # A seed timeout is a failed seed (the verify
                # below cannot pass on no output), never a dead
                # launcher stranding the record at "starting".
                seed_out = ""
                lease.append_launch_outcome(REPO, session, "seed", False, "seed exec timed out")
            if '"applied": true' not in seed_out or seed_ctx["manifest"]["tree"] not in seed_out:
                rec.update(state="failed", error=f"bundle seed: seed did not verify: {seed_out.strip()[-300:]}")
                save_record(rec)
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
            lease.append_launch_outcome(REPO, session, "seed", True, seed_ctx["dest"])
            # The plant's readback verdict rides the apply's
            # JSON; record it beside the intended pair so the
            # record distinguishes planted from merely meant.
            for _line in reversed(seed_out.splitlines()):
                try:
                    _apply = json.loads(_line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(_apply, dict) and "applied" in _apply:
                    rec["bundle_seed"]["git_identity_result"] = _apply.get("git_identity")
                    save_record(rec)
                    break
            seeded = True
        # Relative to the session working dir (/content on a real Colab VM;
        # the fake CLI's sandbox root under test) so the same code runs both.
        # Per-run: a fixed dir leaks the previous run's heartbeat tail,
        # checkpoint, and outputs into this run's completion check.
        remote_out = f"{job_name}-out-{run_id}"
        rec.update(state="running", remote_out=remote_out, extra_args=extra_args, launch_phase="launching")
        save_record(rec)
        # Detached launch (default pending the kill-matrix verdict): a short
        # exec starts the job under nohup; the CLI is never the job's lifeline.
        # The payload runs under a stamp wrapper: its exit code lands at
        # <remote_out>.exit-code, so a payload that never writes a
        # checkpoint still reports its outcome truthfully (2026-11-08:
        # checkpoint-less gate jobs recorded failed on green runs).
        job_args = "".join(f",'{a}'" for a in list(job.get("args", [])) + (["--skip-clone"] if seeded else []) + extra_args)
        stamp = (
            "import subprocess, sys\n"
            f"rc = subprocess.run([sys.executable, '{remote_script}', '--out', '{remote_out}'{job_args}]).returncode\n"
            f"open('{remote_out}.exit-code', 'w').write(str(rc))\n"
        )
        launch = (
            f"import subprocess; subprocess.Popen(['nohup','python3','-c',{stamp!r}],"
            f" stdout=open('{remote_out}.log','w'), stderr=subprocess.STDOUT, start_new_session=True)"
        )
        launcher = REPO / "var" / f"launch-{run_id}.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(launch + "\nprint('launched')\n")
        rec["launch_at"] = now()
        save_record(rec)
        # The launch exec's outcome is CHECKED, and the launch is
        # then confirmed against the venue before the record may
        # claim polling. Until this existed the exec's result was
        # ignored entirely — and its timeout re-raised through
        # the launcher — so a launch that never landed (the
        # daemon's exec lost under kernel saturation) left a
        # record claiming a run that did not exist, forever
        # (2026-11-11 consol records). The stamp wrapper opens
        # the job's log at spawn, so a landed launch has a
        # footprint (pids, log, checkpoint, or exit stamp) on
        # the first reader pass; total absence is a lost launch.
        launch_error = None
        launch_timeout = int(os.environ.get("COLAB_RUN_LAUNCH_TIMEOUT_S", "300"))
        try:
            lr = colab("exec", "-s", session, "--timeout", "120", "-f", str(launcher), timeout=launch_timeout)
            if lr.returncode != 0 or "launched" not in lr.stdout:
                launch_error = f"launch exec rc {lr.returncode}: {(lr.stderr or lr.stdout).strip()[-200:]}"
        except subprocess.TimeoutExpired:
            launch_error = "launch exec timed out"
        lease.append_launch_outcome(REPO, session, "launch", launch_error is None, launch_error or run_id)
        reader = write_reader(run_id, remote_out, job.get("outputs", []))
        # The confirmation is a reader pass and shares the poll
        # timeout's discipline (a test that shrinks poll timeouts
        # shrinks this one too), capped at 120 s so a wedged
        # channel cannot hold the launcher for a full poll bound.
        confirm_timeout = int(os.environ.get("COLAB_RUN_CONFIRM_TIMEOUT_S") or min(int(os.environ.get("COLAB_RUN_POLL_TIMEOUT_S", "300")), 120))
        cstate, _confirm_error = read_reader(session, reader, confirm_timeout)
        footprint = bool(cstate) and bool(cstate.get("job_pids") or cstate.get("exit_stamp") is not None or cstate.get("job_log_tail") or cstate.get("checkpoint"))
        if footprint:
            rec["state"] = "polling"
            rec["launch_phase"] = "launched"
            rec["launch_confirmed_at"] = now()
            rec["last_evidence_at"] = rec["launch_confirmed_at"]
            stamp_collector(rec, rec.get("caller"))
            # A confirmed launch is this attempt's truth: a
            # prior attempt's launch_lost / error (a resume
            # carries both on the record) die in this same
            # save — a record must never read confirmed and
            # lost at once (2026-11-11 record-truth item).
            rec.pop("launch_lost", None)
            rec.pop("error", None)
            # The machine that actually runs the job, from
            # the confirmation reader's own probe (cgroup-
            # aware): sizing reads the measured shape, not
            # the lease's declared one.
            machine = cstate.get("machine")
            if isinstance(machine, dict):
                rec["measured_shape"] = {**machine, "at": rec["launch_confirmed_at"]}
            save_record(rec)
        elif cstate is not None:
            # The channel answered and there is no footprint:
            # the launch is lost, and the record closes honestly
            # now instead of claiming a run that does not exist.
            mark_terminal(rec, "failed", launch_lost=True, error=f"launch never landed on {session} ({launch_error or 'no job footprint on the confirmation read'}); relaunch with --resume {run_id} to retry")
            print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
            return 1
        elif launch_error is None:
            # The venue accepted the launch (the exec printed
            # 'launched') but the confirmation read produced no
            # evidence — the refusing channel is the unknown, and
            # an accepted launch reads polling as it always has,
            # stamped unconfirmed with the failed read noted as
            # poll evidence. Reconciles confirm it, or close it
            # past the absence grace if nothing ever lands.
            rec["state"] = "polling"
            rec["launch_unconfirmed_at"] = now()
            rec["last_poll_error"] = _confirm_error
            stamp_collector(rec, rec.get("caller"))
            save_record(rec)
        else:
            # The launch exec itself failed AND the confirmation
            # read is unreadable: nothing is known, and the
            # record stays in the pre-polling "running" state,
            # stamped unconfirmed — polls reconcile it if the
            # job exists, and the sweep's stale-running rule
            # closes it if nothing ever confirms.
            rec["launch_unconfirmed_at"] = now()
            rec["last_poll_error"] = _confirm_error
            stamp_collector(rec, rec.get("caller"))
            save_record(rec)
        def _name_warnings() -> list:
            # Output-name risk, named at launch while a rename
            # is still cheap: a declared basename a live run on
            # this session also declares is safe in the per-run
            # out dirs, but two payloads writing it cwd-relative
            # share the session root and interleave silently
            # (defect routed 2026-11-12). Advisory only — it
            # blocks nothing; finalize's contention guard is
            # the backstop. The computation is guarded at every
            # level: a warning that can raise is a gate.
            try:
                found = output_name_overlaps(session, run_id, job.get("outputs", []))
            except Exception:  # noqa: BLE001
                return []
            if found:
                rec["output_name_warnings"] = found
                save_record(rec)
                for warning in found:
                    print(f"warning: output name {warning['name']!r} is also declared by live run {warning['shared_with']} on {session} — if both payloads write it cwd-relative, the root copy is unattributable and finalize will flag it contended rather than collect it", file=sys.stderr)
            return found

        if detach_flag:
            # E5: launch exactly as the foreground form, write
            # the record, return. usage_before is stamped now —
            # the reconcile that finalizes this run reads it from
            # the record, not from a dead process's locals. The
            # finally below skips the close-out for a detached
            # launch: no finish_job (the registration is the
            # run's liveness evidence), no stop, no capture.
            detached = True
            rec["detached_launch"] = True
            rec["usage_before"] = usage_before
            save_record(rec)
            # The launch result prints BEFORE any advisory
            # pass runs (commission supplement, 2026-11-12:
            # the overlap crash's worst property was eating the
            # run_id — a lane that loses its launcher before the
            # print cannot even name the run it must adopt).
            # Warnings follow on the record and stderr only —
            # the launch object stays the sole stdout object,
            # exactly as before the check existed, so a consumer
            # parsing stdout whole is untouched.
            print(json.dumps({"run_id": run_id, "session": session, "state": rec["state"]}, indent=2), flush=True)
            _name_warnings()
            return 0
        _name_warnings()
        deadline = time.time() + job.get("timeout", 18000)
        cadence = job.get("checkpoint_pull_seconds", 300)
        # A checkpoint poll that outlives its subprocess timeout
        # is a failed poll, not a dead poller (2026-11-06: an
        # uncaught TimeoutExpired killed the poller mid-run and
        # stranded the record at polling while the payload
        # completed). The timeout is env-tunable for the same
        # reason the cadence is a manifest value: tests shrink it.
        poll_timeout = int(os.environ.get("COLAB_RUN_POLL_TIMEOUT_S", "300"))
        state = {}
        while time.time() < deadline:
            time.sleep(cadence)
            # A failed poll — subprocess timeout or unreadable
            # answer — is counted, not fatal (2026-11-06: an
            # uncaught TimeoutExpired killed the poller and
            # stranded the record at polling while the payload
            # completed); E11 bounds the count and classifies it.
            state, poll_error = read_reader(session, reader, poll_timeout)
            if state is None:
                if register_poll_failure(rec, session, poll_error, collector=rec.get("caller")) == "failed":
                    print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                    return 1
                continue
            done_units = (state.get("checkpoint") or {}).get("completed_units", 0)
            rec["last_checkpoint_units"] = done_units
            rec["last_evidence_at"] = now()
            stamp_collector(rec, rec.get("caller"))
            rec["poll_failures"] = 0
            save_record(rec)
            if job.get("outputs"):
                pull_outputs(rec, job, session, remote_out, state)
                save_record(rec)
            log = state.get("heartbeat_tail") or []
            if any("complete" in ln for ln in log) or (job.get("units") and done_units >= job["units"]) or state.get("exit_stamp") is not None:
                break
            if not state.get("job_pids") and not state.get("job_log_tail") and state.get("exit_stamp") is None and not state.get("checkpoint") and _launch_age_s(rec) > _absence_grace_s():
                # Total absence past the grace bound: no process,
                # no log, no checkpoint, no stamp — the launch
                # never landed (the confirmation read's one pass
                # can race a slow kernel; a grace of polls cannot).
                mark_terminal(rec, "failed", launch_lost=True, error=f"no job footprint on {session} {_absence_grace_s()}s after launch: the launch never landed; relaunch with --resume {run_id} to retry")
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
            if not state.get("job_pids") and state.get("job_log_tail"):
                # The launched process is gone and its launcher log
                # is all that exists: the job died before producing
                # a checkpoint — shell_job refusing a never-uploaded
                # --script file is the proven case (2026-11-05: its
                # one-line refusal sat in this log while the poller
                # polled an empty out dir through two timeouts,
                # ~40 min of apparent progress). A completed job
                # always trips the completion check above in the
                # same poll, so reaching here means death, not
                # completion: fail with the log tail in hand.
                tail = "\n".join(state["job_log_tail"])
                mark_terminal(rec, "failed", error=(f"job exited before completing; remote log tail: {tail}")[-500:])
                print(json.dumps({"run_id": run_id, "state": "failed", "error": rec["error"]}, indent=2))
                return 1
        stamp_collector(rec, rec.get("caller"))
        rc, result = finalize_run(rec, job, session, remote_out, state, usage_before)
        print(json.dumps(result, indent=2))
        return rc
    finally:
        # A detached launch returns with the job still running:
        # no finish_job (the registration is the run's liveness
        # evidence until a reconcile closes it out), no stop, no
        # capture — the reconcile owns the close-out.
        if not detached:
            if use_lease:
                lease.finish_job(REPO, run_id)
            else:
                colab("stop", "-s", session, timeout=120)
            mirror("capture")
