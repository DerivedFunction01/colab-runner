---
id: t-01M6CESS1JZBXDW3XFS8H5R0DS
title: Coexistence proof and system-route audit
status: done
plan: .plans/2026-11-01-colab-env-provisioning-uv-venvs.md
plan_step: 6
created: 2026-11-01
updated: 2026-11-01
---

Step 6: system-route audit + --with-system only for named exception; variant delta machinery (duckdb==2.0.0); live coexistence proof on shared lease; fresh-session rebuild leg on next new lease. Verify: results/ reports + full suite.

LOCAL parts landed; live legs are the parent's. System-route audit (full text in docs/lease-workflow.md, Step 7): every system-python consumer is stdlib-only (keepalive, heartbeat_job, probe_resources, sec_reachability, renewal, shell/env tooling) or venv-served (colab-check gate, facade/fixture/profiler jobs, bootstrap_remote's optional install now into a scratch venv). Two named exceptions, both probes of the image itself that install nothing and are never gate substrates: snippets/smoke_duckdb.py (reads the image's bundled duckdb version) and the OOM ramps bert_batch_ramp.py/memory_ramp.py (image torch, unavailability recorded). DECISION: no consumer needs repo packages in the system python, so --with-system is NOT implemented; --skip-system remains an accepted no-op in env_build.sh. Variant delta implemented: registry variants.<name>.delta applied via resolved uv pip install --python <variant venv> after init and reported as variant_delta (local seed registers the duckdb variant delta duckdb==2.0.0); remote records carry variant + versions_fingerprint for the coexistence comparison. LEFT FOR PARENT: live coexistence proof, fresh-session rebuild leg, Step 4 live parity run, Step 2 live Unit 0 probe, Step 5 live scoped gate.

LIVE coexistence (parent, 2026-11-01): workload-repo-envproof (default) and workload-repo-duckdb (variant, pre-clean fired, delta duckdb==2.0.0) built side by side on one session; both isolated, asserts pass, /content/env-registry.json carries both; rebuilding each left the other's fingerprint (c48910c04aa4) and system duckdb 1.3.2 unchanged. Defect found+fixed live: variant name recorded as raw string crashed the registry merge (silent) — kv now JSON-encodes variant and merge failure fails the build. REMAINING: fresh-session rebuild leg (both clones from empty /content on a new lease) — carried by followup plan .plans/2026-11-01-colab-env-fresh-session-rebuild-proof.md.
