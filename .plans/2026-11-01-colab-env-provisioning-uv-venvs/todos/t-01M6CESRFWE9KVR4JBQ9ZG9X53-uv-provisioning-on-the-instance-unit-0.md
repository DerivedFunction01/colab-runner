---
id: t-01M6CESRFWE9KVR4JBQ9ZG9X53
title: uv provisioning on the instance (Unit 0)
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 2
created: 2026-11-01
updated: 2026-11-01
---

Step 2: Unit 0 uv provisioning in env build (candidate resolution, official installer fallback, absolute-path only, PATH seeding for target script resolver), env_report uv fields, bootstrap_remote alignment. Verify: uvx pytest tests/test_env_build_unit.py + light live probe.

Landed in snippets/env_build.sh Unit 0: candidate resolution ($COLAB_UV_BIN, PATH, ~/.local/bin, ~/.cargo/bin), official installer fallback, verification by absolute path only, --print-uv probe mode emitting uv_path/uv_version/uv_install_route JSON, resolved uv dir seeded on PATH for the driven script's own resolver; report carries the three uv fields. snippets/bootstrap_remote.py aligned: same candidate resolver, bare-uv --system route dropped (optional requirements install now goes into an isolated scratch venv via resolved uv). Verify: tests/test_env_build_unit.py written this build (fake HOME/PATH fixtures, installer stub, no-bare-uv scan); live probe on the lease is parent Step 6 work.

LIVE (parent, lease workload, 2026-11-01): --print-uv probe on the VM resolved uv present at /usr/local/bin/uv 0.13.5, route present; second resolution no reinstall. Installer fallback untested live (image ships uv) — covered by unit fixtures.
