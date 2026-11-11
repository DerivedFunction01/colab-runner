# Lease workflow — operating the shared sessions

Findings first: a lease is a warm VM with a hard deadline, not a job
service. A lease is {session, caller, shape, window}; records are
per-session (`var/lease-<session>.json`) and are read exactly as
written. Everything below exists to keep a live lease usable to its
deadline, make every result and failure come back on its own, and
lose nothing when the reaper fires at the deadline.

## Job lifecycle

A session is another computer: a job fired at it executes
immediately, concurrently with whatever else is running there —
jobs are never serial behind another job, and co-tenancy is
recorded on the lease record's `jobs` list. The runner manages no
workspaces and checks no file isolation: scoping a job's files on
the session is the coordinator's competence, exercised the way it
is on any computer.

1. **Preflight (local, every job)**: `bin/colab-lease status` must
   show the lease active with its deadline in the future. Read the
   local HEAD of the repo being shipped; if the job needs a tree
   newer than the warmed snapshot, build a fresh snapshot bundle
   (sync/make_snapshot_bundle.sh) and apply it remotely
   (sync/apply_bundle_remote.py) BEFORE queueing the job — never
   mid-job. Record the shipped tree hash in the job's run record.
2. **Job zero — remote environment**: the first job on any fresh
   lease is an environment job, and no comparative result is
   trusted until it has passed on that lease. Job zero is
   `snippets/env_build.sh` (see the Shell channel section): Unit 0
   resolves-or-installs `uv` (absolute path only, recorded in the
   report); the build clones the selected repo from the local
   registry (`config/repos.json` — untracked, seeded from
   `config/repos.example.json`; upload it to `/content/repos.json`
   with the build), pre-cleans any legacy inherited venv at the
   repo's declared venv path, drives the clone's own
   `init_venv.py` (the package spec lives there, never in this
   repo), and asserts isolation, the registry's version checks,
   and a clean import scan. The report (`env_report.json`) records
   the VM's Python version, the `uv` path/version/route, the init
   route taken, and the built venv's version map + fingerprint;
   the build also merges its record into
   `/content/env-registry.json`, which gates and consumer jobs
   resolve their clone and interpreter from.
3. **Submit**: `bin/colab-run <job>` with a manifest under
   jobs/<name>/. Manifest essentials: script, args, explicit
   timeout, expected outputs, checkpoint_pull_seconds (small
   enough that a dead session loses at most one checkpoint
   interval of results).
4. **While it runs**: the runner polls and checkpoint-pulls the
   declared outputs, so partial results land during the run, not
   only at the end. A checkpoint read that stalls degrades to a
   recorded poll error on the run record; the poller keeps
   polling.
5. **Completion**: the runner pulls final results into results/
   and the run record reaches a terminal state. A job payload
   that exits non-zero records the run failed — outputs are still
   pulled first. Verify returned files against the job's expected
   outputs and hashes before treating the job as done — a pulled
   directory is not proof of a complete job.
6. **Comparative results** (colab-check, census/gate reruns): the
   VM's Python differs from this machine's and the repo is
   version-agnostic by design, so comparison is by output identity
   (hashes, fingerprints, gate verdicts), never by assumption.
   Record the interpreter beside every comparative result; a
   divergence is a finding to report, not a verdict to smooth
   over.

The runner's own lifecycle verbs ride the same record:

- `bin/colab-run <job> --detach` launches and returns at once;
  `--poll RUN_ID` performs one bounded reconcile (sweep-for-self,
  one reader pass, pull, record update) — exits 0 complete /
  1 failed-or-orphaned / 2 no record / 3 still running;
  `--wait RUN_ID [--max S]` waits on the local record only.
  When a launched run reaches a terminal state, exactly one
  `run-terminal` event is appended to `var/ui-outbox.jsonl` in
  the UI events' shape — the runner core is the outbox's one
  writer besides the UI.
- The orphan sweep is not on the launch path: it runs under
  `--poll` and an explicit `--sweep`, matching live sessions by
  exact name.
- Three consecutive poll failures classify the session at the
  reduced read budget (2 × 45 s): confirmed-absent fails the
  run fast with a `--resume` note; unknown never fails the run.
- A run may pin its session: `--session NAME` (or
  `$COLAB_RUN_SESSION`) makes the acquire adopt the named lease
  or fail `pin-unavailable` — a pin never silently substitutes
  another session and never clears the named record.
- Adoption reads launch health: the launch path journals every
  upload/probe/seed/launch outcome to `var/launch-health.jsonl`,
  and within each caller tier a session whose recent launch
  operations failed ranks below healthy ones (an all-degraded
  pool still adopts, flagged on the result). Health reorders
  placement only; it never reorders caller tiers.
