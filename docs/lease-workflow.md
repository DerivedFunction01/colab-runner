# Lease workflow — operating the shared "workload" session (v1, 2026-10-29)

Findings first: the lease is a warm VM with a hard deadline, not a job
service. Everything below exists to (a) never need a new session while
The operator is away, (b) make every result and failure come back on its own,
and (c) lose nothing when the reaper fires at the deadline.

## Invariants (from the operator's away brief, 2026-10-29)

- One session only: "workload". Wrappers acquire-or-adopt; while the operator is
  away, NEVER create a session of any profile — a fresh tunnel hostname
  needs an approval he cannot give.
- If the session dies before the deadline: do not reacquire. Park the
  work locally, record what was in flight, leave it for the operator's return.
  The reaper hook clears the dead lease record on its own.
- Jobs are detached processes (nohup), never kernel cells — cells die
  with the local CLI, detached jobs survive it. colab-run already
  launches detached; do not improvise other launch patterns.
- Never pass the live session's name to `colab run` (orphan-billing
  trap; server assignment list is truth; recovery is
  snippets/unassign_orphans.py, for the operator's return, not mid-lease use).
- One heavy job at a time (lease v1). Queue further heavy work locally.
- Ignore the "probe-low" session entirely.

## Job lifecycle (the workflow)

1. **Preflight (local, every job)**: `bin/colab-lease status` must show
   the lease active with deadline in the future. Read the local workload-repo
   HEAD; if the job needs a tree newer than the warmed snapshot, build a
   fresh snapshot bundle (sync/make_snapshot_bundle.sh) and apply it
   remotely (sync/apply_bundle_remote.py) BEFORE queueing the job —
   never mid-job. Record the shipped tree hash in the job's run record.
2. **Job zero — remote environment**: the first job on any fresh lease
   is an environment job, and no comparative result is trusted until it
   has passed on that lease. Job zero is `snippets/env_build.sh`
   (see the Shell channel section): Unit 0 resolves-or-installs `uv`
   (absolute path only, recorded in the report); the build clones the
   selected repo from the local registry (`config/repos.json` —
   untracked, seeded from `config/repos.example.json`; upload it to
   `/content/repos.json` with the build), pre-cleans any legacy
   inherited venv at the repo's declared venv path, drives the clone's
   own `init_venv.py` (the package spec lives there, never in this
   repo), and asserts isolation, the registry's version checks, and a
   clean import scan. The report (`env_report.json`) records the VM's
   Python version, the `uv` path/version/route, the init route taken,
   and the built venv's version map + fingerprint; the build also
   merges its record into `/content/env-registry.json`, which gates
   and consumer jobs resolve their clone and interpreter from.
3. **Submit**: `bin/colab-run <job>` with a manifest under jobs/<name>/.
   Manifest essentials: script, args, explicit timeout, expected
   outputs, checkpoint_pull_seconds (small enough that a dead session
   loses at most one checkpoint interval of results).
4. **While it runs**: the runner polls and checkpoint-pulls. Do not
   start a second heavy job; light status checks are fine.
5. **Completion**: the runner pulls final results into results/ and the
   run record reaches a terminal state. Verify returned files against
   the job's expected outputs and hashes before treating the job as
   done — a pulled directory is not proof of a complete job.
6. **Comparative results** (colab-check, census/gate reruns): the VM is
   Python 3.13, local gates run 3.11/3.12 — the repo is version-agnostic
   by design and there is no pin, so comparison is by output identity
   (hashes, fingerprints, gate verdicts), never by assumption. Record
   the interpreter beside every comparative result; a divergence is a
   finding to report, not a verdict to smooth over.

## Shell channel (2026-10-31)

Bare shell on the leased session is available and is the preferred way
to build environments and run commands — no kernel, no Python snippet
jobs:

- `bin/colab-sh` (repo root) runs a command, a `-f` script, or a stdin
  heredoc as bash on the session over the CLI's SSH bridge, with real
  exit codes. One SSH connection per runtime at a time; the wrapper is
  one-shot and never creates a session.
