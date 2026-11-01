# The lobby bridge: a command/output relay through the standing session

Status: landed 2026-11-01 (commits: bridge build + closeout) — built the same day on the operator's go, and live-proven in the Step 7 grant lobby (generation e4c35deb). Drill record: happy path (self command, 3.9 s mint, 3.5 s child relay), duplicate plant (answered as a duplicate, executed once), poison (exit-3 and timeout lines, agent unharmed), agent kill (re-armed by the duty within a pass, sequence and registry resumed), cap (fourth mint refused by name), and the accounting check (the stopped child's segment landed in the spend journal) all passed live. The generation-change drill was deliberately not run live — the lobby was the operator's live grant asset — and the ferry-stall drill is evidenced by catch-up across this session's pass gaps rather than a formal pause; both mechanisms are unit-tested. Open questions resolved as recommended below: the ferry folds into the standing pass (it rides `bridge_duty` inside `bin/colab-lease standing`); children are bridge-owned only in v1.

## Objective

Use the standing lobby as a bridge into Colab: threads and scheduled jobs on this side run OS commands — in the lobby, or in burst sessions the lobby mints and relays into — without agent-fired exec calls, and read the outputs back from a log the bridge keeps. The bridge is asynchronous by design (seconds, not interactive); interactive work keeps using the existing channels (`colab exec`, `bin/colab-sh`).

## Probe evidence (2026-11-01, live, lobby under the Step 7 grant)

Probe snippet: `snippets/bridge_probe.py` (a detached watcher in the lobby polling a queue directory, executing planted command files, appending JSON result lines to `bridge.log`). Beachhead: the broker probe's in-VM CLI venv + canonical token, reused.

What works:

- **Queue → execute → log → pickup, end to end.** A command file planted by upload was picked up in ~1–2 s (2 s poll cadence), executed as an OS command in the lobby (0.03 s for a probe command), and its output read back here from the downloaded log.
- **Relay into a broker-minted child.** The lobby minted a high-RAM child in 4.0 s; a command relayed into it through the in-VM CLI's exec returned the child's own hostname/resources (52 GB RAM shape) in 3.06 s total, captured into the same log.
- **Detached persistence.** The watcher survived across exec calls, heartbeating into the log every 15 s; log downloads taken mid-append were consistent.
- **In-VM stop, correct form.** `colab stop -s <name>` (no `--yes`) terminated the child cleanly from inside the lobby, rc 0.
- **The beachhead is cheap and reusable.** Token placement + CLI venv ≈ 30 s once per lobby session (broker probe), then mints at direct-start speed.

What does not work, or costs:

- **`colab stop --yes` and the positional form both fail (rc 2).** Only `stop -s <name>` works. This corrects the Step 6 record: the "unreliable in-VM stop" was this flag bug — it poisoned the broker probe's own child stop and two manual attempts, and the one orphan it caused is the standing evidence of its cost. Stops use the `-s` form in-VM and `stop_session_safe` from here.
- **Uploads 500 when the remote parent directory does not exist** — directories are created before any upload, always.
- **The file/exec channels stall intermittently** on the egress proxy (long-diagnosed). Every bridge read/write needs a bound and a retry on the next cycle; a stall degrades freshness, never correctness.
- **Brokered children are born locally unbound.** From here they are unaddressable until a binding transplant; from the lobby they are addressable immediately — which is the bridge's reason to exist (this side talks only to the lobby).
- **Port forwarding through the SSH bridge passes no traffic** (measured 2026-10-31) — not a transport. `colab run` must never receive a live session's name (it removes the binding and orphans the assignment).
- **The bridge is mortal.** It lives in the lobby's `/content`: grant expiry, session death, or a runtime recycle takes the watcher, the beachhead, and the log with it. Setup must be idempotent and re-armable, and a missing heartbeat must read as "bridge down", not as an error.
- **The token sits on the lobby VM** while the beachhead is armed — the same accepted exposure as renewal driver 1 (ephemeral, single-user), stated so the design carries it knowingly.
- **Latency floor.** Queue poll + file-channel round trips put a command's round trip at ~2–5 s. The bridge is a relay, not a shell.