- A launch is confirmed against the venue: the launch exec's
  result is checked, then one reader pass looks for the job's
  footprint (pids, launcher log, checkpoint, or exit stamp —
  the stamp wrapper opens the log at spawn). A footprint
  stamps `launch_confirmed_at`; when the channel answers and
  no footprint exists, the run closes `failed` with
  `launch_lost` set. When the confirmation read itself can
  produce no evidence, the record carries `launch_unconfirmed_at`
  (polling if the venue accepted the launch, `running` if its
  exec also failed) and reconciles decide: the same
  launch-lost verdict fires when a reader finds total absence
  600 s past the launch, and the sweep closes a launched
  record still sitting in `running` after 20 minutes with no
  fresher evidence. Every channel call on the launch path is
  timeout-guarded — `colab()` re-raises timeouts by design,
  and a timeout is a recorded failure, never a dead launcher
  stranding the record. `launch_confirmed_at` and
  `last_evidence_at` on the record are the liveness evidence
  supervision reads.
- The sweep also reconciles the pre-launch state: a
  `starting` record bound to a dead session is orphaned like
  its running siblings, and one whose freshest stamp (or file
  mtime — every pre-launch save rewrites the file) is over
  two hours old closes `failed` with `launch_lost` set — the
  bound clears the worst legitimate pre-launch hold (one
  wedged upload call plus its surrounding phases). A resume
  of a run that already launched keeps the record's launched
  state; only its own launch re-stamps it.
- Pulls are change-detected: the reader payload reports each
  declared output's size/mtime and unchanged outputs are
  skipped; outputs over the 50 MB single-file ceiling move via
  the chunked transfer. Every output a run lands is recorded
  in the record's `pulled` list with its route, and finalize
  reconciles the pull against the manifest: a declared output
  that never arrived is named in the record's
  `missing_outputs` and in the run's result — the payload's
  exit code stays the outcome truth, but a short pull is
  never silent.

Jobs are shape-addressed, not session-pinned: acquire picks
the shape's shared session, whichever one is live. A job whose
inputs already live on one session's filesystem (a prior job's
outputs, a hand-built venv) must stage them as manifest
`uploads` or a seed bundle — or run as `bin/colab-sh --detach`
work on that session, which is the session-pinned route.

Two launch rules bind every job:

- Jobs are detached processes, never kernel cells — cells die with
  the local CLI, detached jobs survive it.
- Never pass a live session's name to `colab run`: `run` executes
  on its own fresh VM, releases that VM, and removes the local
  binding for the name, orphaning the live assignment (it keeps
  billing server-side and `stop` cannot see it). The server
  assignment list is truth; orphaned assignments are released with
  `snippets/unassign_orphans.py`.

## Shell channel

Bare shell on the leased session is available and is the preferred
way to build environments and run commands — no kernel, no Python
snippet jobs:

- `bin/colab-sh` (repo root) runs a command, a `-f` script, or a
  stdin heredoc as bash on the session over the CLI's SSH bridge,
  with real exit codes. One SSH connection per runtime at a time;
  the wrapper is one-shot, checks the session is live through the
  lease machinery before connecting, and never creates a session.
- A session name resolves as: the `-s` flag, then
  `$COLAB_SH_SESSION`, then the most recently used lease record —
  there is no other default.
- `--detach` launches register a record at
  `results/detached/<id>.json` and a `detached:<id>` job on the
  lease record; `--poll` closes the record out when the launch
  finishes. `bin/colab-sh --wait ID [--max S]` waits on that
  local record only (0 complete / 1 failed-or-missing /
  3 still running).
- On the sync path the wrapper validates the script, checks the
  duration cap and the claim gate BEFORE any dial — a refusal
  makes zero dials. Liveness verdicts cache for 60 s
  (`var/liveness-<session>.json`), the async path reads at the
  reduced budget (2 × 45 s), and the transient mirror restore is
  skipped while its stamp is fresh (< 300 s).
- A channel-busy answer (HTTP 429) is retried briefly on the
  async path and translated on every path into a named verdict —
  holder and claim expiry when a claim exists — never a bare
  ssh failure.
- Attaching to a live session with no lease record prints an
  advisory naming the orphan state and the adopt command; the
  attach proceeds.
- **Job zero, shell edition**: `snippets/env_build.sh` is the one
  canonical env build (the Python twin is retired). Run it via
  `bin/colab-sh -f snippets/env_build.sh -- --repo <id>` — or with
  no `--repo`, taking the registry's `default_repo` — after
  uploading `config/repos.json` to `/content/repos.json`. It is
  repo-agnostic: dest, venv path, the `init_venv.py` invocation,
  version checks, and scan roots all come from the registry entry.
  A script that reports falling back to pip fails the build — on
  this image that fallback is the pip-less-venv route. As a job,
  env-build runs through `snippets/shell_job.py`
  (`jobs/env-build/job.json`); upload `env_build.sh` and
  `repos.json` to `/content` first.
