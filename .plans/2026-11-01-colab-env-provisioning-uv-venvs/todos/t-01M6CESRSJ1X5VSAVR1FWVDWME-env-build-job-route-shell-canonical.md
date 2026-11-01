---
id: t-01M6CESRSJ1X5VSAVR1FWVDWME
title: Env-build job route: shell canonical
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 4
created: 2026-11-01
updated: 2026-11-01
---

Step 4: parity proof shell vs python twin, reroute jobs/env-build via shell_job.py, retire env_build_job.py (shell canonical per recommendations). Verify: uvx pytest tests/ + parity reports under results/.

Parity status: live parity (one colab-run run vs one shell run) is NOT provable locally and is flagged as parent Step 6 work; locally, the report schema is pinned by tests (tests/test_env_build_unit.py functional fixture builds produce the full new report contract end to end) and the twin's schema was a subset of the shell build's fields by inspection at deletion. Per the recorded recommendation the reroute completed anyway: jobs/env-build/job.json now routes through snippets/shell_job.py --script /content/env_build.sh (units 1, matching shell_job's checkpoint contract; requires env_build.sh + config/repos.json uploaded to /content first, documented in docs/lease-workflow.md); snippets/env_build_job.py deleted; snippets/shell_job.py errors cleanly when --script is missing on the VM.

LIVE (parent, 2026-11-01): colab-run env-build run env-build-20261101T211953Z complete through the shell_job route; env_report.json pulled via manifest outputs (parent fix: shell_job exports SHELL_JOB_OUT, env_build defaults OUT to it). Twin retired; live parity against the deleted twin is moot — the shell route itself is the proven job-zero path (scratch + shared dest).
