# Colab env provisioning: uv-built isolated per-repo venvs on a shared instance

Status: complete — landed 2026-11-01 (local suite 194 passed; live proofs on lease workload: Unit 0 probe, scratch + variant + shared-clone builds all isolated with asserts passing, coexistence fingerprints stable, scoped gate rc 0 on the migrated substrate). Remainder: the fresh-session rebuild leg is carried by `.plans/2026-11-01-colab-env-fresh-session-rebuild-proof.md`.

## Objective

Redesign how this repo provisions environments on a Colab instance: `uv` is resolved-or-installed on the VM and invoked only by absolute path, and each repo clone's own `init_venv.py` is the primary builder of its isolated venv (no system-site inheritance, no system-wide package installs by default), driven by this repo's env build, which owns only what `init_venv.py` does not — the `uv` install guarantee, pre-cleaning a legacy inherited venv the script would otherwise reuse, the isolation and version-spec asserts, the import scan, and the report — so several repos and variant clones coexist on one lease with no package cross-talk and per-repo pins honored. Ends when two clones with conflicting DuckDB specs (the `>=1.5.6,<2.1` line and a `2.0.0` variant) pass their toolchain asserts simultaneously on one session, with the system python untouched, and a fresh session rebuilds both from scratch as job zero.

## Rulings

- Scope is the env-provisioning split only: the lease/session machinery, `config/profiles.json` as the tier/session catalog, the budget guard, keepalive, and renewal are not redesigned. Profiles name sessions and shapes; they never named repos — repo identity, currently hardcoded as workload-repo across the env build, `bin/colab-check`, and the facade/fixture jobs, becomes explicit data under this plan.
- Each repo's own `init_venv.py` is the primary route (owner ruling, 2026-11-01): it owns the package spec (its `PACKAGE_GROUPS`, its `requirements.txt`, or the pins it reads from its `pyproject.toml`), the venv creation, and the install, in its own declared venv location. This repo's registry therefore stores pointers, never package lists: clone URL, dest slug, venv path, the `init_venv.py` invocation (arguments and working directory), version checks, and import-scan roots. A spec is never duplicated into this repo — the three-place DuckDB pin duplication across the two env builders and `bin/colab-check` is the defect this removes.
- No tracked file hardcodes a specific target repo for setup (owner ruling, 2026-11-01): provisioning code, scripts, and config templates are repo-agnostic — no repo URL, dest, venv path, local checkout path, version check, or scan root for any named repo appears in tracked setup files, and no command defaults to a named repo. Repo definitions are local data: the live registry is an untracked `config/repos.json` (tracked `config/repos.example.json` carries a placeholder entry only), seeded per machine, and a fresh install that registers any subset of repos — or removes one by deleting its entry — works with no code change and no other repo's entry present. Consumer job manifests for a specific repo are that repo's own jobs, not setup machinery; tracked tests use fixture registries naming fictional repos.
- A repo whose `init_venv.py` needs adaptation is driven with it, never bypassed silently and never edited from this repo: workload-repo's is interactive only when invoked with no arguments — with `--choice` it runs straight through — resolves `uv` by bare `PATH` only, and creates a cwd-relative `.venv`, so its drive is a named special case in Step 3 (seeded `PATH`, clone-root working directory, `--choice 0`, no torch choice). Its broader install set is not a cost: many of those packages mirror the Colab image's own system set, which the venv previously inherited anyway; installing them as the venv's own copies is the isolation working as intended.
- No `init_venv.py` installs `uv` itself, and on the Colab image the scripts' pip fallback (`python3 -m venv` into the pip-less venv) is broken by construction: the driver guarantees a resolvable `uv` before invoking any script (Step 2) and a build whose script reports it fell back to pip has failed, whatever else passed.
- The shared session is one VM: everything under `/content` is per-session state that dies with the session and is rebuilt by job zero on the next one. Nothing in this plan makes a clone, a venv, or the remote registry persist across sessions; local persistence stays exactly what exists today (lease records, config, results).
- No bare `uv` in any automation, local or remote: resolution follows the `find_uv()` candidate pattern already in `ops/bootstrap_local.sh` (`PATH`, then `$HOME/.local/bin/uv`, then `$HOME/.cargo/bin/uv`), the resolved absolute path is what gets invoked and recorded, and a PATH-stripped shell is a tested case, not an edge case.
- A venv built on the VM's system `python3`, with the interpreter version recorded beside every comparative result and never pinned — the standing no-pin ruling stands; `uv` manages packages and venvs here, not interpreter downloads.
- System-site inheritance is a failure condition: a build whose venv reports `venv_inherits_system_packages = true` has failed its asserts, whatever else passed. The current forced-`true` `pyvenv.cfg` rewrite, the pip seeding ladder (`ensurepip`, `get-pip.py`), and the `--ignore-installed` workaround all exist only to serve the inherited pip route and are removed, not ported — a `uv` venv is pip-less by design and `uv pip install --python <venv python>` is the only installer.
- Steps are written against the recommended answers in `## Open questions`; a question resolved the other way re-scopes only the step it names, as noted there. Implementation tracks the steps as workbench bundle todos, one per step; the harness arms the progress checks, so no separate status interval is set.
- Live proof work rides the existing shared lease and the one-heavy-job-at-a-time rule: proofs use scratch dests and scoped gates, never a second heavy gate alongside another thread's, and create no session outside the lease machinery.

