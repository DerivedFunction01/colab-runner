# Two-tier lease: a standing low-tier session plus burst high-RAM

Status: implemented 2026-11-01 (core `69fb72b`, UI `066134e`, on
main). The operator resolved the open choices by control placement: the mode
is his tap-time choice in the lease UI (normal lease vs two-tier
grant, grant duration = the duty cycle), and the burst spec is a UI
picker. As built: per-profile records with legacy migration, per-spec
rates (t4 unconfirmed — no official table exists and third-party
figures are not this account's price), budget split with the standing
sub-cap, the standing duty wired into the reaper hook, and warm-on-
renew recorded as a marker (the warm runner itself is a later step).
Probes P-T1–P-T4 below are still owed. This extends the lease model
(`.plans/2026-10-29-colab-lease-model.md`); it does not replace it.

## Findings first

- Approval taps are front-loaded per session host. Measured
  2026-10-31 on two hosts: a fresh standard session's create carried
  the one approval; every later call on that host (exec, upload, the
  SSH-bridge shape) ran clean, and lease #8's seasoned host ran all
  four shapes clean. Tap count therefore scales with session churn,
  not with work volume — a long-lived session amortizes its single
  tap, and while it is up, any thread task adopts it with no tap.
- Caveat, measured the same evening: novel command shapes re-flagged
  approvals mid-evening on a seasoned host (the forwarding probes), so
  grant durability across hours and shapes is a probe (P-T2), not a
  fact.
- The lease machinery is single-record today (`var/lease.json`); a
  second concurrent tier cannot be bolted on without per-tier records.
- Colab caps a session at 12 h regardless of keepalive (founding
  research), so "kept alive as long as possible" means a renewal
  cadence at ~12 h, each renewal costing one tap.

## The model

- **Lobby tier**: a standard session (2 vCPU / 13.6 GB, 0.08 CU/h)
  held standing by the same survival stack (keepalive loop + kernel
  pulse), renewed by the hook when it dies or nears the 12 h cap. It
  is the default home for light work: bundle applies, env builds for
  light jobs, small samples, test slices, network-bound fetches, the
  drivers' staging steps. Thread tasks acquire-or-adopt it exactly
  like today's lease — when it is up, they tap in by themselves.
- **Burst tier**: today's `workload` high-RAM lease, unchanged —
  demand-driven (the auto-lease seam already carries a profile per
  request), 8 h deadline, for memory- or CPU-heavy work.
- One heavy job at a time, unchanged; the lobby takes light-class
  jobs only (job class recorded on the run record; a heavy job never
  silently lands on the lobby).

## Cost scenario (why this needs its own ruling)

Rates: lobby 0.08 CU/h, burst 0.26 CU/h (2.08 per full 8 h lease).
Caps today: 10 CU/day, 100 CU/month.

| Lobby duty cycle | Lobby CU/day | Lobby CU/month | + one burst lease/day |
|---|---|---|---|
| 24/7 | 1.92 | ~57.6 | ~4.0/day, ~118/month — over the monthly cap |
| 16 h/day | 1.28 | ~38.4 | ~3.4/day, ~100/month — at the cap |
| 12 h/day | 0.96 | ~28.8 | ~3.0/day, ~90/month — inside, little headroom |

So the standing tier is affordable only with a duty cycle or a
raised monthly cap, and burst frequency is the swing factor. The
budget guard must split: a standing sub-cap (the lobby may not eat
the burst budget) inside the existing day/month checks.

## Configuration set (new or changed)

- `config/profiles.json`: a `lobby` profile (session `lobby`,
  standard tier) alongside `workload`.
- `config/lease.json`: a `standing` block — `enabled` (default off),
  `profile: "lobby"`, `renew_before_minutes` (pre-renew ahead of the
  12 h cap so adoption never sees a gap), and per-tier budget sub-caps
  (`standing_daily_cu`, burst keeps the remainder semantics).
- Lease records become per-profile (`var/lease-<profile>.json`); the
  reaper iterates records; the hook's auto duty gains the standing
  renewal (acquire the lobby when it is down and standing is enabled
  — no demand file needed; the demand seam stays burst-only).
- The lobby renews warm: bundle apply + shell env build (~35 s,
  measured) run by the hook's renewal path, so a renewed lobby is
  work-ready, not bare.

## Steps

1. Per-profile lease records + reaper iteration (burst behaviour
   byte-identical; suite proves it).
2. `lobby` profile + `standing` config block (off) + budget split in
   `budget_check`.
3. Standing duty in the hook/auto path: renew-on-death and
   pre-renew-before-cap, warm-on-renew, wake only on action/failure
   (today's rule).
4. Job-class marking so heavy work refuses the lobby.
5. Probes P-T1–P-T4; the standing flag flips only after P-T1 + P-T2
   report.

## Probes owed

- P-T1 Standing lifetime: a standard session under keepalive+pulse —
  does it reach the 12 h cap, and what kills it if not?
- P-T2 Tap durability: on one standing host across its full life, do
  the four channels stay tap-free, including shapes not used at
  creation time?
- P-T3 Promotion: a task starts light work on the lobby, demands a
  burst lease, and continues there — state handed off by bundle/record,
  nothing lost.
- P-T4 Renewal overlap: pre-renewal creates the successor while the
  predecessor still serves; adopters see one atomic record swap.

## Open rulings for the operator

- Lobby duty cycle: 24/7, active-hours, or on-first-demand each day?
- Monthly cap: raise it, or let the duty cycle fit inside 100?
- Warm-on-renew automatic, or bare lobby until first use?
- May light jobs run on the lobby while a burst lease is live
  (parallel light+heavy), or does the one-job rule span both tiers?