- **Variant clones**: `--variant <name>` builds the
  `<dest>-<variant>` clone beside the base clone; after its
  `init_venv.py` run, the entry's recorded variant delta is
  applied to that clone's venv only, via the resolved `uv`, and
  reported as `variant_delta`. Two clones with conflicting
  version checks coexist on one session; each build's record in
  `/content/env-registry.json` carries its version-map fingerprint
  for cross-talk checks.
- **System-route audit, as present fact**: no consumer needs repo
  packages in the VM's system python, so nothing installs there
  and no `--with-system` opt-in exists. System-python consumers
  are stdlib-only (keepalive, heartbeat, resource probe,
  reachability, renewal) or venv-served (the `colab-check` gate,
  the facade / fixture / profiler jobs, and
  `bootstrap_remote.py`'s optional requirements install, which
  builds an isolated scratch venv). Named exceptions, both probes
  of the image itself that install nothing and are never gate
  substrates: `smoke_duckdb.py` reads the image's bundled DuckDB,
  and the OOM ramps use the image's torch (unavailability is
  recorded, not repaired).
- Port forwarding through the bridge (`-L`/`-R`) passes no
  traffic — the bridge serves session channels only — so it is
  not a substrate for reaching VM services or for automation.

## Synchronous channel claims

A runtime admits one SSH connection at a time, so a synchronous
`colab-sh` / `colab-fs` call holds the channel for its whole
duration. Synchronous use is therefore a claim, not a default
(design: `.plans/2026-11-08-sync-channel-design.md`):

- A session has at most one sync claim: {caller, claimed_at,
  expires_at, last_attach_at}, TTL'd by `sync_claim.ttl_minutes`
  (lease config, default 60). It lives on the lease record when
  a record exists, else at `var/sync-claim-<session>.json`;
  both homes are written staged-sibling + rename.