- **Job zero, shell edition**: `snippets/env_build.sh` is the one
  canonical env build (the Python twin `env_build_job.py` is retired).
  Run it via `bin/colab-sh -f snippets/env_build.sh -- --repo <id>` —
  or with no `--repo`, taking the registry's `default_repo` — after
  uploading `config/repos.json` to `/content/repos.json`. It is
  repo-agnostic: dest, venv path, the `init_venv.py` invocation,
  version checks, and scan roots all come from the registry entry.
  The currently registered repo's invocation is the plain drive
  `python3 init_venv.py` from the clone root (its script is
  non-interactive; Unit 0 seeds the resolved `uv`'s
  directory on `PATH` for its bare-`PATH` detection). A script that
  reports falling back to pip fails the build — on this image that
  fallback is the pip-less-venv route.
  As a job, env-build runs through `snippets/shell_job.py`
  (`jobs/env-build/job.json`, `--script /content/env_build.sh`);
  upload `env_build.sh` and `repos.json` to `/content` first.
- **Variant clones**: `--variant <name>` builds the
  `<dest>-<variant>` clone beside the base clone; after its
  `init_venv.py` run, the entry's recorded variant delta is applied
  to that clone's venv only, via the resolved `uv`, and reported as
  `variant_delta`. Two clones with conflicting version checks coexist
  on one session; each build's record in `/content/env-registry.json`
  carries its version-map fingerprint for cross-talk checks.
- **System-route audit (2026-11-01)**: no consumer needs repo packages
  in the VM's system python, so the old system-wide install is gone
  and no `--with-system` opt-in exists. System-python consumers are
  stdlib-only (keepalive, heartbeat, resource probe, reachability,
  renewal) or venv-served (the `colab-check` gate, the facade /
  fixture / profiler jobs, and `bootstrap_remote.py`'s optional
  requirements install, which now builds an isolated scratch venv).
  Named exceptions, both probes of the image itself that install
  nothing and are never gate substrates: `smoke_duckdb.py` reads the
  image's bundled DuckDB, and the OOM ramps use the image's torch
  (unavailability is recorded, not repaired). Two pip-era image
  defects are history of the retired route: `python3 -m venv` failed
  inside its own ensurepip step, and the inherited venv's pip
  installed nothing locally (ruff's launcher broke under the venv
  python); the `uv` route has neither defect.
- Port forwarding through the bridge (`-L`/`-R`) was measured
  2026-10-31 and passes no traffic — the bridge serves session
  channels only — so it is not a substrate for reaching VM services
  or for automation.

## Reaper and auto-lease (2026-10-31)

- The reap duty runs as a script-level hook (`colab-lease-reaper`,
  120 s poll, `~/hooks/scripts/colab-lease-reaper.sh`), not as an
  agent cron: the agent cron that originally carried it was found
  silently disabled after lease #7's death went unreaped for ~5 h,
  and was retired once the hook proved duty-equivalent (same two
  commands — mirror restore, then reap — same report-only-on-action
  rule). The hook wakes the colab-runner chat only when it releases
  or clears a lease, auto-acquires one, or a command fails.
- **Auto-lease**: `config/lease.json`'s `auto_acquire.enabled`
  (default off) gates a third hook duty. A driver records demand
  with `bin/colab-lease request [profile] --reason TEXT` (writes
  `var/lease-request.json`); on each poll, if a demand exists and no
  live lease does, the hook acquires through the ordinary `acquire()`
  path — same budget guard, keepalive launch, and kernel pulse. The
  demand is consumed when a lease is live or newly created, and kept
  when acquisition is refused (budget) or fails, so later polls
  retry. Skips are silent. This is the sanctioned exception to the
  away invariant above: the acquiring is done by the hook under
  The operator's config flag and a planted demand, never improvised by an
  agent. An agent-driven acquire can surface a network-approval card
  for the fresh tunnel host; the hook's script-level calls do not.
- Proof protocol: the flag stays off until a supervised first fire —
  a demand planted ahead of lease #8's deadline; after the reaper
  releases #8, the flag is flipped and the hook must acquire the next
  lease unattended, verified by record, keepalive heartbeat, and
  pulse.

## Extension and renewal (2026-11-01)

Findings first: a lease's deadline is a window end, and an extension
moves the end — never the session. Renewal keeps a window alive
across the sessions it outlives.

