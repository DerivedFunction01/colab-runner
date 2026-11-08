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
  (`state`, `acquire --shape <shape> [--hours H]`, `release [session]`,
  `grant --hours H [--burst <shape>]`, `ungrant`), and the core's JSON is passed through
  to the page. All derivation stays core-side; the page only does
  countdown arithmetic on served timestamps and projected-CU arithmetic
  over the served rate table.
- `GET /api/state` — core `state` JSON plus `caps` read from
  `config/lease.json`.
- Mutations are `POST` calls for lease start/stop/extend/adopt/
  shorten, access release, grant/extend/ungrant, budget config,
  and server restart;
  the full mapping is the Artifact data plane section below.
  The spec pickers (normal mode, and the burst tier in
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

## Supervision and watchdog

The server runs under systemd: `ui/colab-lease-ui.service.tmpl` is
the tracked unit template (`Restart=always`, logs appended to
`var/lease-ui.log`), `ui/install-unit.sh` renders and installs it,
and `ui/ui.sh` delegates every verb to systemctl while the unit is
installed (the nohup + pidfile path remains the fallback where it
is not). Runtime deployments replace this VM — PID 1 restarts,
/etc is wiped, every unsupervised process dies — which killed the
previously unsupervised (nohup) server at least four times on
2026-11-02 (12:41, 14:39, 16:50, 22:02 EST), each time in the same
minutes as the sibling companion servers and with the server log
ending mid-traffic on a 200: annihilated with the VM, never a
crash, never OOM (its RSS is ~17 MB).

Recovery is `ui/watchdog.sh`, the restore ladder in the sibling
servers' shape: probe `GET http://127.0.0.1:8793/` (the static page
— cheap; any HTTP response means up); healthy but unit file
missing → reinstall the unit quietly; down → reinstall the unit,
start the service, poll-verify; fallback → `ui/ui.sh start`,
poll-verify again. Exit 0 means healthy or restored-and-verified.
The `colab-lease-reaper` hook (script
`~/hooks/scripts/colab-lease-reaper.sh`, 120 s poll) carries a
watchdog duty that runs after its lease duties: it probes the
page and, when the probe fails, runs the ladder. The outcome is
recorded as `ui_action` (`up`, `restored`, `restart-failed`) in
`~/hooks/state/colab-lease-reaper.json`. A successful restore is
routine and stays silent — the hook wakes the chat only when the
ladder failed. Hook dry runs probe only and never restore.

## Env overrides (tests)

- `COLAB_LEASE_BIN` — lease CLI path (default `<repo>/bin/colab-lease`)
- `COLAB_RUNNER_VAR` — var dir (default `<repo>/var`)
- `COLAB_LEASE_UI_HOST` / `COLAB_LEASE_UI_PORT`

## Artifact data plane

The published artifact is a thin front-end over this companion
server (`http://127.0.0.1:8793`). All lease logic stays in the
server and the lease core behind it; the artifact's server-side
actions only proxy to the companion, and the page only renders
what the proxy returns. An unreachable companion is surfaced
plainly — state is never fabricated. One artifact action per
endpoint, JSON in/out:

- `state` — `GET /api/state` → core state JSON plus `caps`;
  `GET /api/state/<part>` serves one portion (`records`, `specs`,
  `budget`, `grant`, `meta`, `orphans`), and portions fail alone.
  The `orphans` portion is the orphan census: live sessions with
  no lease record, each carrying its derived deadline and the
  evidence behind it.
- `start` `{shape, hours}` — `POST /api/lease/start` → core result.
- `stop` `{session}` — `POST /api/lease/stop` → core result.
- `extend` `{session, hours}` — `POST /api/lease/extend` → core result.
- `adopt` `{session, deadline?, hours?, shape?}` — `POST /api/lease/adopt`
  → core result. With neither deadline nor hours the derived
  evidence plants itself; `shape` names the catalog shape for a
  class-ambiguous orphan. A core refusal answers 409 with the
  verdict.
- `shorten` `{session, hours}` or `{session, deadline}` —
  `POST /api/lease/shorten` → core result. Exactly one target:
  pull the deadline in by hours, or set it to the ISO deadline.
  A core refusal answers 409 with the verdict.
- `release` `{session}` — `POST /api/access/release` → core result
  (the unattributed-session release).
- `grant` `{hours, burst?}` — `POST /api/grant` → core result.
- `extend-grant` `{hours}` — `POST /api/grant/extend` → core result.
- `ungrant` `{}` — `POST /api/ungrant` → core result.
- `budgetcaps` `{daily_cu_budget?, monthly_cu_budget?,
  standing_daily_cu_cap?}` — `POST /api/config/budget` → core
  result. Omitted fields are unchanged; a null clears that cap.
- `outbox` `{after}` — `GET /api/outbox?after=<seq>` → `{events,
  last_seq}`.
- `restart` `{}` — `POST /api/server/restart`. A proxy call cannot
  reach a down server, so the artifact action starts the server on
  the host (`bash ui/ui.sh start`, from the repo root) when the
  companion is unreachable, and never claims a restart without the
  server answering afterwards.

The page renders only served state: spec pickers from
`state.specs`, budget and envelope figures from `state`, events
from the outbox. The only local derivations are arithmetic over
served timestamps and rates.

## Published artifact (Library)

The same page is published to the operator's artifacts library as
**Colab Lease** (slug `colab-lease-2`), a fullstack artifact whose
server-side actions proxy this companion server — the phone-friendly
surface; this localhost page remains the local one. Redeploy with
`bash ui/deploy.sh`: sync the artifact sources to
the runtime dir (`$LEASE_UI_RUNTIME_DIR`) → publish step (builder pass via
the hatchlet CLI when present, otherwise the script prints the exact
builder request and exits 4, workbench-style). The data-plane
contract is the section above; `ui/artifact-brief.md` is a pointer
retained because deploy syncs it into the runtime `brief/`.
`bash ui/deploy.sh check` is the read-only drift check: it compares
the repo's artifact sources against the deployed copies and exits 3
on any drift (modified, missing, or deployed-but-not-a-repo-source),
so repo, staging, and Library cannot silently diverge.
