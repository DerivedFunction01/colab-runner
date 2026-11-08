# Transient restore — runbook and specification

Findings first: `config/transient.json` is the specification and
`ops/mirror_transient.py` is the mechanism. Everything else points
here. File contents are never printed or logged; capture, restore,
and status report only existence, size, mtime, and the action taken.

## Registry and operations

The registry names each item's canonical path, mirror path under
`state/transient/`, kind, mode, conflict rule, triggers, and any
verification or validation rule. The mirror script performs three
operations against it:

- `capture` — canonical → mirror.
- `restore` — mirror → canonical.
- `status` — metadata only, no copies.

Copies are atomic (temporary sibling + rename), carry the registry
mode, and preserve the source mtime so newest-wins comparisons stay
honest. A `recreate` item (the CLI install) is not file-mirrored;
bootstrap recreates it from its pin when missing or below the
registry minimum.

## Conflict rules

- `newest-wins` items copy only when the source is newer.
- `restore-if-missing` items restore only into an absent canonical
  path and capture the canonical file when it exists.
- The token item is validated on both operations: an invalid
  canonical heals from a valid mirror during capture, and an
  invalid mirror never overwrites the canonical file during
  restore, whatever the mtimes say.
- The sessions item never captures an empty binding over a
  non-empty mirror.
- Disabled registry items are skipped.

## Triggers

- Wrappers restore on entry and capture on exit.
- The reaper hook's restore duty runs the same restore on its poll.
- Bootstrap restores, recreates the CLI item when needed, and
  reports whether a restore trigger is registered.
- The cron line below is the standing trigger on a machine with
  cron. On this machine the registered trigger is the script-level
  reaper hook; the marker `state/transient/cron-registered.json`
  records that a trigger is registered, whatever the mechanism.

## Cron definition (install after the first successful login)

Substitute the checkout's absolute path for `/path/to/colab-runner`
(cron needs absolute paths; the installed path is machine-local and
lives only in the crontab, never in this repo):

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

Verify: `crontab -l | grep mirror_transient` shows the line; after
deleting the canonical token file, it reappears (mode 600,
hash-equal to the mirror) within 5 minutes.
