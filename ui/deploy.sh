#!/usr/bin/env bash
# ui/deploy.sh — the deploy path for the Colab Lease artifact
# (the lease control UI as a published Library artifact, backed by
# the companion server on 127.0.0.1:8793).
#
#   ui/deploy.sh [--skip-gate]
#   ui/deploy.sh check
#
# `check` is read-only: it compares the repo's artifact sources
# against the deployed copies in the runtime space and reports each
# file in sync or DRIFTED (exit 3 on any drift, including a deployed
# file the repo no longer ships). It never writes.
#
# Steps, fail-fast — red work never deploys:
#   1. gate    the UI server tests + the lease core test files
#   2. deploy  copy the artifact-facing sources (ui/static design
#              source + ui/artifact-brief.md) into the runtime
#              space (LEASE_UI_RUNTIME_DIR — required, set in the
#              operator's environment; the deployed location is
#              machine-local and never named in this repo)
#   3. publish the web-artifact builder pass over the runtime
#              space, via the hatchlet CLI when it is on PATH
#              (HATCHLET env override). Without a builder CLI this
#              step prints the exact builder request needed and
#              exits 4 — the source is deployed but NOT published,
#              and the script says so plainly.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RUNTIME="${LEASE_UI_RUNTIME_DIR:-}"
if [ -z "$RUNTIME" ]; then
  echo "deploy: LEASE_UI_RUNTIME_DIR is not set — point it at the deployed runtime dir" >&2
  exit 2
fi
HATCHLET="${HATCHLET:-hatchlet}"

SKIP_GATE=0
CHECK=0
if [ "${1:-}" = "--skip-gate" ]; then
  SKIP_GATE=1
elif [ "${1:-}" = "check" ]; then
  CHECK=1
elif [ -n "${1:-}" ]; then
  echo "usage: ui/deploy.sh [--skip-gate|check]" >&2
  exit 2
fi

step() { echo; echo "==> deploy: $1"; }

space_json() {
  cat <<'JSON'
{
  "entry": "client/index.html",
  "managedCronJobs": [],
  "name": "Colab Lease",
  "platformVersion": 25,
  "runtime": "typescript",
  "slug": "colab-lease-2"
}
JSON
}

CLIENT_FILES="index.html theme.css app.css app.js"

if [ "$CHECK" -eq 1 ]; then
  DRIFT=0
  for f in $CLIENT_FILES; do
    if [ ! -f "$RUNTIME/client/$f" ]; then
      echo "check: MISSING client/$f (not deployed)"; DRIFT=1
    elif cmp -s "$HERE/static/$f" "$RUNTIME/client/$f"; then
      echo "check: in sync  client/$f"
    else
      echo "check: DRIFTED client/$f"; DRIFT=1
    fi
  done
  for f in "$RUNTIME/client"/*; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    case " $CLIENT_FILES " in
      *" $base "*) ;;
      *) echo "check: DRIFTED client/$base (deployed, not a repo source)"; DRIFT=1 ;;
    esac
  done
  if [ ! -f "$RUNTIME/brief/artifact-brief.md" ]; then
    echo "check: MISSING brief/artifact-brief.md (not deployed)"; DRIFT=1
  elif cmp -s "$HERE/artifact-brief.md" "$RUNTIME/brief/artifact-brief.md"; then
    echo "check: in sync  brief/artifact-brief.md"
  else
    echo "check: DRIFTED brief/artifact-brief.md"; DRIFT=1
  fi
  if [ ! -f "$RUNTIME/space.json" ]; then
    echo "check: MISSING space.json (not deployed)"; DRIFT=1
  elif space_json | cmp -s - "$RUNTIME/space.json"; then
    echo "check: in sync  space.json"
  else
    echo "check: DRIFTED space.json"; DRIFT=1
  fi
  if [ "$DRIFT" -eq 0 ]; then
    echo "check: deployed sources match the repo"
    exit 0
  fi
  echo "check: drift present — run ui/deploy.sh to re-sync" >&2
  exit 3
fi

if [ "$SKIP_GATE" -eq 0 ]; then
  step "gate — UI + lease core tests"
  if ! (cd "$ROOT" && ~/.local/bin/uvx pytest tests/test_ui_server.py tests/test_lease_fake.py tests/test_two_tier_fake.py tests/test_auto_fake.py -q); then
    echo "deploy: gate is RED — not deploying, not publishing" >&2
    exit 1
  fi
else
  step "gate — SKIPPED (--skip-gate)"
fi

step "deploy — sync artifact sources to $RUNTIME"
mkdir -p "$RUNTIME/client" "$RUNTIME/brief"
for f in $CLIENT_FILES; do
  cp "$HERE/static/$f" "$RUNTIME/client/"
done
cp "$HERE/artifact-brief.md" "$RUNTIME/brief/"
space_json > "$RUNTIME/space.json"
echo "deploy: sources synced (client/, brief/, space.json)"

step "publish — builder pass over the runtime space"
if command -v "$HATCHLET" >/dev/null 2>&1; then
  "$HATCHLET" artifact publish --space "$RUNTIME" --slug colab-lease-2
  echo "deploy: published via $HATCHLET"
else
  cat >&2 <<'EOF'
deploy: no builder CLI (hatchlet) on PATH — source is deployed but
NOT published. Run the builder pass with the artifact tool:
  first publish: artifact.create_web_fullstack, name "Colab Lease",
  goal_id goal_e56641726a1f (the builder assigned slug colab-lease-2);
  later updates: artifact.edit on slug colab-lease-2. Either way the
  deployed source in the runtime dir (LEASE_UI_RUNTIME_DIR) is the input
  (client/ is the design source, brief/artifact-brief.md is the
  data-plane contract: server-side actions proxy
  http://127.0.0.1:8793).
EOF
  exit 4
fi
