# Lane consol-d — status (base: main @ 2b7ca4c)

Scope: snippets/ + probes/ only (Steps 10–12 of the consolidation program).
Coordinators build in this worktree only; no commits — the parent gates and lands.

- [x] Step 10 — snippets/job_harness.py extracted; three workload payloads repointed
  - job_harness.py (new, 70 lines): job_parser (--out), beat, write_checkpoint, resolve_remote_repo (verbatim from the battery/gathering duplicate).
  - facade_battery_job.py 98 → 67 lines; fixture_gathering_job.py 125 → 95; facade_memory_profiler.py 474 → 472 (only --out/beat/checkpoint moved; its own env-driven repo resolver is a different function and stays).
  - Payloads bootstrap the sibling import with sys.path.insert(0, <payload dir>) — the broker_probe pattern — so the import resolves locally (snippets/) and on the VM (/content) alike.
  - Pre-checks: py_compile OK on all four files; harness surface exercised locally (parser --out enforcement, beat line format, checkpoint bytes identical, resolve_remote_repo verdicts incl. both SystemExit messages); both payloads load by SourceFileLoader with no snippets/ on sys.path and share one job_harness module.
  - OUT-OF-SCOPE follow-up for the parent: the three job manifests gain "uploads": ["snippets/job_harness.py"] so the module lands beside the payload in /content (details in the lane's final report).
- [x] Step 11 — kill-matrix helpers folded into snippets/kill_matrix_kit.py; spent probes archived under snippets/probes/
  - Decision (implementer's call, per the plan's either/or): a single snippets/kill_matrix_kit.py (89 lines) with alive/reset/launch/state modes — NOT modes on heartbeat_job.py, whose argv is the job contract and which is uploaded alone by name; the four helpers share the drivers' exec -f shape instead. Mode dispatch: $KM_KIT_MODE under colab exec -f (sys.argv there carries the CLI's flags; --env is the repo's measured pattern, cf. p3_execution's SEC_USER_AGENT), argv[1] when run as a plain process; state mode's job dir is argv[2] or $KM_KIT_JOB_DIR. All four helper bodies are verbatim.
  - Drivers repointed: p3_kill_matrix.py (reset, launch) and p3_oom_lowtier.py (alive ×2) now exec the kit with --env KM_KIT_MODE=<mode>.
  - Deleted: alive_check.py (1 line), reset_dir.py (7), launch_detached.py (12), read_job_state.py (21). read_job_state had no tracked consumer; its function survives as the kit's state mode.
  - bridge_probe.py (143) and broker_probe.py (164) moved to snippets/probes/ (git mv, renames staged); broker_probe's _remote_guard sibling import now inserts parent.parent. No other tracked references to either probe exist.
  - Pre-checks: py_compile OK; alive mode prints the exact "session-reusable" marker via both dispatch routes; state mode's JSON matches read_job_state.py's shape on fabricated and absent dirs, via argv and env routes; missing/bogus mode and dir-less state refuse with rc 2. reset/launch bodies are verbatim (not run locally — they act on /content/km-job by design).
- [x] Step 12 — founding probes moved to probes/founding/; _driver import kept working
  - All seven drivers moved with git mv: p0_local_network (84), p1_session (53), p2_transfer (101), p3_execution (64), p3_kill_matrix (143), p3_oom_lowtier (127), p4_restart (93) — line counts unchanged.
  - _driver.py stays at probes/ (per the brief; its funnel disposition belongs to Lane B5). The six importing drivers now insert Path(__file__).parent.parent into sys.path; p0 (no _driver import) computes REPO with one more .parent. Verified by importing every driver from the new location: _driver resolves and REPO == repo root for all seven, so results still land in probes/results/ and every REPO-relative snippet path is unchanged.
  - Two in-scope path strings updated to the new locations: probes/_driver.py's require-login hint now prints probes/founding/<probe>.py; snippets/_remote_guard.py's refusal message names probes/founding/p3_oom_lowtier.py.
  - probes/ now holds only _driver.py and founding/.

Final census (worktree, tracked files): zero references to the four folded helper paths; zero references to the flat founding-probe or spent-probe paths; changed paths confined to snippets/ + probes/; new files carry no home/workspace literals. Full py_compile pass over every touched file.
