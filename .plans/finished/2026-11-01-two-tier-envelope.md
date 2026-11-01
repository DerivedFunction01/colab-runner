# Two-tier envelope: one-tap authorization, tap-free execution, tracked lifecycle

Status: complete — Steps 1–5 landed 2026-11-01 (full suite green, companion deployed); Step 6 probed live and settled the warm runner on evidence (not built — direct starts stand); Step 7's live proof ran the same day under a 12 h grant (lobby raised by the hook, a `--via` demand burst acquired and released, ledger and journal checked). Both open questions were implemented as the steps specify (snapshot-only caps per Step 3; out-of-scope demand runs as an ordinary lease with neither stamp per Steps 2–3) — the refuse variant for out-of-scope demand remains a small followup change if the operator rules it.

## Objective

Evolve the landed two-tier grant (`.plans/2026-10-31-two-tier-standing-lease.md`) into an authorization envelope: starting a grant is the operator's single tap and records the scope it authorizes; every lease action inside that scope then runs through the script-level machinery with no further operator interaction, each action attributed to the envelope in a lifecycle ledger the backend serves and the lease panel renders. Ends when a live grant carries a demand-started burst lease to completion with zero operator taps after the grant tap, with the envelope's scope, leases, and spend visible on the grant card.

## Rulings