- **Extend**: `bin/colab-lease extend SESSION --hours H` (panel:
  the lease card's Extend control) moves the deadline by H, priced by
  the budget guard on the delta only (rate × H). The record gains
  `renew: true` — it is now a window lease. `extend-grant --hours H`
  moves the two-tier grant's `until` the same way, pricing the
  lobby's delta only; the lobby itself refuses direct extension
  (`extend-standing-managed`) because the grant owns its lifetime.
  A release already in flight loses to an extension: `release()`
  re-reads the record before stopping and aborts when the deadline
  has moved.
- **Renewal** is a reaper branch for `renew: true`, non-standing
  records whose window is still open, fired when the session is
  confirmed absent or its age passes the threshold (platform cap
  12 h − 30 min). Driver 1 (preferred, when the renewal kit is
  installed): `snippets/renewal.py` in the VM mints the child session
  at the threshold and writes a handover file; the reaper merges the
  child's binding into the local sessions file, swaps the record,
  and stops the parent once the child answers. Driver 2 (fallback):
  the reaper creates a fresh session against the same window end and
  swaps the record. Renewal never re-prices the window and preserves
  deadline, jobs, and profile; a failed renewal retries next cycle,
  and a window that ends first releases normally. Unflagged records
  are never renewed — the 2026-10-29 away rule stands.
- **Announcements**: extensions and renewals land in the UI outbox as
  `lease-extended` {profile, hours, old_deadline, new_deadline},
  `grant-extended` {hours, old_until, new_until}, and `lease-renewed`
  {profile, driver, old_session, new_session}; the notify hook
  announces each to the main chat. The outbox has exactly one writer
  (the companion server, which owns seq allocation), and renewals
  happen inside reap(), outside the server — so the core spools
  renewal events to `var/reaper-events.jsonl` and the server drains
  the spool into the outbox on its state/outbox reads (byte-offset
  marker in `var/reaper-events.drained`; only complete lines are
  consumed, so a writer mid-append is never half-read).
- **Main → users relay**: the hooks deliver to the main chat only,
  so relaying onward is a main-chat duty. When an extension or
  renewal changes a deadline that a consuming thread (e.g. the XBRL
  chat) is planning against, the main thread relays the new deadline
  to that thread in the same turn the announcement arrives — a
  consuming thread must never plan against a superseded end time.
  The panel reflects the new deadline on its next poll, from served
  state only.

## The two-tier envelope (2026-11-01)

Findings first: the grant is the only tap. Everything inside its
scope afterwards is script-level machinery and served state.

- **The grant is an authorization envelope.** Starting a grant
  records its scope on the grant record: the sanctioned profiles
  (lobby + burst) and a snapshot of the budget caps in force at
  grant time. The grant's `granted_at` is the envelope id.
  `extend-grant` moves only the window, never the scope. The
  panel's cap editor therefore shapes future grants and ordinary
  leases — never a running envelope.
- **Inside the scope**, acquires price against the snapshot caps
  (the standing duty included, through `acquire`), and created or
  adopted records are stamped with `envelope_id` and the requester
  tag `via`. Outside the scope, a lease is an ordinary lease: live
  config caps, neither stamp.
- **Threads start bursts by demand, never by agent-fired
  commands**: `bin/colab-lease request --via TAG
  --reason TEXT` plants the demand; the hook's auto duty creates
  the lease and copies the tag onto its record. A timed start is
  the same record with `--at ISO-TIME` (a `not_before` stamp): the
  duty leaves it untouched until due, then fires it — no agent and
  no operator present. Panel starts stamp `via: panel` and are
  creations, never adoptions (`acquire --create`): starting mints
  a parallel lease at the shape; using the current lease is the
  extend path. The job
  runner stamps `via: runner`; the standing duty stamps
  `via: standing`.
- **Tracking**: the envelope lifecycle is ledgered in
  `var/grant-ledger.jsonl` (grant started, lease attributed,
  renewed, released, grant ended), and `state` serves an
  `envelope` block — scope, attributed live records, spend versus
  the snapshot caps — which the lease panel's grant card renders.
  Envelope lease starts/releases also ride the reaper-events spool
  into the UI outbox as `envelope-lease-started` /
  `envelope-lease-released`.
- **The beachhead question stays open by design**: the lobby may
  broker burst starts later, but direct starts are the standing
  path until a live probe (the envelope plan's Step 6) records
  brokered starts beating them. Tap-freedom is the invariant;
  topology is settled by measurement, not preference.

## Callbacks — how results and failures come back

- **Run records** (results/runs/<run-id>.json) are the callback
  substrate: every state change lands there; the runner's orphan sweep
  reconciles non-terminal records against live sessions on each entry.
- **Chat reporting**: completed jobs and every failure class below are
  reported to the XBRL chat (this lease's home thread) as they are
  observed — by the invoking agent for foreground work, and by the
  deadline sweep (below) for anything still outstanding.
- **Deadline sweep**: a local run-once job at T-30 min before the hard
  deadline checks lease status and all non-terminal run records, pulls
  anything the runner has not, records the final state and the
  idle-behaviour observations, and reports to the XBRL chat. After the
  sweep, no new job is submitted — the reaper owns the session from
  T-0 regardless of what is running.
- **Failure taxonomy** (each class has a different correct response):
  - *Job failure* (non-zero exit, gate red): results + logs are pulled;
    report with the failing step; the lease is unaffected and the next
    queued job may proceed.
  - *Transfer failure* (hash mismatch, part failure): the chunked
    path retries each part (3 attempts) and resumes per part by
    SHA-256 — re-run the transfer and only the missing or corrupt
    parts move (landed 2026-10-31, after the measured 1024 MB
    part-12 failure killed a whole transfer). A 1024 MB-class
    transfer is still slow at ~1.6 MB/s; split or shrink the payload
    when the window matters.
  - *Session death* (status check fails, session absent): STOP. Park,
    record in-flight run-ids and their last pulled checkpoint, report.
    No reacquire while the operator is away.
  - *Deadline overrun* (job still running at T-30): the sweep pulls the
    latest checkpoint and records the job as deadline-killed; its
    run-id is the resume handle for the operator's return.

## Data movement (per the priced per-class strategy)

- Snapshot bundles for code: origin-lineage only, SHA-256 + byte size
  recorded, ~50 MB single-file ceiling — a bundle is ~47 MB, inside it.
- metadata snapshots: always transfer, never refetch (refetch is not
  identity-preserving). filing_catalog: regenerate on the VM from the
  transferred metadata snapshot, verify by snapshot identity.
- Document acquisition (new batch): move only document_planning; the
  VM fetches disjoint accession shards under its own rate window
  (declared UA via exec --env, repo cap 8 rps). Existing working sets:
  bulk-transfer only the named set, chunked above ~50 MB.
- SEC is reachable from the VM (200, declared probe UA measured).

## Edge cases

- **Idle reap is unprobed** (plan probe P-L1): whether an untouched
  session survives to the deadline is unknown. Do not invent keepalive
  traffic; DO log every observed lease state change with timestamps —
  this lease's usage is the first real data.
- **Lease RECORD loss is not session death** (measured 2026-10-29):
  var/lease.json was lost twice in one afternoon (once unexplained,
  once by an un-isolated test run) while the session itself never died
  — same endpoint throughout. A "no-lease" reading is a bookkeeping
  event, not a death: before declaring death, confirm absence against
  the server assignment list (the truth), and treat an unreadable
  sessions list as UNKNOWN, never as death. The park-and-never-reacquire
  rule fires only on CONFIRMED absence. lease.py is hardened
  accordingly (reap clears only on confirmed-absent; release keeps the
  record when a stop fails; tests isolated via COLAB_RUNNER_VAR).
- **VM restart of the local machine**: transient state restores from
  the repo mirror via the existing hook/wrappers; lease record is in
  var/lease.json (untracked) with the server assignment list as the
  fallback truth.
- **Clock discipline**: the deadline is UTC in the lease record
  (2026-10-30T01:37:14Z = 21:37 EDT). All scheduling here is computed
  from the record, never from memory of it.
- **CU budget**: burn is 0.26 CU/h while assigned, idle or not; the
  lease config's budgets (10/day, 100/month) and the hard deadline are
  the controls. Cost is never a reason to stop a session early — the
  reaper owns the end.
- **probe-low**: an unrelated standard-tier session that stops itself.
  Not a fallback, not a resource: ignore.

## This lease (first run of the workflow)

- Acquired 2026-10-29T17:37Z; deadline 2026-10-30T01:37:14Z; balance at
  acquire 581.67 CU. Warmed tree: snapshot commit 21a85ca2 (parent
  c07b1210, tree 3c6a2716) at /content/workload-repo.
- First queued work under this workflow: job zero (remote venv build +
  toolchain assertion), then a comparative gate via bin/colab-check on
  a scoped path set. Heavy queue beyond that waits for job zero.
- OUTCOME (2026-10-29): the session died server-side between 15:54 EDT
  (verified active from the XBRL chat, last_used 19:53Z) and 16:43 EDT
  (reaper read it absent; colab-runner chat confirmed against the
  assignment list: empty, usage 0.00/hr, nothing billing). Lifetime
  bound: acquired 17:37Z, so it lived between ~2h17m and ~3h06m of the
  8h window — the first P-L1 datum point: a warmed session carrying no
  job load did NOT survive to the deadline. Cause not established
  (Google-side reap/recycle candidates); control-plane status reads did
  not keep it alive, or do not count as activity. No jobs had been
  submitted from the XBRL chat (results/runs/ empty): nothing in
  flight, nothing lost. Per the away rule the lease was NOT reacquired;
  job zero + the comparative gate are parked for the operator's return. The
  21:05 EDT sweep was converted to a reconciliation-only pass.
- RECONCILIATION (2026-10-29 21:05 EDT, 2026-10-30T01:05:23Z):
  `bin/colab-lease status` = no-lease; results/runs/ still empty —
  final state confirmed, nothing in flight, nothing lost, no session
  created or reacquired; work parked for the operator's return.


## Attributed spend accounting (2026-11-01)

Findings: the budget guard's day/month spend used to be a
balance-baseline delta, so every balance movement booked as repo
spend. On 2026-11-01 the balance stepped ~66.7 CU within an hour
while the live leases could burn at most ~0.37 CU (a credit
expiration or external charge — the CLI's usage output carries no
breakdown to distinguish them); the guard read day_spent 71.17
against the 15 CU daily cap and refused a 1 h lobby start
projecting 0.08 CU with 699 CU in the bank. The repo's true burn
that day was ~4.8 CU.

Mechanism: spend is now attributed to this repo's own leases.
Every session assignment is a lease record burning its profile's
rate; completed assignments are journaled as segments
(var/lease-spend-journal.jsonl) at release, renewal swap, and
confirmed-dead clear, and live records contribute their running
segment. Day/month/per-profile figures derive from the journal
plus live records, split at the period boundaries — the numbers
the guard enforces and the panel serves are the same derivation.
The balance remains only as a floor: a window projecting more
than the account holds refuses `balance-insufficient`.

Limits, stated plainly: segments completed before this landed
were never journaled and are not reconstructed (counters are
exact from the first full UTC day after landing); a segment
closed by a confirmed-dead clear ends at the reap that detected
the death, overcounting by at most the detection lag; an
assignment with no record burns uncounted (the orphan sweep is
its cure); an unconfirmed-rate profile contributes nothing
rather than a fabricated number.


## Envelope Steps 6–7: the broker verdict and the live proof (2026-11-01)

Step 7 ran live under a 12 h grant (scope lobby + workload, snapshot
caps 15/150/4): the standing duty raised the lobby unattended,
a demand planted with `--via step7-proof` started the workload burst
through the auto duty — envelope-stamped, priced on attributed
spend, ledgered — and the burst was released after verification,
its burn journaled (98 s at 0.26 CU/h). The grant and lobby stay
up for the window; threads now start bursts exactly this way:
plant a demand, let the hook fire it, release when done.

Step 6 measured the alternative — starting the burst from inside
the lobby (the beachhead broker) — and the broker does not win.
One-time beachhead prep (token placement + in-VM CLI venv build)
took 29.6 s; warm brokered mints took 4.6 s / 4.5 s against 4.6 s
for a direct mint of the same
shape, and the Step 7 direct burst completed inside one 47.7 s
reap pass. A brokered session is also born locally unbound — it
cannot be addressed by name until its binding is transplanted —
and the in-VM stop form proved exact: `colab stop -s <name>`
works, while the `--yes` flag and the positional form both fail
rc 2 — the flag poisoned the probe's own child stop, and the
one orphan that caused took a transplant plus `stop_session_safe`
to clear (isolated by the bridge probe, same day). Verdict:
direct starts through the hook machinery are the burst path; the
deferred warm runner is settled as not built; the beachhead's
role stays what it already proves at — renewal driver 1. Stops
go through the lease core's `stop_session_safe`, never the `-s`
flag form.

One recovery gap observed during the proof: the reaper hook's
poller stalled for ~11 minutes across a service restart and
resumed after a manual hook run (cadence normal since). The
duties themselves were unaffected once polling resumed.

## The lobby bridge (2026-11-01)

The standing lobby doubles as a bridge into Colab (plan:
`.plans/finished/2026-11-01-lobby-bridge.md`). An agent in the
lobby (`snippets/bridge_agent.py`, installed by the lease core's
bridge duty riding the standing pass) polls a queue of signed
command files, executes them — OS commands in the lobby, or
relayed into child sessions the lobby mints — and appends every
outcome as a JSON line to a segmented log. Threads and crons
drive it two ways: through `bin/colab-bridge` (cmd / mint / stop /
ping / log / status / children), or by files alone — signed
commands dropped in `var/bridge-outbox/`, outputs read from
`var/bridge-mirror.jsonl`, which the ferry (same duty pass)
keeps current by (generation, segment, offset).

The invariants, all live-proven on the first grant lobby: a
command file carries a SHA-256 over its canonical JSON, so a
partially planted file never executes; a command id executes at
most once per bridge generation (a re-plant is answered with a
duplicate line naming the original); a command is taken at most
twice, so one whose execution kills the agent quarantines
instead of looping; children live in a reserved `bridge-`
namespace under a config cap, are pulsed by the agent, and are
reaped on idle or age; the agent's heartbeat loss re-arms it
under the same generation (log sequence and registry resume),
while a recycled lobby starts a new generation and the ferry
re-plants whatever was unacknowledged. Bridge children hold no
lease record, so their stop lines fold into the attributed
spend journal as the ferry mirrors them — bridge burn is
counted like any other. The bridge dies with the grant; the
mirror is the record of what ran.

The live proof (2026-11-01, generation e4c35deb): ping → pong
in ~5 s; a self command returned the lobby's hostname; a minted
high-RAM child (3.9 s) answered a relayed command with its own
hostname and 53 GB in 3.5 s; an exit-3 command returned rc 3 and
a sleep past its timeout returned a timeout line, the agent
unharmed; a re-planted command was answered as a duplicate; the
child cap refused a fourth mint by name; an external kill was
re-armed by the duty within one pass, sequence and registry
intact. The generation-change drill was not run live (the lobby
was the operator's live grant asset); its logic is unit-tested.

## Parallel instances for low-tier CPU profiles (2026-11-03)

An eligible profile may run one extra session next to its primary
(plan: `.plans/2026-11-03-parallel-cpu-sessions.md`). Eligibility
is the lease core's determination, served ready in each spec's
`parallel` block: CPU shapes only — no GPU, no TPU (per-account
accelerator concurrency is unverified and accelerator rates are
unmeasured; widening is a rule change in `parallel_eligible`, not
a rebuild). At most one extra instance per profile.

The instance is a derived profile, not a new lease kind: its
profile id and session name are the base's plus `2` (the High-RAM
instance is profile `workload2`, session `workload2`), with the base's
shape and measured rate and its own record
(`var/lease-workload2.json`). Every profile-keyed path — acquire,
reap, keepalive, kernel pulse, renewal, spend — covers it
unchanged; spend segments journal under the instance's own
profile and session at the shared rate.

The toggle is per base profile in `config/lease.json`
(`"parallel": {<profile>: true}`), default off, set from the
panel (`POST /api/config/parallel`, riding `bin/colab-lease
parallel <profile> --on|--off`) or the CLI verb directly.
Toggling on registers the instance in the served catalog and
starts nothing; toggling off refuses while the instance lease
exists — the toggle never stops a session.

Adopt-foreign: acquiring an instance whose session already
exists live (created outside the lease, e.g. by a direct CLI
call) writes the record against the existing session and proves
it with keepalive + kernel pulse — no `new` and no `stop` are
issued against it. An unreadable session list refuses
(`acquire-unknown`), as everywhere. The budget guard prices the
pair as the sum it commits: either side's check adds the
sibling's remaining window (rate × time to its deadline,
reported as `sibling_projected`) to its own projection for the
balance floor and the daily/monthly caps.

Live proof (2026-11-03): the CLI-created `workload2` High-RAM
session was adopted in place (`adopted-foreign`, pulse ok,
keepalive launched, sibling projection 1.59 CU priced against
the primary's remaining window) with the primary workload lease's
session and deadline untouched, and the reaper kept both across
subsequent cycles. The stop path was proven on a standard-tier
instance instead: `standard2` created, pulsed, released, and
journaled at 0.08 CU/h, after which its toggle came off cleanly.


## Session shape gate (2026-11-04)

Agent actors may create only High-RAM CPU sessions. The incident
behind the gate: a session bound under a High-RAM profile name
proved to be a Standard assignment (endpoint prefix `m-s-`), and
Standard-tier work ground serially on it for hours — the profile a
caller names is not proof of the shape the server assigns. The
gate is a creation-time policy, not a lifecycle policy: adopting
or reusing a live session, `extend`, and `release` are never
gated.

### Lifecycle

- **Birth:** the policy is born as the `session_gate` block in
  `config/lease.json` (`enabled`, `agent_classes`,
  `bypass_actors`, `infra_actor`). An absent block enforces the
  built-in defaults — High-RAM only, panel bypass, standing infra
  actor — so deleting the block never opens the gate.
- **Reads:** the core reads the block fresh on every invocation;
  there is no cached or compiled copy to go stale.
- **Change:** edit the block. Widening `agent_classes` or
  `bypass_actors` is a config edit, reviewed like any other.
- **Death:** only an explicit `"enabled": false` disables the
  gate. Nothing else — no env var, no flag — turns it off, and a
  disabled gate still resolves through the same code path
  (fixtures use the explicit disable to state their intent).

### Where it sits

- `acquire()`'s creation branch (the adopt/reuse paths above it
  are untouched), including the demand path: `request` refuses an
  attributed, catalogued, blocked demand at write time, and
  `auto` consumption clears a refused demand (`skipped-shape`)
  instead of retrying it forever.
- Renewal re-creation, as a bare class check — no actor bypass
  ever applies to a renewal. A refused renewal recreates nothing;
  the record persists and the reap surfaces `renewal-failed`.
- The wrappers that create sessions outside the lease core
  (`bin/colab-up`, `bin/colab-run`'s lease-disabled fallback)
  consult the core's single verdict seam, `bin/colab-lease
  admission [profile] [--via TAG]`, before any CLI call.
- The bridge agent's mint: kit install stamps
  `agent_allowed_profiles` into the bridge config, and the agent
  refuses any other profile at mint time (a config without the
  stamp derives the same set from its snapshot's shapes).
- A post-start assertion on every fresh create in `acquire()`:
  the local binding's endpoint host class (`m-hm-` / `m-s-`) is
  compared with the profile's declared RAM class. A proven
  mismatch is stopped via `stop_session_safe` and refused as
  `shape-mismatch` — no lease record is written. Unreadable or
  unparseable evidence never triggers the assertion.

### Actors

A requester's identity is the pair (via tag, `COLAB_LEASE_ACTOR`
env claim). A bare via tag is caller-asserted and never sufficient
for a bypass.

- **Agent** (anything else): may create only `agent_classes`
  shapes (High-RAM). GPU/TPU profiles are their own classes and
  are agent-blocked.
- **Panel:** bypasses only with both halves — the server sets
  `COLAB_LEASE_ACTOR=panel` in the child env on every core call
  (and the unit/ui.sh carry the same claim), and the action calls
  pass `--via panel`. Either half alone is refused.
- **Standing infra:** the infra actor (`standing`) admitting
  exactly the configured standing profile (the Standard lobby)
  rides the exemption; the same actor is gated like any agent on
  every other profile.
- **Probe door:** `COLAB_LEASE_ACTOR=test` is whitelisted in
  `bypass_actors` for deliberate live probes of Standard/GPU
  shapes. It is set in the probe's own command environment only,
  never exported into a shell or a unit, and every use is
  journaled as a bypass.

### Journal and surface

Refusals, load-bearing bypasses, infra admissions, refused-demand
dispositions, and shape mismatches are appended to
`var/session-gate-journal.jsonl` (kinds `shape-refused`,
`shape-bypass`, `shape-infra`, `demand-shape-refused`,
`shape-mismatch`, each naming the seam, profile, and shape class).
`state` serves the resolved policy as `session_gate` and each
spec's `agent_startable`, so the panel renders the gate's effect
from served data.
