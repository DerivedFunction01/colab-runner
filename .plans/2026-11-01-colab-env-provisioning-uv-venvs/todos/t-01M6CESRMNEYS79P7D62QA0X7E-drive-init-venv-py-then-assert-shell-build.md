---
id: t-01M6CESRMNEYS79P7D62QA0X7E
title: Drive init_venv.py, then assert (shell build)
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 3
created: 2026-11-01
updated: 2026-11-01
---

Step 3: env_build.sh Unit 2 becomes drive-and-assert: pre-clean legacy inherited venv, drive clone init_venv.py (workload adapted --choice 0), refuse pip fallback, import scan + isolation + version asserts, new report fields. Verify: tests/test_env_build_unit.py + live scratch job-zero.

Landed in snippets/env_build.sh Unit 2 (full rewrite): pre-clean deletes a legacy inherited (include-system-site-packages=true) or pip-era (bin/pip without uv marker) venv at the declared path and records venv_rebuilt_legacy; drives the clone's init_venv.py per the registry invocation (arguments + cwd from the entry; the seeded local entry carries the adapted drive init_venv.py --choice 0 from clone root); any 'falling back' output from the script fails the build; old system-install block, pyvenv.cfg rewrite, pip ladder, and --ignore-installed are gone (--skip-system kept as an accepted no-op); import scan over registry scan_roots installs missing dists via resolved uv; isolation asserts (sys.path + pyvenv.cfg), registry version checks, derive_resources where the entry provides it; report drops venv_pip_bootstrap/system_install_route and adds init_route, venv_isolated, versions map + fingerprint, system_version_map_before/after; build merges its record into env-registry.json beside the clones. Verify: functional fixture test in tests/test_env_build_unit.py; live scratch job-zero is parent Step 6 work.

LIVE (parent, 2026-11-01): scratch job-zero /content/workload-repo-envproof in ~20s — init drive ok, venv_isolated=true, inherits=false, version checks ok (duckdb 2.0.0 in >=1.5.6,<2.1), import scan caught polars missing_before and installed it, system duckdb 1.3.2 unchanged, derive_resources ok. Shared-clone migration via colab-run env-build also landed (pre-clean fired, isolated, report pulled).
