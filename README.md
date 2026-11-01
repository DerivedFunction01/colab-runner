# colab-runner

Runs compute-bound work from this 2-vCPU machine on a Colab Pro high-RAM CPU runtime (measured 8 vCPU / ~51–55 GB) via Google's official `google-colab-cli`. First consumer: workload-repo / XBRL — code and gate jobs, pipeline jobs, and probes.

## How a job runs

1. `bin/colab-run <job>` (or `bin/colab-check <paths>` for a gate job) restores transient state from the repo mirror and acquires-or-adopts the profile's shared lease session (`bin/colab-lease`; `sync/lease.py`) — one session per profile, treated as a VM, 8 h hard cap, budget-guarded. Code syncs in as a git bundle on top of a base HTTPS clone.
2. A remote bootstrap snippet prepares the VM (clone, dependencies, resource probe, Python-version assertion).
3. The job executes detached on the VM (never as a kernel cell — the kill matrix measured that cells die with the local CLI); the wrapper polls and pushes checkpoints off the VM at the job's cadence.
4. Results return as artifacts plus a hash manifest. The session stays up for the next job; a script-level reaper hook (2-minute poll) enforces the lease deadline. Colab is compute-only: code is authored on this machine, never on the VM.

## Key pieces

- `config/transient.json` + `ops/mirror_transient.py` — everything that would not survive a VM restart (OAuth token, session state, the CLI install) is mirrored into `state/transient/` (untracked) and restored from there, by the reaper hook's poll and on every wrapper use, under per-item rules in the registry.
- `config/profiles.json` — the spec catalog: every grantable Colab tier with its CU/h rate (null = unconfirmed) (`workload`: high-RAM CPU; `lobby`/`standard`: standard CPU; `t4`: T4 GPU).
- `ui/` — the operator's lease control panel (127.0.0.1:8793): start any spec for a chosen duration, grant a two-tier lease (standing lobby + burst spec), terminate early; every action is a `bin/colab-lease` call, and starts/stops/grants are announced to the main chat via the outbox + notify hook. No agent operates the lease.
- `sync/` — snapshot-bundle construction on the origin lineage, bundle apply, the per-class data manifest (transfer vs driver-plus-regenerate vs network regeneration, priced), and result pull-back with hash verification.
- `probes/` — the P0–P4 probe suites: local network, session shape, transfer channels, execution behaviour + kill matrix, restart survival. Results land in `probes/results/`.
- `snippets/` — the versioned snippet library (the CLI has no snippet feature): remote bootstrap, resource probe, smoke tests, heartbeat job, and job zero (`env_build.sh`: Unit 0 `uv` guarantee + the clone's own `init_venv.py`, driven from the local repo registry in `config/repos.json` — untracked, seeded from `config/repos.example.json`).
- `jobs/` — job manifests: pipeline jobs and gate jobs.

## First-time setup

Operator-only step: the one-time OAuth login.

```bash
ops/bootstrap_local.sh          # installs google-colab-cli via uv, restores transients
colab --auth oauth2 new -s workload --high-mem   # prints a URL; open it anywhere, paste the code back
```

After that, the token is cached and mirrored, and wrappers handle the rest. Design and rulings: `.plans/2026-10-29-colab-runner-for-workload-repo.md`.

## Status (2026-10-29)

Login is done and the system is live. Proven: the transient mirror and bootstrap (CLI 0.7.5; the tool interpreter must be pinned to Python ≥ 3.12 — an unpinned `uv tool install` resolves a 3.11 here and fails); the sync layer (94.7 MB origin-lineage snapshot bundles, hash-verified, applied on the VM with tree equality; the remote apply pins the scratch clone to the snapshot's parent, so origin moving mid-check no longer breaks it); the probe suite — P1 session shape (8 vCPU / 54.7 GB / Python 3.13.18, drivemount unusable headless), P2 transfer channels (single-file reliable to ~50 MB, chunked above; in-VM clone works), P3 (SEC reachable from the VM; install set 14 s), the kill matrix (kernel-cell jobs die with the local CLI in every kill mode; detached jobs are the sanctioned pattern); the first live `bin/colab-run` end-to-end; the restore+reap reaper (an agent cron at first; replaced 2026-10-31 by a script-level hook after the cron was found silently disabled); and the lease model (`.plans/2026-10-29-colab-lease-model.md`) — first away lease acquired 2026-10-29 with the VM warmed and verified. Test suite: `uvx pytest tests/` green (9 tests, fake CLI).

Still owed: the low-tier OOM loop's final record (driver restructured to detached processes after two harness-eaten attempts; rerun in flight), the P4 Colab leg (stop mid-job, resume on a fresh session, hash-equal finish), a real `bin/colab-check` comparative verdict (remote leg needs the registered repo's venv built on the VM by job zero), per-part retry/resume for very large chunked transfers (a 1024 MB upload failed at part 12 of 32), and the lease probes P-L1–P-L3 (idle reap, adoption, deadline behaviour).

Limit probes are Colab-only by construction: the OOM snippets refuse to run without a Colab runtime marker (`snippets/_remote_guard.py`), and the OOM loop runs on the cheap standard tier, whose purpose is determining the Colab runtime's edge-case behaviour at its limits.
