---
id: t-01M6CESRADQCANAYY3FMB6NB4S
title: Repo registry (pointers) and interpreter resolution
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 1
created: 2026-11-01
updated: 2026-11-01
---

Step 1 of .plans/2026-11-01-colab-env-provisioning-uv-venvs.md: config/repos.json pointer registry (no package lists), --repo on env builders, remote /content/env-registry.json merge, bin/colab-check --repo/--variant resolution. Verify: uvx pytest tests/test_env_registry_unit.py.

Owner ruling 2026-11-01 added before build: no tracked file hardcodes a specific repo for setup — registry is untracked local config/repos.json seeded from tracked config/repos.example.json (placeholder entry); setup code takes repo id from registry/default_repo, never a named default; bundle builder source comes from the entry's local path. Plan Step 1 updated accordingly.

Landed: sync/repo_registry.py (pointer-only validation, default_repo resolution, variant dest/delta, version-spec eval, remote merge/resolve); tracked config/repos.example.json (placeholder only); config/repos.json gitignored + seeded locally with the live entry; sync/make_snapshot_bundle.sh takes COLAB_REPO_SRC (no default, no repo named); sync/base_clone.sh generic COLAB_REPO_URL + required dest; env_build.sh and bin/colab-check resolve repo/dest/venv/checks from the registry. Verify: tests pending in test_env_registry_unit.py (written this build); live behavior for the registered repo unchanged by construction (same URL/dest/venv/check values, now data).
