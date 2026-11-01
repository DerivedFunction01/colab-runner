---
id: t-01M6CESS6B4CQ421H8M0GDT900
title: Docs, AGENTS, workflow alignment
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 7
created: 2026-11-01
updated: 2026-11-01
---

Step 7: AGENTS.md shell-channel bullet, docs/lease-workflow.md job-zero/shell sections, README. Verify: uvx pytest tests/ + grep pass.

Landed: AGENTS.md shell-channel bullet now records the pip-era defects as history of the retired route, plus a new env-provisioning bullet (registry as local data, no tracked repo literals, Unit 0 absolute-path uv, init_venv.py-primary drive with pre-clean/fallback refusal/isolation asserts, env-registry resolution, variant deltas, per-session rebuild). docs/lease-workflow.md job-zero step and shell sections rewritten to the shipped flow, incl. the Step 6 system-route audit (named exceptions: smoke_duckdb.py and the OOM ramps, image probes only). README snippet/status lines follow. Verify: full suite 194 passed (baseline 149 + 45 new); grep pass shows no doc instructing the system-install/inherited route as current practice (remaining system-site mentions are the isolation checks themselves and the recorded history).
