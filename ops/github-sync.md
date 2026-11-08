# GitHub sync — git over SSH (both routes proven 2026-11-03)

The primary sync between this repo and its private GitHub remote is real
git over SSH: history, deletions, binaries, and executable modes all
travel with the push. The connector snapshot flow is the fallback only —
it carries none of those (a snapshot restore needs `chmod +x` on the
executables before anything runs).

Route preference (standing rule, 2026-11-08): pushes and syncs run
from this machine — directly when shell writes are healthy, or
through the scheduler ops route (`bin/sched exec`, whose server
process the write-denial flap cannot reach) while the flap is up.
Route B below is the last resort, not a standing route: it stays
available per use case when the work already exists inside a Colab
session or the local routes genuinely cannot do the job.

## Key and host pinning

- One Ed25519 keypair, registered at account level on GitHub. The
  private half lives outside the repo and is never copied into tracked
  files, logs, or chat; it is identified by fingerprint only.
- GitHub's host keys are pinned, never trust-on-first-use. Verify any
  fresh scan against GitHub's published fingerprints before pinning:
  - RSA `SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s`
  - ECDSA `SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM`
  - Ed25519 `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`
- Agent shells may resolve ssh's home differently from the login shell
  (the passwd entry wins over `$HOME`), so this repo's git config pins
  the transport explicitly: `core.sshCommand` names the key and the
  verified known-hosts file by absolute path. Bare `git push` then
  works from any shell.

## Route A — push from this machine (the standing route)

1. Commit locally as usual.
2. `git push origin main` — directly, or via `bin/sched exec` while
   the write-denial flap is up.
3. Verify with `git ls-remote origin main` when the push matters.

## Route B — push from inside an active Colab session (last resort)

Rides a session that is already active (check status first and yield to
its work, per the shared-session etiquette); spends no lease of its own.

1. Build a git bundle of the commits to push and assemble the kit: the
   bundle, the private key, and the pinned known-hosts file.
2. Upload the kit into the session through the guarded `colab()` funnel
   to a work directory under `/content/` (create the directory first —
   uploads fail when the parent does not exist).
3. In the session: clone from the bundle, do the git work, and push
   with the planted key named explicitly —
   `ssh -i <key> -o UserKnownHostsFile=<known_hosts> -o StrictHostKeyChecking=yes`.
   Never rely on the runtime's default ssh identity; it is the
   session-bridge key, not the GitHub key. The runtime's git is 2.43
   and outbound port 22 is open.
4. An EXIT trap shreds the key and removes the work directory on every
   exit path, success or failure; verify both are absent afterwards.

Script-delivery gotcha: a script fed to `bash -s` on stdin is truncated
if any command in it drains stdin. Wrap the body in a function invoked
with `</dev/null`, and pass `-n` to any `ssh` probe inside it.

## Clone / restore notes

- A fresh clone is executable-complete: git carries the modes (24
  files at `100755` at the time of writing).
- Local-only state is not in the repo by design and must be seeded
  from the workspace after a clone: `config/callers.json` (the local
  caller registry), plus the runtime state under `var/` and
  `state/` when the clone is meant to operate, not just build.
- History note: on 2026-11-03 the history was rewritten from inside a
  Colab session (Route B's first push) to strip a local config file
  from all commits; the tip tree was unchanged. The pre-rewrite history
  is preserved in a full-history bundle archived outside the repo, and
  the adjacent sync record carries both tips and the bundle checksum.
