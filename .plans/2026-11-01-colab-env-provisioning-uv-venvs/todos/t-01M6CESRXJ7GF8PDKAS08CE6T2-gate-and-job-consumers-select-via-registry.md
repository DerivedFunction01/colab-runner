---
id: t-01M6CESRXJ7GF8PDKAS08CE6T2
title: Gate and job consumers select via registry
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 5
created: 2026-11-01
updated: 2026-11-01
---

Step 5: bin/colab-check registry version checks + dest, facade/fixture jobs resolve repo+venv via env-registry, literal pin test. Verify: tests/test_env_registry_unit.py tests/test_shell_unit.py + live scoped gate baseline match.

Landed: bin/colab-check derives local checkout, clone URL, remote dest, venv path, and version checks from the registry entry (--repo/--variant, default default_repo); the hardcoded DuckDB tuple is replaced by generated spec-eval over the entry's version_checks (template compile + probe + satisfies() verified locally against this python); sync/base_clone.sh invoked with registry dest. snippets/facade_battery_job.py and snippets/fixture_gathering_job.py take --repo/$COLAB_REPO_ID and resolve dest + venv_python from /content/env-registry.json; snippets/facade_memory_profiler.py resolves via PROFILE_REPO_ID/PROFILE_REPO-as-dest against the same registry (legacy path fallback kept). sync/data_manifest.py lost its named-repo --artifacts default (registry default_repo checkout instead). Literal scan extended in tests/test_env_registry_unit.py (setup files + consumer clone-path scan). Verify: pytest tests/test_env_registry_unit.py tests/test_shell_unit.py tests/test_shell_job_unit.py tests/test_sync.py — 52 passed. Live scoped gate vs baseline is parent Step 6 work.

LIVE (parent, 2026-11-01): scoped gate bin/colab-check tests/foundation/test_hashing.py on the migrated shared substrate — applied tree verified, rc 0, 20 passed, 8 workers, substrate '3.13.18 duckdb==2.0.0' from registry checks.