## Design

Components:

1. **Bridge agent** (in-VM, evolved from the probe watcher): queue directory with per-command files and `.done` acknowledgement, an append-only JSON log (results with bounded output tails, mint/stop events, heartbeats), a child registry (name, shape, minted_at, last-used), and child hygiene: idle timeout plus a hard cap, after which the agent stops a child and logs it. Verbs arrive as command files: `run` (target self or a child), `mint` (profile/shape → child name), `stop` (child), `ping`. Commands execute in isolated subprocesses (never in the agent's own loop), each with its own timeout; a long command runs detached under the runner's pattern and its completion appends its result when it lands. The agent also pulses each live child on an interval (a trivial relayed exec), because children carry no lease keepalive of their own.
2. **Beachhead install in the lease core**: standing-acquire installs the bridge kit (agent + CLI venv + token placement, the renewal kit's proven pattern) into the lobby idempotently and records a ready marker carrying the protocol version and a bridge generation id (one per lobby `/content` lifetime); `state` serves bridge status (armed, last heartbeat age, children). Re-arm on heartbeat loss is a reaper duty, bounded like the keepalive's.
3. **Local client** `bin/colab-bridge`: `cmd <target> <shell…>` (plant, wait bounded for the matching result line), `log [--tail N]`, `mint <profile>`, `stop <name>`, `status`. All channel calls bounded; failures return a named state, never a traceback. The client refuses a protocol-version mismatch with the armed agent as a named state.
4. **The ferry** (the operator's cron suggestion): a scheduled job on an interval that uploads queued commands from a local outbox directory and appends new log lines to a local mirror file — so a thread can drive the bridge with plain local file reads and writes alone, no CLI calls in its hot path, and outputs are readable here as ordinary files. The outbox is the source of truth: a command leaves it only when its result line reaches the mirror; anything unacknowledged is re-planted after a re-arm or a generation change. The mirror tracks (generation id, byte offset); a generation change restarts the mirror segment cleanly instead of corrupting it.
5. **Ownership rules**: bridge-minted children are bridge-owned and reaped by the agent, and live in a reserved `bridge-` name prefix so they can never collide with lease session names. Lease-record sessions are never bridge targets and never bridge-stopped; relay into a lease session goes through `bin/colab-run`'s existing job pattern, not the bridge. A child that graduates to real work is adopted into a lease record through the existing transplant/handover pattern, at which point the lease owns its lifecycle.
6. **Accounting fold-in**: bridge children burn CU with no lease record, so the attributed accounting would miss them. The agent's mint/stop log lines carry shape and timestamps; the ferry folds each completed child segment into the spend journal (the attributed-accounting seam) as it mirrors the log, and live children appear in bridge status with their ages — bridge burn is counted and visible, never a surprise the balance floor has to catch.

## Steps

1. Harden the probe watcher into the bridge agent snippet (stop-form fix, child registry + reaping, bounded output tails); local tests for the command/log protocol parsing.
2. Lease-core beachhead install at standing-acquire + bridge status in `state`; fake-suite coverage.
3. `bin/colab-bridge` client verbs over the funnel; live smoke against a grant lobby.
4. The ferry job (outbox upload + log mirror on an interval) with failure surfacing through the existing notify pattern.
5. Docs (`docs/lease-workflow.md`, `AGENTS.md`) and the live proof: through the client alone — mint a burst child via the bridge, run a real command chain in it, read the output here, stop it.

## Edge cases

The bridge inherits every edge case the non-bridge system already has, and adds its own. Both classes are designed for here; the live-test checklist at the end drills the ones that can be drilled.

### Inherited — the bridge changes nothing, and leans on the existing mechanisms

- **Idle death.** The lobby is a lease: keepalive + kernel pulse already cover it. Children have no lease coverage, so the agent pulses them itself (Design 1); a child that still dies is a registry event, not a silent loss — the next relay's failure surfaces it, and the agent confirms absence before marking it gone (the tri-state rule applies in-VM exactly as here: unknown is never dead).
- **The 12 h session cap and lobby turnover.** A grant longer than a session cap, or a lobby death mid-grant, has the standing duty re-acquire the lobby; the bridge on the new session is a new generation: re-armed by the duty, its queue empty, its log fresh. Unacknowledged outbox commands are re-planted by the ferry (Design 4) — a command can therefore execute twice across a generation boundary only if its first result never reached the mirror; ids make the duplicate detectable, and the mirror records both attempts under the one id.
- **Grant expiry mid-command.** The lobby's release at expiry kills the agent and any running command with it. Commands carry timeouts well inside sane bounds; anything longer belongs on a lease under the runner, not the bridge. The mirror keeps whatever result lines landed before the end; an unacknowledged command reads as never-run, and its owner decides whether to re-issue it elsewhere.
- **Channel stalls.** Bounded calls everywhere, retry next cycle; a stall costs freshness, never correctness. Bridge status shows both heartbeat age and last-ferry age, so a stalled bridge and a stalled ferry are distinguishable at a glance.
- **Auth failure mid-window.** In-VM, the CLI cannot complete an interactive login and must never try: a mint or relay that hits an auth error is logged once as `auth-failed`, the bridge reports down-with-reason, and the agent does not retry-loop against a dead credential. From here, the existing guard funnel and `auth-required` surfacing apply unchanged.
- **Budget.** Bridge mints bypass `acquire()`'s guard, so the bridge carries its own bound: a config cap on live bridge children (count) plus the agent's idle reaping and hard caps, and the accounting fold-in (Design 6) so the guard's day/month figures stay honest. The balance floor still backstops everything.
- **Orphan assignments.** The server-side sessions list is truth, as always. A bridge child orphaned by a lobby recycle is identifiable as bridge-born from the mirrored log's mint records and released by the existing orphan path; it is never mistaken for a lease.
- **This side restarting.** A VM replacement wipes the local mirror offsets and any in-flight ferry state; the mirror rebuilds from the live generation's log, the outbox re-plants what is unacknowledged, and hooks/crons recover under their own rules. The Step 7 poller stall (~11 min across a restart) is the calibration: the design assumes missed cycles happen and loses nothing by them.
- **Clock skew.** Log timestamps are guest-clock and labelled by generation; latency math stays inside one clock's domain (dispatch measured in-VM, round trip measured here), never across the seam.

### Bridge-specific

- **Partially planted commands.** An upload interrupted mid-write must never execute half a command. Command files carry a content hash; a file that fails to parse or hash is deferred one cycle, then quarantined as `.bad` with a log line. Execution is whole-file or nothing.
- **Duplicate delivery.** Client retries and ferry re-plants can deliver the same command twice within a generation. The agent keeps a processed-id registry in the bridge directory: an id executes at most once per generation, and a re-plant of a completed id is answered from the log, not re-run.
- **Poison and runaway commands.** Each command runs in an isolated subprocess with a timeout, so a crashing command is a result line, not a dead agent. A command that kills the agent itself (or fills the lobby's disk — outputs are tail-capped, result files size-capped, the log rotated at a cap with generations kept) shows up as heartbeat loss and is re-armed by the duty; the queue on disk survives the agent, so a re-arm resumes where the log ends. Commands are the operator's own — this is robustness, not a security boundary.
- **Long commands.** Past the per-command timeout, `run` detaches under the runner's pattern: the log records a job handle, and the result line lands on completion. The queue never blocks behind a long job; per-target ordering is preserved only for commands that ask for it (a `serial` flag), because strict ordering and non-blocking are a tradeoff the caller should choose.
- **Registry drift.** A child stopped from elsewhere, or adopted into a lease, disagrees with the agent's registry. The agent reconciles on each mint/stop/relay failure against the in-VM sessions list; adoption removes the child from the registry by an explicit command verb, so the agent never reaps a session the lease now owns.
- **Two writers.** Threads plant concurrently by writing distinct files with writer-scoped ids; nothing in the protocol mutates a shared file from two sides. Results route by id, so concurrent callers cannot read each other's outputs unless they go looking in the shared log — which is a feature for debugging and stated as such.
- **Version skew.** The ready marker carries the protocol version; a client that meets a different version refuses with a named state. Upgrading the agent is a re-arm, not a migration: the queue format is per-generation.
- **The `colab run` trap.** The bridge never routes through `colab run` for a named live session (it removes the local binding and orphans the assignment — the repo's standing rule). Relay uses exec; jobs use the detached pattern.

## Tradeoff: the bridge vs a long high-RAM lease

The alternative, whenever the bridge looks attractive, is simply holding one long high-RAM lease and running everything in it. The honest comparison:

- **Cost.** A 12 h high-RAM lease is 3.12 CU, paid whether it works or idles. The bridge's lobby costs 0.96 CU for the same 12 h, plus 0.26 CU per burst-hour actually used: break-even is ≈ 8.3 burst-hours in a 12 h window. Below that, the bridge is cheaper; above it, the lease is cheaper *and* simpler.
- **Latency and feel.** The lease is immediate and interactive. The bridge adds ~2–5 s per command and is asynchronous only; a workflow of hundreds of small interactive steps feels it on every step.
- **Capability.** The bridge fans out (several children, different shapes, driven by file from any thread or cron, tolerant of this side's agents restarting). The lease is one big room: everything shares one session's fate, and driving it is the runner's job pattern — proven, but one workload at a time per lease.
- **Risk surface.** The lease's risks are the known lease risks. The bridge adds: token on the lobby VM, agent mortality, a queue/log protocol — each mitigated above, none free.
- **Rule of thumb.** A sustained single workload (roughly > 8 high-RAM hours a day, or interactive) → long lease. Bursty, parallel, or cron/thread-driven work with idle gaps → bridge. The hybrid is built into the design: a bridge child that turns sustained gets adopted into a lease, and the bridge keeps the rest.

## Live-test checklist (for the future session that tests this)

Run under a fresh grant, through the client and ferry only (no agent execs):

1. Happy path: mint a high-RAM child via the bridge, run OS commands in the lobby and relayed into the child, read outputs from the local mirror; stop the child; verify the spend journal gained the child's segment.
2. Duplicate plant: plant the same command id twice; verify one execution, two result references, one id.
3. Poison command: a command that exits non-zero, one that overruns its timeout, one that produces output past the cap; verify result lines, caps, and a live agent heartbeat throughout.
4. Agent kill: kill the watcher mid-queue; verify heartbeat loss in status, re-arm by the duty, and queue resumption with no command lost or doubled.
5. Generation change: stop the lobby session under the bridge; verify the standing duty's new lobby re-arms a new generation, the ferry re-plants unacknowledged commands, and the mirror marks the boundary.
6. Ferry stall: pause the ferry for several cycles; verify status distinguishes bridge-up/ferry-stale, and the catch-up sync loses nothing.
7. Cap drill: mint children to the bridge cap; verify the next mint refuses by name, and idle reaping frees a slot on schedule.

## Open questions

- Does the ferry run as a hook (event-shaped) or a cron (interval-shaped)? The sync is interval work; a cron at the reaper's cadence is the natural fit, but it duplicates the reaper's poll — folding the ferry into the reaper hook's duties is the alternative. Recommendation: fold into the reaper pass (one poller, one stall surface), unless the operator wants the bridge usable with the reaper paused.
- Are bridge children ever worth adopting into lease records, or is bridge-ownership (with its idle reaping) the whole lifecycle? Recommendation: bridge-ownership only for v1; adoption is a later step if a real workflow asks for it.
