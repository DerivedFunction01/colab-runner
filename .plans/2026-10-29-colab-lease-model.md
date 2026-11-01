# Colab lease model — one shared session, treated as a VM

Status: ruled and in implementation (2026-10-29). Follow-on to the
colab-runner plan; redesigns Step 8's session lifecycle. Rulings recorded
at the bottom: defaults adopted with the idle amendment, unattended
acquire confirmed.

## Findings that motivate it

- Every new session mints a fresh tunnel hostname
  (`8080-<session-id>-b.<region>.prod.r.colab.dev`), and Muse network
  approvals are per exact host — so per-job sessions mean a fresh approval
  prompt per job. A domain-level approval has been requested from the Muse
  team (filed 2026-10-29, delivery confirmed) but does not exist today. One
  long-lived lease collapses approvals to at most one per lease.
- Session creation is cheap (~4 s) but per-job churn also repeats bundle
  uploads and environment setup; a warm shared VM amortises both.
- Kill matrix (measured 2026-10-29): a job launched detached (nohup, real
  process) survives local CLI death; a job in a kernel cell dies with the
  CLI. Jobs on a shared session are therefore always detached processes —
  the session is a VM that hosts processes, not a notebook that runs cells.
  The kernel is only a control channel (launch, poll, file moves).
- Burn accrues while a session is assigned, even idle (measured), so cost
  control is the lease's idle shutdown and hard expiry, not job count.
- `colab stop` hangs on a wedged kernel (measured); release must keep the
  direct-unassign fallback. Server-side assignment list is the truth about
  what is billing; local bindings can be empty while an assignment lives.

## The model

One lease at a time per profile. A lease is a record plus a live session:

- **Lease record** (`var/lease.json`, untracked): profile, session name,
  endpoint, acquired-at, hard deadline (acquired + `max_hours`), last-used
  timestamp, current job run-ids. Reconstructible: if the file is lost, the
  server assignment list plus the sessions binding rebuild it or prove it
  absent.
- **Acquire-or-adopt** on every wrapper entry (`colab-run`, `colab-check`,
  `colab-lease`): restore transient state → read lease record → validate
  the session with a short-timeout status/alive check → reuse it; if dead
  or past deadline, release and create fresh. No job ever creates a second
  session while a valid lease exists.
- **Jobs** run detached under `/content/jobs/<run-id>/`, exactly as the
  current runner launches them, but job end no longer stops the session —
  it stamps the lease's last-used time and pulls results as today.
- **Exclusive tenancy (v1 default):** one heavy job at a time; further
  submissions queue locally and start when the running job finishes.
  Light co-tenancy is a later refinement, not v1.
- **Reaper:** the existing 5-minute restore cron gains a lease duty —
  enforce the hard deadline always, and reconcile orphans. Idle shutdown
  is subordinate to the deadline under the idle amendment (below): the
  lease normally lives out its window, because the work that uses it is
  bursty and can arrive at any time.
- **Budget guard:** before acquiring, read `colab usage`; refuse (and
  surface) when the projected lease would cross the configured daily or
  monthly CU budget.

## Config (extends `config/profiles.json` or a sibling `config/lease.json`)

- `max_hours`: 8 (hard deadline; Pro's own ceiling is 12 h, so 8 leaves
  margin and matches a working day away).
- `idle_shutdown_minutes`: default **disabled** (idle amendment, ruled
  2026-10-29): XBRL/Workload demand is unpredictable, so an idle lease is a
  warm lease, not a finished one. The knob stays in config for profiles
  where a shutdown is wanted; the hard deadline is the cost bound.
- `profile`: proposed `workload` (high-RAM, 0.26 CU/h) as the lease default;
  the standard tier (0.08 CU/h) stays available for light leases.
- `daily_cu_budget` / `monthly_cu_budget`: proposed 10 / 100, matching the
  monthly refresh scale.
- `keepalive_minutes`: proposed 15, pending probe P-L1 — a trivial kernel
  exec on that cadence only if an untouched session is reaped early.

## Cost