- Activity is evidence, read per attach: a job registered on
  the lease record whose backing the pruned projection still
  counts as live, a non-terminal run record naming its caller
  whose file has been re-saved inside 65 minutes (a live run
  is re-saved on every reconcile outcome; older is a dead
  launcher's residue and holds nobody active), a running
  detached record inside one 12 h session lifetime, or a
  channel attach in the caller-claims journal inside
  `sync_claim.attach_window_minutes` (default 15).
- `sync_rights` (`sync/lease/syncclaims.py`) is the one gate,
  checked by `bin/colab-sh` before connecting and by the
  probes' driver for its long calls: a live claim held by
  another caller refuses, and any other active caller refuses
  — synchronous is off while multiple threads are active, for
  the claimant too; their claim is not revoked and resumes
  when the session is single-occupancy again. A sole active
  caller's attach auto-claims, journaled in the caller-claims
  journal. Refusals exit 2 naming the holder, the evidence,
  the expiry, and the detached form of the refused command.
- A caller marked active in the caller registry
  (`config/callers.json`'s "active" list) is refused before
  any of that: active means a potential use for Colab exists
  for that caller even when no session is currently up
  (owner ruling 2026-11-10), so while the marking stands its
  synchronous attaches refuse outright (`sync-refused-active-
  caller`, exit 2 with the detached form) — no claim is
  granted, and no occupancy state changes the verdict. A lane
  suffix inherits its base's marking. Detached work is
  unaffected, as for every caller.
- A claim lapses at its TTL or when its holder goes quiet
  (last attach older than the window, no other live evidence);
  a lapsed claim reads as absent. `bin/colab-lease claim-sync`
  and `release-sync` take and end a claim explicitly; only
  the holder releases by name.
- Detached work never needs a claim: `colab-run` jobs and
  `bin/colab-sh --detach` / `--poll` (launch under nohup at
  `/content/sync-detached/<id>`; poll state + log tail with
  one short call) stay open to every caller. The records
  state serves each lease's live claim as `sync_claim` (or
  null), and the panel card renders holder + expiry from it.
- Duration is capped as well as admission:
  `sync_claim.max_sync_seconds` (default 300) bounds a sync
  call, and the wrapper's default timeout resolves to the cap.
  A longer sync call refuses pre-connect, printing the detached
  form — or runs under `--long`, which journals the hold on
  the claim: the hold counts as live claim evidence until its
  stated end, so the claim cannot lapse mid-hold.
- A claim refresh whose store write fails degrades instead of
  refusing: the attach proceeds on the stored claim and the
  degradation is journaled. Grants stay strict — a claim that
  cannot be stored is not granted.
- Claim evidence reads a bounded tail of the caller-claims
  journal (256 KiB); the evidence window is minutes, the tail
  covers days.
- State reads cache their two account dials (the usage call
  and the live list) for 30 s in `var/state-cache-*.json`; the
  access list rides the same live-list cache. A failed dial is
  never cached, and a session's tri-state classification is
  never served from cache.

## Reaper and auto-lease

The reap duty runs as a script-level hook (`colab-lease-reaper`,
120 s poll; its script is registered with the runtime's hook
system, outside this repo). Each pass runs, in order:

1. **Approval-gate evaluation** (below).
2. **Transient mirror restore** — the local machine's token and
   session state are repaired from the repo mirror per item.
3. **Liveness reconciliation** — a local duty (it runs even while
   the gate stands): `bin/colab-liveness --sync` arms scheduler
   entries for live leases missing one and retires entries whose
   record is gone.
4. **Network duties** — reap, auto-acquire, and the standing
   duty; skipped while the approval gate stands for a host with
   no proven contact.

**Approval gate**: dialing a tunnel host whose endpoint has never
been reached can mint a network-approval card. The gate tracks
per-host contact state: a host with no proven contact defaults
CLOSED (the network duties skip it), one probe pass per host per
hour is allowed, the probe stamp is written when the pass is
decided — before any duty dials — and a host once reached latches
`contact_ok` and is never gated again.

**Reap**: passes are mutually exclusive (flock). Reap classifies
each record's session through the core's tri-state read, which
retries transient failures inside a bounded budget before a
session may read `unknown` — and unknown is never treated as
dead. A lease record is deleted only on a confirmed-absent
reading or a successful stop, never on an unreadable one. Reap
also keeps live sessions warm: it relaunches the detached
keepalive loop (CPU touch + heartbeat every 60 s) whenever its
heartbeat goes stale, and pulses the session's kernel with a
trivial exec every `pulse_seconds` — a pulse timeout reads as
busy, since a kernel that cannot take the pulse is already
executing. The idle path releases a lease only past its idle
threshold AND with an empty `jobs` list: registered jobs are
protection, not activity bookkeeping.

**Auto-lease**: `config/lease.json`'s `auto_acquire.enabled`
(default off) gates the auto duty. Demand is planted with
`bin/colab-lease request` (writes `var/lease-request.json`); on
each poll, if a demand exists and no live lease satisfies it, the
hook acquires through the ordinary `acquire()` path — same budget
guard, keepalive launch, and kernel pulse. The demand is consumed
when a lease is live or newly created, and kept when acquisition
is refused (budget, shape gate) or fails, so later polls retry. A
demand may carry `--at ISO-TIME` (a `not_before` stamp): the duty
leaves it untouched until due, then fires it. Acquisition is done
by the hook under the config flag and a planted demand — never
improvised by an agent.

**Liveness schedule**: every live lease carries a per-session
scheduler entry, armed and retired by the lease core's record
writers. `bin/colab-liveness <session>` checks the lease from
local files only — record against binding, deadline, pulse
verdict and freshness, approval-blockage age — and speaks on
change; it never dials, so it cannot mint an approval card.

## Session shape gate

Agent actors may create only High-RAM CPU sessions, and a start
is justified only by parallel compute — gates, censuses, builds,
drives; a data-analysis workload is not a justification. The
policy is the `session_gate` block in `config/lease.json`: read
fresh on every invocation, changed by editing the block, dead
only by an explicit `"enabled": false`; an absent block enforces
the built-in defaults (fail-closed).

- The gate sits on creation only: `acquire()`'s creation branch;
  demand write and consumption (`request` refuses a blocked
  demand at write time, and consumption clears a refused demand
  instead of retrying it); renewal re-creation, as a bare class
  check with no actor bypass; the session-creating wrappers,
  through the core's `bin/colab-lease admission` verdict; and the
  bridge agent's mint (kit install stamps the allowed shapes into
  the bridge config).
- A post-start assertion rides every fresh create: the local
  binding's endpoint host class (`m-hm-` / `m-s-`) is compared
  with the shape's declared RAM class. A proven mismatch is
  stopped via `stop_session_safe` and refused as
  `shape-mismatch` — no lease record is written. Unreadable or
  unparseable evidence never triggers the assertion.
- Adopting or reusing a live session, `extend`, and `release`
  are never gated.
- Actors are the pair (caller claim, `COLAB_LEASE_ACTOR` env
  claim); a bare caller tag is caller-asserted and never
  sufficient for a bypass. The panel bypasses only with both
  halves — the env claim the server sets on every core call, plus
  `--via panel` on the call. The standing duty is exempt only as
  the infra actor admitting exactly the configured standing
  shape. `COLAB_LEASE_ACTOR=test` is the whitelisted probe door
  for deliberate live probes of other shapes; every use is
  journaled as a bypass.
