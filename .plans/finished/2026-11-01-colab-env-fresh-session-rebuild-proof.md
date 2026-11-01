# Colab env: fresh-session rebuild proof (followup)

Status: complete — executed 2026-11-01 on the 24h standard probe session (`standard-r1793561066`) under the operator's route change: the workload high-RAM lease was released early at his direction and the timed demand for a fresh lease was cancelled, so the proof ran on the probe instead of a new lease. The probe session pre-existed, but its `/content` was clone-free (keepalive files + `sample_data` only), so both clones were still built from no prior clone/venv state. Both builds passed (`assertions_passed`, isolated, inherits false, system duckdb 1.3.2 unchanged, duckdb 2.0.0 in both venvs), `/content/env-registry.json` carries both dests, scoped gate 20 passed on the probe substrate; reports filed under `results/fresh-session-20261101T215952Z/`.

## Objective

On the next new Colab lease, prove job zero rebuilds both registered clones from empty `/content` state with no manual steps: the default clone and its `duckdb` variant, each isolated, asserts passing, both recorded in `/content/env-registry.json`, system python untouched. Ends when both builds' reports are filed under `results/` from that fresh session.

## Rulings

- Runs on the next lease acquired through the ordinary lease machinery; no session is created for this proof alone.
- One heavy build at a time, per the shared-lease rule; the two builds run sequentially.
- The proof uses the shipped `snippets/env_build.sh` and the local `config/repos.json` exactly as a consuming thread would: upload both to `/content`, run the default build, then the `--variant duckdb` build.

## Steps

### Step 1 — Fresh-session rebuild of both clones

Upload `snippets/env_build.sh` and `config/repos.json` to `/content`. Run `bash /content/env_build.sh --repo workload-repo --out /content/env-build-out-fresh` (clone from origin, `init_venv.py` drive, asserts), then `bash /content/env_build.sh --repo workload-repo --variant duckdb --out /content/env-build-out-fresh-duckdb`. Pull both `env_report.json` files into `results/` and check them against the 2026-11-01 coexistence fingerprints' shape: isolated, inheritance false, version checks ok, system version map unchanged.

files: `snippets/env_build.sh`, `config/repos.json`
verify: both reports show `assertions_passed = true`, `venv_isolated = true`, and `/content/env-registry.json` carries both dests; the scoped `bin/colab-check` gate on the fresh substrate passes.
done when: both clones stand built from empty state on the new session and the reports are filed.

## Critical files

- `snippets/env_build.sh` — the job-zero build being proven
- `config/repos.json` — the local registry the builds resolve from
- `docs/lease-workflow.md` — the job-zero procedure this proof exercises