## Steps

### Step 1 — Repo registry (pointers, not specs) and interpreter resolution

Add the repo registry as local data: an untracked `config/repos.json` (gitignored, seeded from a tracked `config/repos.example.json` whose single entry is a placeholder repo), one entry per repo holding pointers only — `id`, clone URL, local checkout path, dest slug under `/content`, the repo's declared venv path (each repo declares its own, e.g. `.venv` or `var/venv`), the `init_venv.py` invocation (arguments, working directory, any adaptation flags), version checks, import-scan roots, and an optional `default_repo` marker. No package list is stored: the spec lives in each repo's `init_venv.py` and what it reads. `snippets/env_build.sh` and `snippets/env_build_job.py` gain `--repo <id>` and lose every built-in repo literal (clone URL, default dest, package set); with no `--repo` they take the registry's `default_repo`, and with neither they refuse. `sync/make_snapshot_bundle.sh` takes its source checkout from the selected entry's local path (its repo-named env override becomes a generic one), so the bundle route is as repo-agnostic as the clone route. The local seed for today's work registers the one repo in current use; nothing in code or tests depends on its presence. Each build merges its result into a remote `/content/env-registry.json` — per repo: dest, venv python path, cloned head/tree, VM python, `init_venv.py` route taken, and the built venv's version-map fingerprint — written by the build, never hand-edited, and rebuilt with the session. `bin/colab-check` gains `--repo <id>` and `--variant <name>` (defaulting to the registry's `default_repo`) and derives its local checkout, remote dest, venv path, and working directory from the registry instead of hardcoded strings.

files: `config/repos.example.json`, `.gitignore`, `snippets/env_build.sh`, `snippets/env_build_job.py`, `sync/make_snapshot_bundle.sh`, `bin/colab-check`, `tests/test_env_registry_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_env_registry_unit.py` — registry parsing over fixture registries with fictional repos (a package-list field in an entry is rejected), variant dest naming (`<slug>-<variant>`), remote-registry merge (a second repo's build never clobbers the first entry), `default_repo` resolution, and a scan pinning that the setup files (`bin/colab-check`, `snippets/env_build.sh`, `snippets/env_build_job.py`, `sync/make_snapshot_bundle.sh`, `sync/base_clone.sh`) contain no named-repo literal.
done when: a repo's dest, interpreter, invocation, and checks are looked up from the local registry at every seam this step touches, the live behavior for the currently registered repo is unchanged, and deleting that repo's entry (fixture) leaves every setup command working for the remaining entries.

### Step 2 — uv provisioning on the instance (Unit 0)

The env build gains a Unit 0 ahead of clone: resolve `uv` by the candidate list (an explicit override env var, `PATH`, `$HOME/.local/bin/uv`, `$HOME/.cargo/bin/uv`); if no candidate verifies via `<candidate> --version`, install with `curl -LsSf https://astral.sh/uv/install.sh | sh`, then re-resolve across the same candidates plus the installer's landing spots and verify by absolute path — the installer's own PATH output is never trusted. Idempotent: a verifying candidate short-circuits with no network call, and a re-run reports the same path and version. Failure to install or verify fails Unit 0 and the build. Unit 0 also guarantees discoverability by the target repo's own resolver: the invocation environment for `init_venv.py` carries the resolved `uv`'s directory on `PATH`, so a candidate-based `find_uv()` finds it and a bare-`PATH`-only detection (workload-repo's) finds it too — this seeding is the one sanctioned use of `PATH`, and the driver itself still invokes `uv` only by absolute path. `env_report.json` gains `uv_path`, `uv_version`, and `uv_install_route` (`present` | `installer` | `failed`). `snippets/bootstrap_remote.py` is aligned to the same resolver and drops its bare-`uv` `--system` install route.

files: `snippets/env_build.sh`, `snippets/env_build_job.py`, `snippets/bootstrap_remote.py`, `tests/test_env_build_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_env_build_unit.py` — resolver tests over fake `HOME`/PATH fixtures (candidate order, PATH-stripped shell, installer landing in either candidate dir, no bare `uv` invocation anywhere in the builders, pinned by scan), plus a light live probe via `bin/colab-sh` on the active lease showing Unit 0 resolving the installed `uv` with no reinstall on a second run.
done when: a PATH-stripped build finds or installs `uv`, records its absolute path and version in the report, and a repeat build performs no install.

### Step 3 — Drive `init_venv.py`, then assert (shell build)

Rebuild the shell build's Unit 2 as a driver around the clone's own script. In order: (1) pre-clean — a pre-existing venv at the repo's declared path that is pip-era or inherited (`include-system-site-packages = true`) is deleted first and the deletion recorded as `venv_rebuilt_legacy`, because every known `init_venv.py` reuses an existing venv instead of recreating it, and installing into an inherited venv would silently keep the system substrate; (2) invoke the registry's recorded invocation in the clone with Unit 0's seeded environment — for a clean non-interactive script that is plain `python3 init_venv.py`; for workload-repo it is the adapted drive, `python3 init_venv.py --choice 0` from the clone root (its `.venv` is cwd-relative; the script is interactive only with no arguments, so the `--choice` invocation never prompts), with no torch choice taken; (3) refuse a fallback — output showing the script fell back to `python3 -m venv` + pip fails the build (that route is broken on this image); (4) delete the old system-install block (its opt-in successor is Step 6), the `pyvenv.cfg` inheritance rewrite, the pip seeding ladder, and `--ignore-installed` from the build. Colab-runner keeps the contract the scripts do not carry: the import scan (roots from the registry) installing and asserting any third-party module the tree imports beyond the declared spec, the isolation asserts (`venv_inherits_system_packages` must be `false`; no system site-packages directory on the venv's `sys.path`), the registry's version checks, and `derive_resources()` where the repo provides it. The report drops `venv_pip_bootstrap` and `system_install_route`, and adds `init_route` (the invocation used), `venv_isolated`, and a per-package version map.

files: `snippets/env_build.sh`, `tests/test_env_build_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_env_build_unit.py` plus a live job-zero run via `bin/colab-sh` into a scratch dest on the active lease: report shows the `init_venv.py` route, `venv_isolated = true`, inheritance `false`, asserts passed, and the system python's package versions are identical before and after the build; a planted inherited `.venv` at the dest is rebuilt, not reused.
done when: the scratch build's toolchain was built by the clone's own `init_venv.py`, runs entirely from its isolated venv, and the system python is provably untouched.

### Step 4 — Env-build job route: shell canonical

Per the recommended twin ruling: prove report parity once (one `colab-run env-build` run and one shell run into scratch dests, same report fields and verdict), then reroute `jobs/env-build` through `snippets/shell_job.py` driving `snippets/env_build.sh`, port Unit 0 and the isolated build's semantics into `snippets/env_build_job.py` only if parity fails for a structural reason, and otherwise retire the Python twin and its `jobs/env-build` manifest entry in favor of the shell route. If the twin question resolves to keep-parity instead, this step becomes the parity port: the twin gains `--dest`/`--repo` arguments and the identical Unit 0 + drive-and-assert semantics, and both builders stay.

files: `jobs/env-build/job.json`, `snippets/shell_job.py`, `snippets/env_build_job.py`, `tests/test_env_build_unit.py`
verify: `~/.local/bin/uvx pytest tests/` — plus the parity comparison above, with both reports filed under `results/` for the record.
done when: exactly one canonical env-build route exists (or both, under the alternate ruling), and `bin/colab-run env-build` produces the new isolated report end to end.

### Step 5 — Gate and job consumers select via the registry

`bin/colab-check` loses its remaining hardcodes: the DuckDB tuple assert is replaced by the registry's version checks for the selected repo, `sync/base_clone.sh` is invoked with the registry dest, and the gate refuses — as today — when the resolved venv is missing or a checked version is out of spec. `snippets/facade_battery_job.py`, `snippets/facade_memory_profiler.py`, and `snippets/fixture_gathering_job.py` derive their repo dir and venv python from an env/argument repo id resolved against `/content/env-registry.json` (the profiler's `_VENV_PY` hardcode goes; its existing `PROFILE_REPO` override becomes a repo-id selector). The Step 1 scan extends here: no named-repo literal survives in any tracked setup or consumer file (`bin/`, `snippets/`, `sync/`); consumer jobs resolve their repo from an argument or env repo id, and their manifests' repo binding moves to the invocation, not the file.

files: `bin/colab-check`, `snippets/facade_battery_job.py`, `snippets/facade_memory_profiler.py`, `snippets/fixture_gathering_job.py`, `tests/test_env_registry_unit.py`
verify: `~/.local/bin/uvx pytest tests/test_env_registry_unit.py tests/test_shell_unit.py` plus one live scoped `bin/colab-check` gate on the `workload` profile whose verdict and substrate line match a pre-change baseline gate on the same paths.
done when: a gate for a non-default repo or variant runs under that clone's venv with no code edits, and the workload-repo default gate is unchanged in behavior.

### Step 6 — Coexistence proof and the system-route audit

Audit every remaining consumer of the system python's package set (kernel `exec` snippets, `bootstrap_remote.py`, smoke jobs) and record each as either venv-served or a named exception; the shell build's `--with-system` opt-in is added only if a named exception needs it, default off, and any exception's install is reported as its own route in the report. Live proof on the shared lease: build `workload-repo` (DuckDB on the declared `>=1.5.6,<2.1` line) and the `workload-repo-duckdb` variant clone side by side — the variant is the one place a targeted install remains: its clone's `init_venv.py` builds the default spec, then a recorded delta (`"$UV" pip install --python <variant venv python> duckdb==2.0.0`, named in the registry entry as the variant's delta and in the report as `variant_delta`) moves that one venv to the conformance substrate; fingerprint both venvs' version maps, rebuild one, and show the other's fingerprint and the system python's versions are unchanged. Close with the fresh-session leg: on the next new lease, job zero builds both clones from empty `/content` state with no manual steps.

files: `snippets/env_build.sh`, `snippets/bootstrap_remote.py`, `config/repos.json`, `tests/test_env_build_unit.py`
verify: the coexistence and rebuild reports filed under `results/`, checked against the pre-build fingerprints; `~/.local/bin/uvx pytest tests/` green.
done when: two conflicting DuckDB specs hold simultaneously on one session with no cross-talk, and the from-scratch rebuild on a fresh session passes both clones' asserts.

### Step 7 — Docs, AGENTS, and workflow alignment

Rewrite the `AGENTS.md` shell-channel bullet: the two pip-era image defects (pip-less venv, inherited-packages install skip) are recorded as history of the retired route, replaced by the `uv` rules (absolute-path resolution, isolation as an assert, registry-driven selection). `docs/lease-workflow.md` job-zero and shell sections describe the `init_venv.py`-primary flow (Unit 0 guarantee, pre-clean, drive, asserts), the pointer registry, variant clones and their delta installs, the per-session rebuild boundary, and the one-heavy-gate rule for proof work; `README.md`'s status and snippet descriptions follow. No behavior changes in this step.

files: `AGENTS.md`, `docs/lease-workflow.md`, `README.md`
verify: `~/.local/bin/uvx pytest tests/` (full suite) and a grep pass showing no doc still instructs the system-install or inherited-venv route as current practice.
done when: the docs describe the shipped provisioning exactly, and the full suite is green.

## Open questions

- [x] Where does each repo's venv live: inside its clone (`/content/<repo>/.venv`, today's convention and the fresh-clone precedent) or under a shared root (`/content/.venvs/<repo>`)?
  Recommendation: inside the clone — clone and venv already move, reset, and get rebuilt together (`git reset --hard` in the bundle apply leaves the untracked `.venv` intact, and no step runs `git clean -x`), every existing consumer already resolves the interpreter there, and a shared root adds a second lifetime to keep in sync for no measured benefit.
  <!-- wb-question
  id=venv-location
  type=choice
  options=inside clone | shared venv root
  recommended=inside clone
  answer=inside clone
  -->
- [x] What happens to the system-wide install: opt-in only, retired entirely, or kept as the default?
  Recommendation: opt-in only (`--with-system`, default off, Step 6 adds it only for a named exception) — kernel-side convenience work is the only known consumer, shell and gate work never needed it, and a default-on system route is precisely what made substrates leak across clones.
  <!-- wb-question
  id=system-install-fate
  type=choice
  options=opt-in only | retire entirely | keep default
  recommended=opt-in only
  answer=opt-in only
  -->
- [x] Is the remote `uv` version pinned, or installed latest and recorded?
  Recommendation: latest via the official installer, with `uv_version` recorded in every report — the local CLI install already floats at a floor (`>= 0.7.1`) rather than a pin, and a substrate divergence caused by a `uv` release would surface in the report before it could be mistaken for a repo result; pin later if drift ever bites.
  <!-- wb-question
  id=uv-version-policy
  type=choice
  options=latest, recorded | pinned version
  recommended=latest, recorded
  answer=latest, recorded
  -->
- [x] Where does a repo's package spec live: in this repo's `config/repos.json`, or in a spec file owned by each target repo?
  Recommendation: repo-owned, executed through its `init_venv.py` — resolved by the owner 2026-11-01 (the `init_venv.py`-primary ruling in `## Rulings`): the spec is only needed after the clone exists, because the build runs the clone's own script; the registry stores pointers and checks, never the spec.
  <!-- wb-question
  id=package-spec-source
  type=choice
  options=colab-runner registry | repo-owned spec file
  recommended=repo-owned spec file
  answer=repo-owned spec file
  -->
- [x] How is workload-repo's `init_venv.py` driven: adapted in place (`--choice 0` from the clone root, Unit 0's seeded `PATH` covering its bare-`PATH` `uv` detection, no torch choice), or does the env build extract its package groups and install them itself?
  Recommendation: adapted drive — resolved by the owner 2026-11-01: the script is interactive only when invoked with no arguments, so the `--choice` drive never prompts, and its broader install set is harmless, many of those packages mirroring the Colab image's system set; extraction would re-create the spec duplication the `init_venv.py`-primary ruling removes.
  <!-- wb-question
  id=workload-init-drive
  type=choice
  options=adapted drive via --choice 0 | extract spec into env build
  recommended=adapted drive via --choice 0
  answer=adapted drive via --choice 0
  -->
- [x] Does the Python twin `snippets/env_build_job.py` stay at parity, or is the shell build canonical and the twin retired?
  Recommendation: shell canonical, twin retired after one parity proof (Step 4) — the shell channel was already ruled the preferred way to build environments on the instance, `shell_job.py` gives shell payloads the runner's heartbeat contract, and twin parity has been a recurring drift source (the twin still lacks the shell build's `--dest`/`--skip-system` surface).
  <!-- wb-question
  id=env-build-twin-fate
  type=choice
  options=shell canonical, twin retired | keep both at parity
  recommended=shell canonical, twin retired
  answer=shell canonical, twin retired
  -->

## Critical files

- `snippets/env_build.sh` — job zero itself: Unit 0, the pre-clean, the `init_venv.py` drive, and the report schema all change here
- `config/repos.json` — the new pointer registry every consumer resolves repo, dest, venv path, invocation, and interpreter from
- `bin/colab-check` — the gate's interpreter and version-spec selection moves from hardcodes to the registry
- `snippets/env_build_job.py` — the twin whose disposition Step 4 settles
- `docs/lease-workflow.md` — the standing operating rules consuming threads run job zero by