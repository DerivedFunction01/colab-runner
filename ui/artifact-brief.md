# Colab Lease — artifact builder brief

The published artifact is a thin, private front-end over the lease
companion server that already runs on this machine. All lease logic
stays in that server (and the lease core behind it); the artifact's
server-side actions only proxy to it, and the page only renders what
the proxy returns.

## Data plane (server-side actions -> companion server)

Companion base: `http://127.0.0.1:8793` (ui/server.py in the
colab-runner repo; always-on while a lease matters, started by
`ui/ui.sh`). One artifact action per endpoint, JSON in/out:

- `state` — GET /api/state -> {records: [{session, action,
  record: {deadline, acquired_at, last_used_at, jobs, shape, caller,
  ...}, keepalive_heartbeat_age_s}], grant: {kind, granted_at, until,
  scope: {caps, standing_shape, burst_shape, shapes}} | null,
  envelope: {id, scope, caps,
  records: [attributed lease records with envelope_id and via],
  spend: {balance, day_spent, month_spent, standing_day_spent},
  session_spend} | null, specs: [{id, label,
  gpu, tpu, high_mem, rate_cu_per_hour (may be null),
  agent_startable}], budget:
  {balance, day_spent, month_spent, standing_day_spent, caps:
  {daily, monthly, standing_daily}}, caps}
- `start` {shape, hours} — POST /api/lease/start -> core result JSON
- `stop` {session} — POST /api/lease/stop -> core result JSON
- `grant` {hours, burst?} — POST /api/grant -> core result JSON
- `ungrant` {} — POST /api/ungrant -> core result JSON
- `budgetcaps` {daily_cu_budget?, monthly_cu_budget?,
  standing_daily_cu_cap?} — POST /api/config/budget -> core result
  JSON (a null value clears that cap; omitted fields are unchanged)
- `outbox` {after} — GET /api/outbox?after=<seq> -> {events:
  [{seq, ts, kind, detail}]}
- `restart` {} — the degraded panel's Restart button. A proxy call
  cannot reach a down server, so: if the companion answers, proxy
  POST /api/server/restart; if it is unreachable, start it on the
  host (bash ui/ui.sh start, from the repo root) and report
  that. Never claim a restart happened without the server answering
  afterwards.

If the companion server is unreachable, the action must surface that
plainly (the page shows "lease server unreachable") — never fabricate
state.

## Page (mirror ui/static in this repo — same design, same tokens)

The design of record is the approved mockup,
ui/mockups/2026-11-01-lease-lifecycle-mockup.html: phone panels for
the four states plus the desktop reflow (≥1024px: top bar with
balance, main column + 330px rail, 1200px cap). ui/static already
implements it; mirror ui/static.

- Theme: the workbench viewer's olive palette, CSS custom properties
  under the same `--wb-*` names as ui/static/theme.css (light + dark
  via prefers-color-scheme). Cards/panels per ui/static/app.css.
- Status cards per lease record: profile, session, state, deadline
  with a live countdown, keepalive heartbeat age.
- Budget card: balance, day/month spent vs caps, and an Edit caps
  editor — the three caps as number fields prefilled from
  budget.caps (blank = no cap), saved through `budgetcaps`; the
  events list renders the resulting `budget-caps-changed` event.
- Controls, two modes:
  - Normal lease: a spec picker rendered from state.specs (never
    hardcoded) — each option shows the spec's served label
    ("High-RAM", "T4", "Standard") and its rate ("0.26 CU/h") or
    "cost unconfirmed" when rate_cu_per_hour is null; duration
    presets 1/2/4/8/12/16/24 h and 1/2/3 d plus a custom hours
    field (days are sent as ×24 hours); projected cost = rate ×
    hours, or "cost unconfirmed".
  - Two-tier grant: grant duration (same presets) + a burst-spec
    picker (same specs list, default workload) + Grant button; an
    active grant shows its until time, the standing spend today
    vs cap (budget.standing_day_spent vs caps.standing_daily),
    and a Revoke (ungrant) button. With an envelope served, the
    grant card also renders the envelope: the snapshot caps at
    grant time, envelope spend (today / this month), and the
    attributed leases with their requester (via) tags and
    deadlines — all from state.envelope, never derived locally.
  - Terminate button on each live lease card, with a confirm step.
- Events list from the outbox (kind + time + detail), newest first;
  the envelope kinds `envelope-lease-started` and
  `envelope-lease-released` (spooled by the core through the
  reaper-events drain) render with the lease's profile and via tag.
- Poll state about every 15 s; countdown ticks every second
  (arithmetic on served timestamps is the only client-side
  derivation).

No agent operates the lease: page taps call the actions above, the
companion server and the lease core do the work, and the reaper hook
enforces deadlines whether or not the page is open.
