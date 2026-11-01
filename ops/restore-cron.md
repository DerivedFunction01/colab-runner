# Restore cron — definition (installed 2026-10-29, after login)

Purpose: keep transient files (auth token, session list) restored from the
repo mirror even if nothing invokes a wrapper — e.g. after a VM reset wipes
`~/.config/colab-cli/`. Runs the mirror restore only; it never logs in and
never prints file contents.

## Crontab line (install after the first successful `colab` login)

Substitute the checkout's absolute path for `/path/to/colab-runner`
(cron needs absolute paths; the path itself is machine-local and
lives only in the installed crontab, never in this repo):

```
*/5 * * * * /usr/bin/python3 /path/to/colab-runner/ops/mirror_transient.py restore >> /path/to/colab-runner/var/restore-cron.log 2>&1
```

Install (from the checkout):

```
( crontab -l 2>/dev/null | grep -v 'colab-runner/ops/mirror_transient.py' ; \
  echo "*/5 * * * * /usr/bin/python3 $PWD/ops/mirror_transient.py restore >> $PWD/var/restore-cron.log 2>&1" \
) | crontab -
mkdir -p var
touch state/transient/cron-registered.json
```

Verify: `crontab -l | grep mirror_transient` shows the line; after deleting
`~/.config/colab-cli/token.json`, it reappears (mode 600, hash-equal to the
mirror) within 5 minutes.

Why staged: before the first login there is no token to mirror, so the job
would only log `no-mirror` every 5 minutes. `ops/bootstrap_local.sh` reports
registration state and re-offers install once a token exists.

## As installed on this machine (2026-10-29)

This VM has no `crontab` binary and no user systemd bus. The trigger was at
first a Muse scheduled job (`colab-runner-transient-restore`, every 5
minutes, restore + reap, report only on failure); it was found silently
disabled on 2026-10-31 after a dead lease went unreaped for ~5 hours, and
was retired once its replacement proved duty-equivalent. The trigger now
is the script-level hook `colab-lease-reaper` (120 s poll), whose first
duty is the same restore command. The crontab line above remains the
definition for a machine that has cron. The marker file
`state/transient/cron-registered.json` records that a trigger is
registered (any mechanism counts).
