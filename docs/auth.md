# Colab authentication — credential record, the 2026-11-01 incident, and the guards

Findings first: the stored credential was destroyed on 2026-11-01 by an
automated CLI call, not by expiry. The defenses below now make that
class of loss structurally impossible, and any future auth death
surfaces as an explicit `auth_required` state instead of junk data.

## The credential

- Canonical file: `~/.config/colab-cli/token.json` (mode 600), mirrored
  to `state/transient/colab-cli/token.json` under the transient
  registry (`validate: colab-token`).
- Strategy: `colab --auth oauth2` — Google's InstalledAppFlow
  (copy-paste code variant, redirect hosted at
  sdk.cloud.google.com). The client is Google's public Cloud SDK
  installed-app client (`764086051850-…apps.googleusercontent.com`),
  not a project owned by the operator, so no testing-mode refresh-cap
  applies; the re-auth cadence is measured from the grant record
  below, not assumed.
- Validity check used everywhere: the file parses as JSON and carries
  a `refresh_token` string of length ≥ 20. Contents are never printed
  or logged.

## Grant record

| Grant (consent) | Strategy | Client | Outcome |
| --- | --- | --- | --- |
| 2026-10-29 ~15:23Z | oauth2 InstalledAppFlow | public Cloud SDK client | Worked 3 days; destroyed 2026-11-01 (incident below), not expired |
| 2026-11-01 13:17:54Z | oauth2 InstalledAppFlow | public Cloud SDK client | Current credential; recovered by direct code exchange (below) |

Update this table at every re-authorization: grant time, strategy,
client type. The panel's re-auth cadence is read from it.

## The 2026-11-01 incident

- ~12:59Z the credential was verified live. By 13:04:06Z the CLI log
  shows it unloadable; at 13:05:58Z an automated call whose token
  refresh had failed fell into the CLI's interactive login flow,
  which overwrites the token file with a 36-byte stub (a lone
  15-char `refresh_token`) BEFORE prompting. No code ever arrived;
  the stub remained.
- Every failure after ~13:06Z traces to the dead credential. The
  flow's prompt (`Enter the authorization code:`) appeared in the
  stdout of automated `usage`/`sessions` calls and was consumed as
  data: budget checks refused as "usage-unparseable", and a session
  list carrying the prompt read as "session absent", so the auto
  duty deleted lease #9's record under its still-live session (the
  session survived unmanaged; its record was reconstructed and
  re-adopted the same day). A mirror capture also propagated the
  stub over the mirror's good copy.
- Recovery: the owner's second authorization code was exchanged
  directly (authorization code + the flow's PKCE verifier, recovered
  from the CLI's own log) after two chat-relayed attempts failed —
  the relayed codes were corrupted in transcription (extra characters
  appended at the tail, proven byte-for-byte against the message
  record). Standing rule from this: an authorization code never
  transits an agent's re-typing unverified — verify the transcribed
  code against the message record before submitting it, or move it
  as a file.

## The guards (all landed 2026-11-01)

- `bin/colab-run`'s `colab()` — the single funnel for automated CLI
  calls: refuses to invoke the CLI when the stored token is
  missing/invalid; snapshots the token before each call and restores
  it (atomically) if the call leaves it invalid, including on
  timeout; any output carrying the login prompt becomes an
  auth-required result (rc 3, empty stdout) and drops a marker at
  `var/auth-required.json`, cleared by the next healthy call. Inert
  under `COLAB_BIN` (test fakes are not credential flows).
- The same funnel snapshots the session binding (`sessions.json`,
  landed 2026-11-13): the CLI rewrites it non-atomically on
  ordinary calls, and a write denied after the truncate left it at
  0 bytes — every recorded session read absent and the fleet
  blinded while its lease records stayed correct. A call that
  leaves the binding missing or unparseable gets the snapshot
  restored atomically; a binding that still parses (even `{}`) is
  the CLI's own view and is never overridden. The mirror's
  never-capture-empty guard counts an unparseable canonical as
  empty, so a torn binding can no longer overwrite its last good
  mirror copy either.
- `sync/lease/sessions.py`: `session_state` treats a prompt-carrying list as
  unknown, never absent; `acquire` branches on the tri-state and
  never stops/deletes/recreates on unknown (`acquire-unknown`);
  `budget_check` maps the guard's signal to reason `auth-required`;
  `state()` serves `auth_required` from the marker for the panel.
- Mirror (`ops/mirror_transient.py`): the token item is validated —
  an invalid canonical heals from a valid mirror on capture; an
  invalid copy never overwrites a valid one in either direction,
  regardless of mtimes.
- Atomic token writes (owner ruling): every save, mirror, restore,
  and the renewal kit's in-VM placement writes a `.tmp` sibling and
  renames it over the live file; the token is never written in place
  under its live name.

## Re-authorization procedure

1. Start `colab --auth oauth2 usage` and give the owner the URL.
2. The owner approves and pastes the code. Verify the code
   byte-for-byte before submitting it (see the standing rule above).
3. Confirm the token file is valid (keys + refresh_token length
   only), run a live `usage`/`sessions` check, and add the grant to
   the record table above.
