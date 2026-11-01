---
id: t-01M6CAJ8Q18KY6B5PS2KEMFGE6
title: Tap-free timed starts
status: done
plan_step: 4
created: 2026-11-01
updated: 2026-11-01
---

Step 4 of .plans/2026-11-01-two-tier-envelope.md: request not_before and request --at; auto_acquire leaves future requests untouched and consumes when due.

Landed: write_request not_before + request --at (ISO-validated); auto_acquire returns not-due and leaves future demands untouched, fires when due. Verified: pytest test_lease_unit + test_auto_fake — 54 passed.
