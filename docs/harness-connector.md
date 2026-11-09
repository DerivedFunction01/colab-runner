# Harness connector — venue bootstrap and ferry relay

The harness connector (owned by the scheduler repo; contract
annex: scheduler `.plans/2026-11-08-harness-connector-tool-inventory.md`)
exposes the agent tier's privileged surface as tools on a
service that binds loopback/tailnet only. This repo owns the
venue side: preparing a Colab session to consume it (connector
plan, Step 5).

## Shape

A Colab session cannot reach the service, so the client
(the scheduler repo's `bin/harness`, shipped as-is) speaks a
ferry transport: each call is a request file
`{"id", "method", "path", "token", "body"}` in the session's
`/content/harness-ferry/out`; the home-side relay performs it
against `http://127.0.0.1:8796` with the envelope's token as
the `Authorization: Bearer` credential and lands
`{"status", "body"}` at the same id in
`/content/harness-ferry/in`. Only `/tools` paths are relayed —
the relay refuses anything else locally, and the service never
sees it. The relay deletes each request file once served; the
client deletes each response once read.

The relay is runner-internal channel use, like the bridge
duty: its exec calls ride the funnel directly and take no sync
claim. Bridged tools (chat reads/sends) additionally wait on
the home bridge agent's poll cycle, so a bridged call's
latency is cycles, not milliseconds; direct tools answer on
the next relay cycle.

## Bootstrap

One command prepares a session:

```
bin/colab-harness-bootstrap [--consumer sweeper-venue]
    [--scopes read,write-state,send] [--remint]
```

It mints (or reuses) the consumer's connector token through
the scheduler's `bin/sched connector-token` verb — the token
is captured from the verb's stdout directly into
`var/harness/<consumer>.token` at 0600 and never printed or
logged; the server keeps only its SHA-256, and revocation is
the same verb with `--revoke`, effective immediately. It
stages the client byte-for-byte, launches the
`harness-bootstrap` job (caller `panel` — runner machinery
keeps this repo's registry identity; the planted token belongs
to the connector's own registry, one caller per consumer
role, never per session), relays until the run lands, and
prints the verdict: run id, session, run state, relay totals,
and the smoke result.

In-session, the payload installs the client at
`/content/harness-client/harness` and the token at
`/content/harness-client/token` (0600), shreds the uploaded
token copy (zero-overwrite, then unlink) so the token exists
in exactly one place in the session, and runs one smoke call —
`chat_list`, a bridged read — through the ferry. The payload
exits with the smoke's verdict, so the run record states the
bootstrap's truth, and `smoke-result.json` rides the run's
outputs home. The token is read from its installed home into
the smoke subprocess's environment only; it appears in no
config file, output, or log.

## Relay as a standing process

A consumer that runs longer than a bootstrap (a venue sweep)
needs the relay for its duration:

```
bin/colab-harness-relay -s SESSION [--interval 5] [--cycles N]
```

With no `--cycles` it runs until interrupted and prints what
it served, one timestamped line per serve (id, method, path,
status). The bootstrap driver keeps the same record as JSONL
at `var/harness/relay-<session>.jsonl`.

One relay per session, enforced: a relay holds an flock on
`var/harness-relay-<session>.lock` for its lifetime, and a
second relay for the same session refuses — two relays cycling
one ferry both read the same listing and both perform the same
envelope, which for a bridged write is duplicated work with
side effects (a `chat_send` would send twice). When the
bootstrap finds the lock held, it serves nothing itself and
runs reconcile-only while the holder serves the ferry.
