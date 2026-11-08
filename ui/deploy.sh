#!/usr/bin/env bash
# ui/deploy.sh — deploy or check the Colab Lease artifact.
# Contract and deploy story: ui/README.md. `check` is read-only and
# exits 3 on any drift. A full deploy gates first, syncs sources to
# $LEASE_UI_RUNTIME_DIR, then publishes via the hatchlet CLI when
# present; without one it prints the builder request and exits 4.
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

CLIENT_FILES="index.html theme.css app.css app.js colab-icon.svg"
# Generated icon PNGs (ui/make-icon-png.sh) ship when they exist; the
# SVG is the tracked source and the only required icon file.
OPTIONAL_CLIENT_FILES="colab-icon-192.png colab-icon-512.png colab-icon-180.png"

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
  for f in $OPTIONAL_CLIENT_FILES; do
    if [ -f "$HERE/static/$f" ] && [ ! -f "$RUNTIME/client/$f" ]; then
      echo "check: MISSING client/$f (generated locally, not deployed)"; DRIFT=1
    elif [ -f "$HERE/static/$f" ] && ! cmp -s "$HERE/static/$f" "$RUNTIME/client/$f"; then
      echo "check: DRIFTED client/$f"; DRIFT=1
    fi
  done
  for f in "$RUNTIME/client"/*; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    case " $CLIENT_FILES $OPTIONAL_CLIENT_FILES " in
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
for f in $OPTIONAL_CLIENT_FILES; do
  if [ -f "$HERE/static/$f" ]; then
    cp "$HERE/static/$f" "$RUNTIME/client/"
  fi
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
  (client/ is the design source; brief/artifact-brief.md points to
  the data-plane contract in ui/README.md).
EOF
  exit 4
fi
