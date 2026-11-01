# Lease extension — plan

Status: implemented 2026-11-01 (worktree build merged at 555809b; gate fixes: TPU shape args in renewal minting, the lease-renewed spool/drain at 775772a, the budget-guard usage-stall refusal at 6cc5e5e; full suite green). Deviations from this plan as built: extend_grant takes the colab callable (pricing needs it); the release-guard abort is action release-aborted (rc 0); driver 2 waits one cycle when a kit-installed parent is alive below its cap rather than racing the in-VM mint; renewal children are named <session>-r<unix-ts>; the lease-renewed announcement runs core spool → companion drain (the outbox keeps one writer). Live proof PASSED 2026-11-01 (after the auth restoration, commit 752f630): disposable standard lease through the companion — start 1 h (deadline 14:28:27Z) → extend +1 h (deadline 15:28:27Z, renew set) → outbox gained lease-started + lease-extended → stop released, record cleared, live assignments back to 1 (workload only), no leftover session. The earlier "stall window" framing was corrected the same day: the refusals were the destroyed OAuth credential, not the proxy (docs/auth.md). A full driver-1 renewal cycle is likewise unobserved live: it needs an 11.5 h window or a temporarily lowered cap constant.
Resolves the mockup's D5: durations are windows. Renewal prefers the nested (in-VM child) driver proven 2026-11-01, with reaper renewal as fallback.

## What extension is

`extend` moves a live lease's end time later by H hours without touching its session. One operation across CLI, companion, and panel; the added window is priced by the budget guard before anything changes; every extension and renewal is announced through the outbox.

## Core

- `lease.extend(colab, repo, profile, hours)` + `bin/colab-lease extend [profile] --hours H`. New deadline = current deadline + H (H > 0, validated like acquire's). The budget guard prices only the delta (rate × H) against the daily/monthly caps; refusal returns `refused-budget` and nothing changes.
- `lease.extend_grant(repo, hours)` + `bin/colab-lease extend-grant --hours H` moves `grant.until`; the delta priced is the lobby's hours only, against the standing sub-cap.
- Extending sets `renew: true` on the record: an extension is a promise about an end time, so the lease becomes a window lease (below). A start whose duration exceeds one session's life also sets it at acquire.
- Release guard (hardens all leases): `release()` re-reads the record immediately before stopping and aborts if the on-disk deadline has moved into the future since the caller's read — an extension racing an in-flight release wins.

## Windows and renewal

A record's `deadline` is its window end. A session dies at the platform cap (~12 h from its own start) or early; a window may outlive several sessions. Renewal is a reap branch, fired only when **all** hold: the record has `renew: true`, it is not a standing record (the standing duty owns those), and now < deadline. Trigger: the session is confirmed absent, or its age passes the renewal threshold (cap − 30 min overlap margin).

- **Driver 1 — nested (preferred when the renewal kit is installed).** At acquire/extend time for a `renew` lease, the scripted warm path installs the kit in the VM: the CLI in a venv, the account token at its canonical path (mode 600, contents never printed or logged — the exposure accepted for the 2026-11-01 probe, now a stated design property), and `snippets/renewal.py`, which runs detached, wakes at the threshold, mints the child with the same shape, installs the kit in the child, and writes a handover file (the child's binding entry + the window record) under `/content` for the reaper to pull. The reaper's renewal step downloads the handover, merges the binding into the local sessions file programmatically (never printed), swaps the record's session, relaunches the keepalive, and stops the parent once the child answers.
- **Driver 2 — reaper fallback.** No handover by the threshold (or no kit): the reaper acquires a fresh session for the profile against the same window end and swaps the record.
- Renewal never re-prices the window (priced at grant/extend time) and never moves spend baselines. It preserves deadline, jobs, and profile; only `session` and the session's `acquired_at` change. A failed renewal retries next cycle; if the window ends first, the lease releases normally.
- Unflagged records keep today's behaviour exactly: a dead session is a cleared record, never resurrected (the 2026-10-29 away rule stands — renewal is opt-in by extension or by a long start, never blanket).

## Edge cases — normal leases

- Session state **unknown** → `extend-unknown`, nothing changes. Never act on an unreadable list.
- Session confirmed absent, `renew` false → the lease is over; extension refuses (`extend-no-lease`). A fresh start is a new lease.
- Session absent, `renew` true → the extension applies to the window; renewal fires on the next reap.
- Over budget → `refused-budget` with the projection; deadline unchanged.
- H ≤ 0 / non-numeric → CLI exit 2 (existing `parse_hours` behaviour).
- A job running → extension is allowed; the jobs list is untouched. The deadline is the only change.
- Repeated extensions accumulate on the deadline; each prices its own delta.

## Edge cases — two-tier

- Extend the **grant** → `until` moves; the standing duty follows automatically; the burst is unaffected in both directions.
- Extend the **burst** → normal rules; the grant neither extends nor blocks it.
- Extend the **lobby directly** → refused (`extend-standing-managed`): the grant owns the lobby's lifetime; extend the grant.
- Grant already expired → extension refuses (`grant-inactive`); a new window is a new grant.
- Ungrant during a pending renewal → ungrant wins: the grant is deleted, the lobby released, and the renewal branch skips standing records regardless.

## Notification

- New outbox kinds: `lease-extended` {profile, hours, old_deadline, new_deadline}, `grant-extended` {hours, old_until, new_until}, `lease-renewed` {profile, driver, old_session, new_session}.
- The notify hook announces each to the main chat (its vocabulary gains the three kinds).
- Main → users: hooks deliver to the main chat only, so the relay is a main-chat duty, recorded in `docs/lease-workflow.md`: when an extension or renewal changes a deadline that a consuming thread (e.g. the XBRL chat) is planning against, the main thread relays it to that thread in the same turn it is announced. The panel reflects the new deadline on its next poll — served state only.

## UI and companion

- Lease card: an Extend control opening the shared duration picker → `POST /api/lease/extend {profile, hours}`. Grant card: Extend grant → `POST /api/grant/extend {hours}`. Refusals surface the core's reason (budget, unknown, standing-managed) — no silent clamping.
- `ui/server.py` gains both endpoints; failure classification follows `is_success` (refusals are failures with reasons). Both actions append their outbox events through the server, as starts and stops do today.

## Tests (fake CLI, no network)

Extend: happy path (deadline moves, delta priced), refused-budget, unknown refusal, absent+renew window extension, repeated extension accumulation. Release guard: deadline moved on disk → no stop. Renewal: threshold trigger with driver 2; handover adoption with driver 1 (binding merge + session swap, handover file as fixture); renewal skips unflagged and standing records; failed renewal retries without touching the deadline. Two-tier: grant extension prices the lobby delta only, burst independence, lobby-direct refusal, expired-grant refusal. UI server: both endpoints against the stub core, refusal classification.

## Order of work

Core extend + release guard → renewal branch + kit snippet → companion + UI → tests throughout → parent gate: full suite, a live proof on a disposable standard lease (acquire 1 h, extend +1 h, watch the deadline, outbox, and announcement; release), deploy, artifact edit.
