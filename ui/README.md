# Lease control UI

The operator's manual control surface for the colab-runner lease: tap to start
a lease (or a two-tier grant), choose how long it lasts, terminate early.
No agent operates the lease — this server's scripts do, by shelling out
to `bin/colab-lease`. Start/stop/grant events are appended to the UI
outbox (`var/ui-outbox.jsonl`), which the `colab-lease-notify` hook
tails to announce them in the main chat.

## Run

```sh
ui/ui.sh start     # binds 127.0.0.1:8793 (ui/config.json), pidfile in var/
ui/ui.sh status
ui/ui.sh stop
```

Then open <http://127.0.0.1:8793>. Or run the server directly:

```sh
/usr/bin/python3 ui/server.py [--host 127.0.0.1] [--port 8793]
```

## How it works

- `ui/server.py` — Python standard library only. It holds no lease
  logic: reads and mutations are subprocess calls to `bin/colab-lease`
  (`state`, `acquire <profile> [--hours H]`, `release [profile]`,
  `grant --hours H [--burst <profile>]`, `ungrant`), and the core's JSON is passed through
  to the page. All derivation stays core-side; the page only does
  countdown arithmetic on served timestamps and projected-CU arithmetic
  over the served rate table.
- `GET /api/state` — core `state` JSON plus `profiles` and `caps` read
  from `config/profiles.json` / `config/lease.json`.
- `POST /api/lease/start {profile, hours}`, `POST /api/lease/stop
  {profile}`, `POST /api/grant {hours, burst}`, `POST /api/ungrant`.
  The profile/spec pickers (normal mode, and the burst tier in
  two-tier mode) are rendered from the core's `state.specs` list —
  every spec with its served label and `rate_cu_per_hour`; a null rate
  renders as "Cost unconfirmed", including in the projected cost.
- `POST /api/server/restart` — spawns `ui/ui.sh restart` detached
  (path overridable via `COLAB_LEASE_UI_SH`) and answers
  `{action: "restart-requested"}` immediately. It is the degraded
  panel's recovery action; it writes no outbox event.
- `GET /api/outbox?after=<seq>` — outbox events with `seq > after`.
  Reading never acknowledges; consumers keep their own checkpoint
  (the notify hook keeps its in `~/hooks/state/colab-lease-notify.json`).
- Outbox events: `{seq, ts, kind, detail}` with kinds `lease-started`,
  `lease-start-failed`, `lease-stopped`, `grant-started`, `grant-ended`.
  Single writer: this server.
- `ui/static/` — one page, no build step, no external assets. The theme
  (`theme.css`) mirrors the workbench viewer's design tokens
  (`--wb-*`: olive palette, surfaces, accent, radius, font stacks) so
  the page reads as a sibling of the workbench.

## Watchdog

This server is unsupervised — nothing else starts it, so a runtime
restart used to leave the panel down until a human noticed. The
`colab-lease-reaper` hook (script `~/hooks/scripts/colab-lease-reaper.sh`,
120 s poll) carries a watchdog duty that runs after its lease duties:
it probes `GET http://127.0.0.1:8793/` (the static page — cheap; any
HTTP response means up) and, when the probe fails, runs `ui/ui.sh start`
and re-probes once. The outcome is recorded as `ui_action` (`up`,
`restarted`, `restart-failed`) in `~/hooks/state/colab-lease-reaper.json`.
The hook stays silent while the server is up; it wakes the chat only
when it restarted the server or the restart failed. Hook dry runs
probe only and never restart.

## Env overrides (tests)

- `COLAB_LEASE_BIN` — lease CLI path (default `<repo>/bin/colab-lease`)
- `COLAB_RUNNER_VAR` — var dir (default `<repo>/var`)
- `COLAB_LEASE_UI_HOST` / `COLAB_LEASE_UI_PORT`

## Published artifact (Library)

The same page is published to the operator's artifacts library as
**Colab Lease** (slug `colab-lease-2`), a fullstack artifact whose
server-side actions proxy this companion server — the phone-friendly
surface; this localhost page remains the local one. Redeploy with
`bash ui/deploy.sh`: gate (tests) → sync the artifact sources to
the runtime dir (`$LEASE_UI_RUNTIME_DIR`) → publish step (builder pass via
the hatchlet CLI when present, otherwise the script prints the exact
builder request and exits 4, workbench-style). The data-plane
contract for the artifact lives in `ui/artifact-brief.md`.
`bash ui/deploy.sh check` is the read-only drift check: it compares
the repo's artifact sources against the deployed copies and exits 3
on any drift (modified, missing, or deployed-but-not-a-repo-source),
so repo, staging, and Library cannot silently diverge.