High-RAM lease held the full 8 h: 2.08 CU. Standard: 0.64 CU. At a ~100 CU
monthly refresh, even a full high-RAM lease every day (~62 CU/month) fits;
with a 30-minute idle shutdown, real cost tracks actual work, not the cap.

## Scenarios

- **S1 — Away, scheduled work fires.** A cron-driven job calls
  `colab-run`; no lease exists → acquire (one approval prompt, pushed to
  the phone), run detached, pull results. Lease idles, reaper stops it
  after 30 idle minutes. Total prompts for the day: ≤1 per lease.
- **S2 — Interactive burst.** Several jobs submitted in an afternoon share
  the warm lease: no re-upload of the repo bundle, no repeated setup.
  Heavy jobs serialize through the local queue; each new submission sees
  "queued behind run X" in its record instead of minting a session.
- **S3 — Hard deadline hits mid-job.** At 8 h the reaper does not kill
  blindly: the running job checkpoints (jobs are chunked with resume —
  the workload-repo precedent), the lease is released, and the next wrapper
  entry acquires a fresh lease and resumes the same run-id. Work spanning
  the boundary loses at most the in-flight chunk.
- **S4 — This machine restarts mid-lease.** The 5-minute cron restores the
  token and sessions binding from the mirror; the next wrapper entry
  validates the lease against the server list and adopts it. If the
  binding is unrecoverable, it releases the orphan assignment server-side
  and acquires fresh — never leaves an unnamed assignment billing.
- **S5 — Colab kills the session** (runtime recycle, Google-side fault).
  The next poll or wrapper entry finds the alive check failing → lease
  marked dead → reacquire → running jobs resume from checkpoints. The
  failure is surfaced in the run record, not silent.
- **S6 — Job finishes, more work may come at any time.** Lease stays warm
  until its deadline (idle amendment): Workload/XBRL work is bursty and
  unscheduled, so "idle" never means "done" inside the window. The full
  window's cost (≤2.08 CU high-RAM) is the explicit price of warm starts,
  paid against the monthly refresh.
- **S7 — Manual control.** `colab-lease status|acquire|release` for direct
  control; release is immediate and uses the unassign fallback when stop
  hangs. The operator present or away makes no difference to the machinery.
- **S8 — Budget pressure.** Acquire refused when the projection crosses
  the configured budget; queued work waits and the refusal is surfaced
  with the numbers, not discovered from a zero balance.
- **S9 — Kernel wedges on the shared session.** Detached job processes are
  unaffected (they are not kernel cells). Control ops fail → treat as S5
  for the control channel: if the wedge persists past a short probe
  window, release via unassign and reacquire; jobs resume from
  checkpoints. A wedged kernel never takes a job down with it.
- **S10 — Two heavy jobs genuinely needed at once.** Out of scope for v1:
  they serialize. Recorded here so the limitation is a decision, not a
  surprise; a second concurrent lease is the named future extension.

## Probes owed before implementation

- **P-L1 — Idle reap.** ANSWERED in the negative by the first away lease:
  an untouched high-RAM session died at ~3 h inside its 8 h term (alive
  15:5x EDT, dead by 16:43, server-verified). The operator's ruling (2026-10-30):
  the idle timeout is the presumed killer and a leased session must
  never sit truly idle. Mechanism landed (commit `28805cc`):
  `snippets/keepalive.py`, a detached single-instance loop doing a CPU
  touch + heartbeat append every 60 s (config `keepalive` in
  lease.json), launched by acquire and relaunched by the reaper when
  its heartbeat goes stale. Result on lease #2: the loop beat 289/289
  cycles without a gap and the session still died at ~6 h 17 m — a
  detached OS process does not register as activity. Second channel
  added on the operator's ruling (2026-10-30, commit `42ffc52`): a kernel
  pulse — the reaper fires a trivial kernel exec every
  `pulse_seconds` (600), tracked on the lease record; a pulse timeout
  reads as busy. Verdict from lease #3 (2026-10-30): the session
  survived the full 8 h term and was released by the reaper at its
  deadline (21:05:30Z, stop confirmed, 478/478 keepalive cycles) —
  the first lease to reach term, against ≤3 h 06 m (idle, no loop)
  and ~6 h 17 m (loop only). The kernel pulse is the leading
  explanation for the difference; a single run does not exclude
  recycling variance, but the two-channel stack is the established
  default and P-L1 is closed on it.