- Refusals, load-bearing bypasses, infra admissions,
  refused-demand dispositions, and shape mismatches are appended
  to `var/session-gate-journal.jsonl`. `state` serves the
  resolved policy as `session_gate` and each spec's
  `agent_startable`, so the panel renders the gate's effect from
  served data. Agent-class fresh starts are refused at every
  creation seam regardless of claim or shape
  (`caller-start-refused`, journaled): starts are the panel's,
  the hook duties', or a demand's — renewal, extension, release,
  adoption, and duty creation are lifecycle, not starts.

## Extension and renewal

Findings first: a lease's deadline is a window end, and an
extension moves the end — never the session. Renewal keeps a
window alive across the sessions it outlives.

- **Extend**: `bin/colab-lease extend SESSION --hours H` (panel:
  the lease card's Extend control) moves the deadline by H,
  priced by the budget guard on the delta only (rate × H). The
  record gains `renew: true` — it is now a window lease. A
  release already in flight loses to an extension: `release()`
  re-reads the record before stopping and aborts when the
  deadline has moved.
- **Extend guard**: a scheduler guard extends non-standing
  leases before they lapse, under the `extend_guard` policy in
  `config/lease.json`: when a lease comes within the policy
  threshold of its deadline, the guard extends it by the policy
  window. A per-shape / per-caller mode decides eligibility —
  `active-only`, `active-or-first`, `always`, `never` (caller map
  first, then shape, then the default; an unknown mode fails
  closed to `never` with a finding). Activity means registered
  jobs, an in-flight run record, or a `last_used_at` inside the
  policy's idle window — keepalive heartbeats are not activity.
  Idle extensions are capped per shape, warmest first, and an
  idle lease is extended at most once per idle episode, so an
  extended-then-idle lease can still lapse. Decision lines are
  records in `var/extend-guard-state.json`, which `state` serves;
  only refusal findings escalate.
- **The standing lease** refuses direct extension
  (`extend-standing-managed`) because the grant owns its
  lifetime; `extend-grant --hours H` moves the grant's `until`
  instead, pricing the standing lease's delta only.
- **Renewal** is a reaper branch for `renew: true`, non-standing
  records whose window is still open, fired when the session is
  confirmed absent or its age passes the platform threshold
  (platform cap 12 h − 30 min). Driver 1 (preferred, when the
  renewal kit is installed): `snippets/renewal.py` in the VM
  mints the child session at the threshold and writes a handover
  file; the reaper merges the child's binding into the local
  sessions file, swaps the record, and stops the parent once the
  child answers. Driver 2 (fallback): the reaper creates a fresh
  session against the same window end and swaps the record.
  Renewal never re-prices the window and preserves deadline,
  jobs, caller, and shape; a failed renewal retries next cycle,
  and a window that ends first releases normally. Unflagged
  records are never renewed.
- **Announcements**: extensions and renewals are spooled by the
  core to `var/reaper-events.jsonl` — `lease-extended` {session,
  hours, old_deadline, new_deadline}, `grant-extended` {hours,
  old_until, new_until}, `lease-renewed` {shape, driver,
  old_session, new_session} — and the UI server, the outbox's
  single writer, drains the spool into `var/ui-outbox.jsonl` on
  its state/outbox reads (byte-offset marker in
  `var/reaper-events.drained`; only complete lines are consumed,
  so a writer mid-append is never half-read). The notify hook
  announces each event to the main chat.

### Shortening

Findings first: shorten is extend's mirror, not a soft release —
the session keeps running, only the deadline moves.

`bin/colab-lease shorten SESSION (--deadline ISO | --hours H)`
pulls a lease's deadline earlier; the panel action rides the
same verb. The target must be strictly earlier than the current
deadline and strictly in the future: a target at or beyond the
deadline is extend's case (`shorten-not-earlier`), and a target
at or before now refuses (`shorten-deadline-past`) — ending a
session now is release's job. Standing-managed leases refuse, as
they do for extend: the duty owns those deadlines. Shortening
prices nothing — it only reduces the committed projection. The
session classification is re-checked immediately before the
save, under the same absent/unknown discipline as extend. When
the record carries a renewal kit, the kit is reinstalled for
the new deadline; a kit failure is reported in the verdict and
the move stands.

A successful shorten stamps the record (`shortened_at`,
`shortened_by`, `shortened_from`). While the stamp stands, the
extend guard skips the lease (`skipped-shortened`) — the guard
never silently undoes a deliberate shorten. An operator
extension clears the stamp in its own save: the operator's
latest action wins, so shorten-then-extend is an ordinary
sequence, and the guard resumes its policy afterwards.

## The two-tier envelope

Findings first: the grant is the only tap. Everything inside its
scope afterwards is script-level machinery and served state.

- **The grant is an authorization envelope.** Starting a grant
  (`var/grant.json`) records its scope: the sanctioned shapes and
  a snapshot of the budget caps in force at grant time. The
  grant's `granted_at` is the envelope id. `extend-grant` moves
  only the window, never the scope. The panel's cap editor
  therefore shapes future grants and ordinary leases — never a
  running envelope.
- **Inside the scope**, acquires price against the snapshot caps
  (the standing duty included, through `acquire`), and created or
  adopted records are stamped with `envelope_id` and the
  requester caller. Outside the scope, a lease is an ordinary
  lease: live config caps, neither stamp.
- **The standing duty** keeps the standing lease up while a
  grant is active and releases it at expiry, inside a standing
  daily sub-cap.
- **Bursts start by demand, never by agent-fired commands**:
  `bin/colab-lease request --shape SHAPE --caller TAG` plants
  the demand (`--via` is an accepted alias for the same claim);
  the hook's auto duty creates the lease and copies the caller
  onto its record. A timed start is the same record with `--at
  ISO-TIME`. Panel starts stamp the panel caller and are
  creations, never adoptions (`acquire --create`): starting
  mints a parallel lease at the shape; using the current lease
  is the extend path. The job runner and the standing duty stamp
  their own callers.
- **Direct starts are the burst path.** A brokered start from
  inside the standing lease is not faster, and a brokered
  session is born locally unbound until its binding is
  transplanted — so bursts start through the hook machinery, and
  stops go through the lease core's `stop_session_safe`, never
  the `colab stop -s` flag form.
- **Tracking**: the envelope lifecycle is ledgered in
  `var/grant-ledger.jsonl` (grant started, lease attributed,
  renewed, released, grant ended), and `state` serves an
  `envelope` block — scope, attributed live records, spend
  versus the snapshot caps — which the lease panel's grant card
  renders. Envelope lease starts/releases also ride the
  reaper-events spool into the UI outbox as
  `envelope-lease-started` / `envelope-lease-released`.
- **The standing bridge**: the standing lease also hosts the
  bridge agent (`snippets/bridge_agent.py`, kit at
  `/content/bridge-kit`, runtime at `/content/bridge`) — a
  hash-signed command queue in the standing lease, executed there
  or relayed into bridge-minted children (reserved `bridge-`
  namespace, cap from config `bridge.max_children`), with
  outcomes in a segmented log the standing duty's ferry mirrors
  to `var/bridge-mirror.jsonl`; `var/bridge-outbox/` is the
  file-only driving surface (unacknowledged commands re-plant,
  acknowledged ones drop), and `bin/colab-bridge` is the CLI
  surface. A command file carries a SHA-256 over its canonical
  JSON, so a partially planted file never executes; a command id
  executes at most once per bridge generation; a command is
  taken at most twice, so a self-killing command quarantines
  instead of looping. Children hold no lease record, so their
  stop lines fold into the attributed spend journal. The bridge
  dies with the grant; the mirror is the record of what ran.

## Budget guard and attributed spend

The budget guard prices every window before it is promised —
acquire, extend, and the standing duty all pass through it
(`budget_check` in `sync/lease/spend.py`). Caps live in
`config/lease.json` (daily, monthly, and the standing daily
sub-cap); a grant in scope replaces them with its snapshot caps
for the check.

- **Projection**: a window projects rate × hours at its shape's
  `rate_cu_per_hour`. A shape whose rate is null has an
  unconfirmed rate: no projection is fabricated — the check
  passes with `rate_unconfirmed` and a null projection.
- **Committed projection**: every other live lease's remaining
  window (rate × time to its deadline) is already committed
  burn, added to the projection being priced — concurrent
  windows are priced as the sum they commit, so two windows must
  not each pass a guard their sum would fail. The other leases'
  elapsed burn is already inside day/month spend via their live
  records.
- **The checks**, in order: the balance floor — a committed sum
  above the account balance refuses `balance-insufficient` (the
  balance is a floor, never a spend figure); the daily cap; the
  monthly cap; and, for standing leases only, the standing daily
  sub-cap against the standing caller's own day spend
  (`standing-budget`). A failed or stalled usage read refuses
  `usage-unavailable`: the guard never prices a window without a
  balance.
- **Day clipping**: the daily comparison clips other leases'
  committed windows at the end of the current UTC day — the same
  clipping day spend gets — so a multi-day window charges only
  today's portion against today's cap, and each later day
  re-prices as it arrives. A daily refusal reports the clipped
  figure as `committed_projected` and keeps the unclipped sum in
  the verdict as `committed_full`. The balance floor and the
  monthly check keep the full windows.
- **Attributed spend**: day/month figures count only this repo's
  own leases. Completed assignments are journaled as segments to
  `var/lease-spend-journal.jsonl` at release, renewal swap, and
  confirmed-dead clear; live records contribute their running
  segment. Figures split at the period boundaries, and the
  numbers the guard enforces and the panel serves are the same
  derivation. External Colab usage and credit expirations move
  the balance; they never enter the spend figures.
- **Limits, stated plainly**: a segment closed by a
  confirmed-dead clear ends at the reap that detected the death,
  overcounting by at most the detection lag; an assignment with
  no record burns uncounted (the orphan sweep is its cure); an
  unconfirmed-rate shape contributes nothing rather than a
  fabricated number.

## Callbacks — how results and failures come back

- **Run records** (`results/runs/<run-id>.json`) are the callback
  substrate: every state change lands there, and a resume reuses
  the run's original record — `started_at` is the run's birth,
  not the latest attempt's. The runner's orphan sweep reconciles
  non-terminal records against live sessions on each entry:
  records co-tenanted on a live session are untouched, and runs
  whose session is gone are marked orphaned together. Sweep
  findings print to stderr, never into the runner's JSON result.
- **Job failure** (non-zero exit, gate red): results and logs are
  pulled, the run record names the failing step, and the lease is
  unaffected.
- **Transfer failure** (hash mismatch, part failure): the chunked
  path retries each part and resumes per part by SHA-256 —
  re-running the transfer moves only the missing or corrupt
  parts.
- **Session death**: only a confirmed-absent reading counts (see
  Edge cases). In-flight run records stay non-terminal until the
  orphan sweep reconciles them against the assignment list.

## Data movement

Data moves are decided per class by the data manifest
(`sync/data_manifest.py`): transfer the artifact, transfer its
driver and regenerate, or regenerate from the network (8 rps,
declared user agent passed via `--env`) — priced, with the route
recorded.

- **Snapshot bundles for code**: origin-lineage only (parent =
  current origin tip, tree = the tree being shipped), byte size
  and SHA-256 recorded before transfer and verified after,
  applied by `git fetch` + `merge --ff-only`. Never a bundle on
  the local lineage.
- **Metadata snapshots**: always transfer, never refetch —
  refetch is not identity-preserving. Derived artifacts
  regenerate on the VM from the transferred snapshot and verify
  by snapshot identity.
- **Working sets**: bulk-transfer only the named set; the VM
  fetches new material under its own rate window.
- **File channel**: uploads always use `/content`-absolute
  remote paths (a relative path lands at the VM root).
  Single-file transfers are reliable to ~50 MB; anything above
  moves as 32 MB chunks via `sync/transfer.py`, reassembled and
  SHA-256-verified on the far side, each part retried (3
  attempts) and resumable in both directions. Tunnel throughput
  is ~1.6 MB/s — split or shrink the payload when the window
  matters.
- Every transfer phase is bounded and named: the part-hash
  resume probe (240 s, its timeout caught — no resume
  information just moves everything), the reassemble/verify
  and the download split ride the same 3-attempt discipline as
  the parts, and a phase that exhausts its attempts returns a
  failure carrying its stage (`verify (reassemble)`, `remote
  split`, `upload part N`…) — a stall is distinguishable from
  a slow transfer at the call site, and no timeout can strand
  a transfer record at "running".
- `colab-fs` reports only verified landings: a write/patch/
  delete exits 0 only when the payload's verdict says ok —
  for a write, only when the verdict's `after_sha256` equals
  the local content's hash. A refused attach produces no
  verdict (the gate answers refusals rather than failing
  them), and no verdict means the op did not land: exit 1
  with the non-landing named, never a silent success.

## Orphaned sessions and adoption

Findings first: adoption restores a record from evidence; it
never invents one. A live session with no record — a torn or
lost `var/lease-<session>.json` — is an orphan: it bills, but
no deadline is enforced, no jobs list protects it, and its burn
is unattributed. The census is served as the `orphans` portion
of `state`: live sessions minus recorded ones, bridge-prefixed
sessions excluded, a handover child flagged rather than
offered.

The derivation reads the strongest evidence first: (1)
`var/renewal-config-<session>.json`, a verbatim copy of the
record written when the renewal kit was installed — deadline,
acquired_at, shape, and spec; (2) any later `lease-extended`
event in the UI outbox applied over it, since extension does
not rewrite the config. When the last extension event carries
no usable new deadline, the derived value is served flagged
stale-possible. A third source rides under both:
`var/session-evidence-<session>.json`, the record's own
{deadline, acquired_at, shape, spec, caller} projected at
every deadline-moving lifecycle write — it is the derivation's
base when no renewal config exists, and its deadline joins the
conflict values otherwise. Caller claims, liveness state, and run records
carry caller and timing evidence only — never a deadline. The
record's caller is the evidenced creator, not the earliest
claim: a claim is a ride, and the operator's own differing
caller is journaled as an `adopt` claim. Shape is evidenced by
the config's spec where present, else resolved from the live
list's shape class against the catalog; when that leaves
several candidates the operator picks one (`--shape`, chosen
in the panel from the served specs) — derivation never guesses,
and a `--shape` that contradicts the evidenced shape refuses
with the conflict shown.

A record file that cannot be read at all is a torn record:
`state` serves it under `torn_records` by its filename-derived
session, readers never rename it, and the first writer that
needs the path quarantines it to `<name>.torn-<timestamp>`
with its bytes preserved — the write then proceeds onto a
clean path, and adoption over a torn file stays possible.

`bin/colab-lease adopt SESSION` plants the derived deadline
verbatim, or the operator's `--deadline`/`--hours` target.
Restoring the evidenced window is exempt from budget pricing —
it creates no new commitment; only the excess beyond the
evidenced deadline prices, as an extension delta does.
Planting earlier than the evidenced deadline refuses
(`adopt-deadline-shortens`): adoption is a restore, not an
edit — the operator adopts at the evidenced deadline and
shortens explicitly afterwards if that is the intent. A
derived or planted deadline at or before now refuses rather
than writing a record the next reap pass would release. The
session's classification is re-checked immediately before the
save (absent and unknown both refuse), as is the record's
continued absence. The restored record carries the evidenced
`acquired_at`, so attributed spend counts the real elapsed
burn; the jobs list seeded from non-terminal run records and
live detached-launch records (`detached:<id>`) bound to the
session; and the provenance keys `adopted`,
`adopted_at`, and `deadline_source`. The panel's orphan
section renders only this served state — derived value,
source, and conflicts — and its adopt action posts to the
same verb.

## Edge cases

- **Lease record loss is not session death**: a "no-lease"
  reading is a bookkeeping event, not a death. Before declaring
  death, confirm absence against the server assignment list (the
  truth); an unreadable sessions list reads as UNKNOWN, never as
  death. Reap clears a record only on confirmed absence; release
  keeps the record when a stop fails. Test suites run isolated
  from live state (a scratch `COLAB_RUNNER_VAR` and home), so a
  green suite never writes a real record.
- **Local-machine restart**: transient state restores from the
  repo mirror via the reaper hook and the wrappers; the lease
  records are in `var/` (untracked), with the server assignment
  list as the fallback truth.
- **Clock discipline**: the deadline is UTC in the lease record.
  All scheduling is computed from the record, never from memory
  of it.
- **Cost**: compute units burn while a session is assigned, idle
  or not; the budget caps and the hard deadline are the controls.
  Cost is never a reason to stop a session early — the reaper
  owns the end.

## Panel server supervision

The lease panel's companion server (`ui/server.py` on
127.0.0.1:8793) conforms to the **VM-restart survival pattern
contract v1** (canonical text: the spec-driven-starter SPEC;
conformance landed 2026-11-08 under the survival-pattern plan's
Step 3). The unit's source is the tracked template
`ui/colab-lease-ui.service.tmpl`, rendered by
`ui/install-unit.sh` into the system instance and enabled;
`Restart=always` guards it within a boot. The reaper hook's UI
duty drives `ui/watchdog.sh` every pass (~120 s): the watchdog
probes the page (a whole-operation-deadline read), checks the
unit's presence and render currency beside the health check —
a missing unit is reinstalled, a unit not byte-equal to the
current render is re-rendered, both quietly while the server
answers — and when the page does not answer it runs the ladder:
install, then the load-state verb (a loaded unit is restarted,
anything else started), poll-verified, with the pre-systemd
`ui.sh` start as the last rung. A failed ladder wakes; a
successful restore is silent. Proof: the 2026-11-08 19:14 EST
VM replacement restored the panel unattended in ~2¾ minutes,
and an adoption drill the same evening (unit removed, service
stopped) is recorded in the repo's run logs.

The server also conforms to the **stack-dump contract**
(`stackdump-contract: v1`; canonical text: the
spec-driven-starter SPEC). An in-process watchdog
(`ui/stackdump.py`) checks the state computation the reads
serve — `run_core`'s state path, called directly, never an
HTTP self-call — on the cadence `ui/config.json`'s
`stackdump_*` keys set (defaults 5 s cadence, 15 s threshold,
60 s repeat, 16 MiB aggregate cap), and on a lag dumps every
Python thread's stack to `stackdumps.log` in the server's var
dir (`COLAB_RUNNER_VAR`; the repo's `var/` in the live
deployment) through a handle opened at startup. The dump is
diagnostic only — recovery stays with the ladder above. Every
`/api/state` response carries `stackdump_last_at`, the last
dump's timestamp or null, so a watcher can tell "wedged and
dumped" from "wedged silently" without reading the dump.