- The grant is the authorization act. Its record carries the scope it sanctions: window, lobby and burst profiles, and a snapshot of the budget caps (daily, monthly, standing daily) taken at grant time.
- Inside an active envelope, lease lifecycle work — burst acquires, renewals, releases at expiry — runs only through the script-level machinery (reaper hook duties, the runner's acquire-or-adopt, the in-VM renewal kit). No step of it is fired by an agent shell; an agent-fired start is what produced approval cards during the 2026-11-01 endurance start.
- Timed starts are data, not agent jobs: a demand request may carry a `not_before` time and the auto duty consumes it when it comes due.
- The lobby tier is the envelope's beachhead. It may broker session starts and pipe files to sessions started within Colab (the proven in-VM path), but any session under the envelope may bypass the beachhead and connect directly when that is better. Tap-freedom is the invariant; topology is an implementation choice settled by measurement (Step 6).
- The envelope's lifecycle is a backend ledger: grant start and end, every attributed lease (profile, window, requester), renewals, and spend against the snapshot caps. The panel renders only what `state` serves; the tracking surface is the lease panel's grant card (inference: the view the grant already renders on).
- Work outside an envelope keeps today's behavior exactly: panel starts, per-job leases, the config budget guard.
- Implementation tracks the steps as workbench bundle todos, one per step; the harness arms the progress checks, so no separate status interval is set.

## Steps

### Step 1 — Envelope scope and identity on the grant record

`write_grant` in `sync/lease.py` adds a `scope` block to the grant record: the budget caps snapshot from `config/lease.json` at grant time, plus the lobby and burst profiles it sanctions; the grant's `granted_at` serves as its id. `extend_grant` moves only the window, never the scope. `state()` serves the scope with the grant. `bin/colab-lease` output shapes stay additive (new keys only).

files: `sync/lease.py`, `bin/colab-lease`, `tests/test_two_tier_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_two_tier_unit.py` — new tests pin the snapshot (config edits after grant start do not move the recorded scope) and extend-window-only.
done when: a written grant record shows `scope` with the caps in force at its start, and the suite passes.

### Step 2 — Attributed acquires and requester tags

`acquire()` stamps a created or adopted record with the active grant's id when the profile is inside the grant's scope; renewals preserve the stamp (the renewal record-merge keeps unknown keys, verified at implementation). The demand seam learns who is asking: `bin/colab-lease request` gains `--via <tag>` stored on the request, and `auto_acquire` copies it onto the record it creates. Panel- and runner-started leases stamp `via` as `panel` / `runner`.

files: `sync/lease.py`, `bin/colab-lease`, `tests/test_lease_unit.py`, `tests/test_two_tier_fake.py`
verify: `~/.local/bin/uvx pytest tests/test_lease_unit.py tests/test_two_tier_fake.py` — a demand-started burst under a grant carries the grant id and the `--via` tag; an out-of-scope profile carries neither.
done when: lease records started under a grant are mechanically distinguishable from ordinary leases by their stamps.

### Step 3 — Envelope budget semantics

`budget_check` gains an optional caps override; when a grant is active and the profile is in scope, `acquire` and the standing duty price against the envelope's snapshot caps (daily, monthly, standing daily) instead of the live config caps. Out-of-scope and no-grant pricing is byte-identical to today. The panel's cap editor therefore shapes future grants and ordinary leases, never a running envelope.

files: `sync/lease.py`, `tests/test_lease_unit.py`, `tests/test_two_tier_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_lease_unit.py tests/test_two_tier_unit.py` — an in-scope acquire that the live caps would refuse passes under a larger snapshot, and the reverse refuses; no-grant cases unchanged.
done when: the caps an envelope enforces are exactly the caps its grant tap authorized.

### Step 4 — Tap-free timed starts

The demand request record gains an optional `not_before` timestamp; `auto_acquire` leaves a not-yet-due request untouched (neither consumed nor failed) and consumes it when due. `bin/colab-lease request --at <ISO time>` writes it. This replaces agent-fired timed starts (the 2026-11-01 endurance start) with a hook-consumed record.

files: `sync/lease.py`, `bin/colab-lease`, `tests/test_lease_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_lease_unit.py` — a future-dated request survives an auto pass untouched, then acquires on the first pass after its time; existing demand tests unchanged.
done when: a timed lease start can be registered as data and fires with no agent and no operator present.

### Step 5 — Envelope ledger and viewer tracking

The core appends envelope events to `var/grant-ledger.jsonl` (grant started with scope, lease attributed, lease renewed, lease released, grant ended), written at the same seams that already spool reaper events. `state()` serves an `envelope` block: the grant, its scope, the attributed live records, and spend versus the snapshot caps, derived backend-side from the per-profile spend baselines over the stamped profiles (the derivation's known limit — a stamped profile also used by non-envelope work blurs attribution — is stated in the block's docstring). The panel's grant card renders the envelope block: scope, child leases with requester tags, spend versus caps. New outbox kinds (`envelope-lease-started`, `envelope-lease-released`) ride the existing reaper-events drain in `ui/server.py`.

files: `sync/lease.py`, `ui/server.py`, `ui/static/app.js`, `tests/test_two_tier_fake.py`, `tests/test_ui_server.py`
verify: `~/.local/bin/uvx pytest tests/test_two_tier_fake.py tests/test_ui_server.py` — the stub contract grows the envelope block and the card renders it; ledger lines match the lifecycle of a fake grant end to end.
done when: the grant card shows the envelope's scope, its live leases with requesters, and its spend, all from served state.

### Step 6 — Beachhead broker: measure first, build only on evidence

Probe, then decide: time a direct burst start (today's `acquire`) against an in-VM brokered start, where the standing lobby's kit starts the burst session from inside Colab and hands it the warm bundle — the nested-session path the renewal kit already proves — across several starts each, recording latency and failure modes in the plan's probe style (the still-owed P-T3 promotion probe folds in here). If the brokered path wins on the recorded evidence, build it as the envelope's default burst path with direct starts as the bypass; if it does not, the step closes with bypass as the standing answer and the evidence recorded. Either way the warm runner deferred from the two-tier build (its `warm: pending` marker) is settled by this step's outcome.

files: `sync/lease.py`, `snippets/renewal.py`, `tests/test_two_tier_fake.py`
verify: unverifiable — the step's product is live probe evidence and a recorded decision, not a test artifact; the decision and numbers land in this plan's successor note in `docs/lease-workflow.md`.
done when: the broker-versus-bypass decision is recorded with its measurements, and any build it justifies is covered by the fake suite.

Outcome (2026-11-01, live probe from the Step 7 grant's lobby; probe snippet `snippets/broker_probe.py`): brokered mints from inside the lobby work (rc 0, bindings produced), but they do not beat direct starts. One-time beachhead prep (token placement + in-VM CLI venv build) measured 29.6 s. Warm brokered mints: 4.6 s / 4.5 s — the direct mint of the same shape from here: 4.6 s. A brokered session is born locally unbound (exec by name fails until its binding is transplanted; transplant + ping proven), and the in-VM stop form is exact — `colab stop -s <name>` works, while the `--yes` flag and the positional form both fail rc 2 (the flag poisoned the probe's own stop of its second child, leaving an orphan assignment that took a transplant plus `stop_session_safe` from here to clear; isolated by the bridge probe the same day). Decision: bypass is the standing answer — direct starts through the hook machinery are the burst path, and the deferred warm runner (D4) is settled as not built. The beachhead's proven role remains renewal driver 1. The still-owed P-T3 folds in here as covered: timed/promoted starts are the Step 4 demand path, proven live in Step 7.

### Step 7 — Workflow docs and the live proof

`docs/lease-workflow.md` gains the envelope rules (threads start bursts by demand request with a `--via` tag, never by agent-fired commands; timed starts are `not_before` records). The repo `AGENTS.md` gains the envelope bullet beside the two-tier one. Live proof: the operator starts one grant (the single tap); a consuming thread's demand then starts and completes a burst job with no further operator interaction; the ledger and the grant card are checked against the run.

files: `docs/lease-workflow.md`, `AGENTS.md`
verify: `~/.local/bin/uvx pytest tests/` (full suite) plus the live proof above, reported with its ledger lines.
done when: the full suite is green and the live proof has run with exactly one operator tap — the grant's.

Outcome (2026-11-01, live): grant started on the operator's instruction (12 h, scope lobby + workload, caps snapshot 15/150/4). The hook's standing duty raised the lobby unattended (envelope-stamped, via `standing`, deadline = grant end, priced on attributed spend). A demand file planted with `--via step7-proof` was consumed by the auto duty and the workload burst was acquired envelope-stamped and ledgered (`lease-attributed`), then released after verification (`lease-released`; spend journal segment 22:29:08→22:30:46Z at 0.26 CU/h). Two deviations from the step's letter, recorded plainly: (1) the reaper hook's poller stalled ~11 minutes across a service restart (last poll 22:27:44Z; a manual hook run at 22:39:03Z fired the demand; normal cadence resumed from 22:41Z) — the proof's burst therefore started on a hand-kicked poll, not a scheduled one; (2) the operator counted three approval taps, all on agent-shell commands around the proof (the grant command and verification execs; a fourth landed on the Step 6 transplant exec) — the system's own operation after the grant cost zero taps, but agent verification must ride file reads and hook runs, not bespoke execs, and that is the recorded working rule from this proof.

## Open questions

- [ ] While a grant is active, may an out-of-scope demand run as an ordinary lease, or is it refused with a named event?
  Recommendation: refuse it — the envelope is the authorization, and silently running outside it recreates the ambiguity the envelope exists to remove.
  <!-- wb-question
  id=out-of-scope-demand
  type=choice
  options=refuse | allow as ordinary lease
  recommended=refuse
  answer=
  -->
- [ ] Do the panel's cap edits apply to a running envelope, or only to future grants and ordinary leases?
  Recommendation: snapshot only (Step 3 as written) — the grant tap authorized specific numbers; mid-window changes belong to the next grant.
  <!-- wb-question
  id=envelope-caps-live-or-snapshot
  type=choice
  options=snapshot only | live edits apply
  recommended=snapshot only
  answer=
  -->

## Critical files

- `sync/lease.py` — grant record, acquire, budget guard, duties, and state all change here
- `bin/colab-lease` — the request/grant verbs are the envelope's only entry points
- `ui/static/app.js` — the grant card becomes the envelope's tracking surface
- `docs/lease-workflow.md` — where the tap-free routing rules become standing instructions for consuming threads
- `tests/test_two_tier_fake.py` — the end-to-end envelope contract is pinned here
