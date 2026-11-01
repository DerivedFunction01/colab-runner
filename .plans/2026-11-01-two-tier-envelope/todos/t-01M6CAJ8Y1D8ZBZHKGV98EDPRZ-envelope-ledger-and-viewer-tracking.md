---
id: t-01M6CAJ8Y1D8ZBZHKGV98EDPRZ
title: Envelope ledger and viewer tracking
status: done
plan_step: 5
created: 2026-11-01
updated: 2026-11-01
---

Step 5 of .plans/2026-11-01-two-tier-envelope.md: var/grant-ledger.jsonl, state envelope block, grant-card rendering, envelope outbox kinds through reaper drain.

Landed: var/grant-ledger.jsonl at grant/acquire/renewal/release seams; state envelope block (scope, attributed records, spend vs snapshot caps, derivation limit in docstring); envelope-lease-started/released spool through the existing drain; grant card renders the envelope; panel starts stamp via=panel. Also fixed 3 pre-existing tests that pinned the old live caps (10/100/2.0) — they now read the operator's panel-set config. Verified: pytest test_two_tier_fake + test_ui_server + test_two_tier_unit — 56 passed.