- **P-L2 — Adoption.** Kill local state (binding + lease record) with a
  live assignment; test each adoption route in order: mirror restore →
  status validation; CLI rebind by endpoint if supported; else release +
  reacquire timing. Record which route actually works.
- **P-L3 — Deadline behaviour.** What the CLI reports as a session
  approaches Pro's 12 h ceiling, and whether any warning exists to key
  the 8 h handoff on (else the lease's own clock is the only guard — it
  is, by design, but confirm no earlier Google-side cutoff).

## Implementation steps

1. Lease record + `bin/colab-lease` (status/acquire/release) over the
   existing wrappers, with the unassign fallback on release.
2. `colab-run` / `colab-check`: replace create/stop-per-job with
   acquire-or-adopt + last-used stamping; per-job behaviour otherwise
   unchanged. Local heavy-job queue.
3. Reaper duty added to the 5-minute cron (deadline + idle enforcement,
   silent unless it acts or fails).
4. Budget guard at acquire, using `colab usage`.
5. Probes P-L1..P-L3 run and recorded; config defaults adjusted to their
   results before the lease becomes the default path.
6. Fake-CLI tests extended: adopt-existing, deadline handoff, idle
   shutdown, orphan-after-restart. Suite green, then live: one full
   away-day cycle (acquire → two jobs share → idle shutdown) observed.

## Organisation (ruled alongside: modular, easy to maintain)

- All lease logic lives in one module, `sync/lease.py`, written against an
  injected `colab` callable so the fake CLI tests it without a network.
  `bin/colab-lease` is a thin CLI over it (status / acquire / release /
  reap); `bin/colab-run` and `bin/colab-check` call the module instead of
  owning session lifecycle code.
- One config seam: `config/lease.json` (defaults shipped in git; state
  never in config). Lease state is one file, `var/lease.json`.
- Setup is one path: `ops/bootstrap_local.sh` verifies the config, the
  module, and the cron's reaper duty alongside the existing CLI checks,
  so a rebuilt machine gets the whole lease machinery from bootstrap.

## Sentinel decisions — resolved (2026-10-29)

- `lease-defaults`: adopted as proposed (profile `workload` high-RAM,
  `max_hours` 8, budgets 10 CU/day and 100 CU/month) with one amendment:
  `idle_shutdown_minutes` defaults to disabled, per the idle ruling —
  XBRL/Workload work may use the lease at any given time, even when it has
  been idle.
- `away-acquire`: confirmed. Scheduled and wrapper-driven work may
  acquire a lease unattended; the one network approval per lease is the
  accepted prompt.

## Addendum — script reaper and auto-lease (2026-10-31)

Findings first: the agent cron carrying restore+reap was found silently
disabled after lease #7's death went unreaped ~5 h; duty-equivalent
replacement by a script-level hook is proven live, and the cron is
retired. Port forwarding through the SSH bridge passes no traffic
(session channels only), so no automation is built on it.

- Reaper carrier: hook `colab-lease-reaper` (120 s poll) runs mirror
  restore, then reap, at script level; it wakes the colab-runner chat
  only on a release/clear, an auto-acquire, or a command failure.
- Auto-lease: `auto_acquire.enabled` in `config/lease.json` (default
  off) gates a third hook duty. Demand is a file
  (`var/lease-request.json`, via `bin/colab-lease request`);
  acquisition goes through `acquire()` unchanged (budget guard,
  keepalive, pulse). Demand is consumed on a live or created lease and
  kept on refusal/failure. Tests: `tests/test_auto_fake.py` (6, fake
  CLI, flag via env override so the tracked config is never edited by
  tests).
- Proof protocol: the flag stays off until a supervised first fire —
  a demand planted ahead of lease #8's deadline (2026-11-01T11:03:06Z);
  after the reaper's release wake, the flag is flipped and the hook
  must acquire the next lease with no interactive exec in the loop,
  verified by record, keepalive heartbeat, and pulse.
