---
id: t-01M6CAJ8HGJXDP6GWE737Z61WV
title: Envelope budget semantics
status: done
plan_step: 3
created: 2026-11-01
updated: 2026-11-01
---

Step 3 of .plans/2026-11-01-two-tier-envelope.md: budget_check caps override; in-scope pricing uses grant snapshot; out-of-scope/no-grant unchanged.

Landed: budget_check caps_override; in-scope acquire (and the standing duty through it) prices against the grant snapshot; out-of-scope/no-grant unchanged. Verified: pytest test_two_tier_unit + test_lease_unit — 62 passed.
