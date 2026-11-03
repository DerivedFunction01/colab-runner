#!/usr/bin/env bash
# Render the lease UI's systemd unit from its template (@ROOT@ ->
# the repo root derived from this script's location, @PYTHON@ ->
# the system python3 the UI has always run under, @HOME@ -> the
# parent of the workspace dir) and install it into the systemd
# unit dir. Idempotent: re-rendering over an installed unit is a
# no-op in content when nothing changed. systemd here is the
# SYSTEM instance (there is no user bus on this machine), the
# same as the sibling companion servers.
#
# Test seams (env): COLAB_UI_UNIT_DIR, COLAB_UI_SYSTEMCTL.
set -euo pipefail

UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$UI_DIR")"
PYTHON="${COLAB_UI_PYTHON:-/usr/bin/python3}"
UNIT_NAME="colab-lease-ui.service"
UNIT_DIR="${COLAB_UI_UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${COLAB_UI_SYSTEMCTL:-systemctl}"
HOME_DIR="$(dirname "$(dirname "$ROOT")")"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed -e "s|@ROOT@|$ROOT|g" -e "s|@PYTHON@|$PYTHON|g" -e "s|@HOME@|$HOME_DIR|g" \
    "$UI_DIR/colab-lease-ui.service.tmpl" > "$tmp"

mkdir -p "$UNIT_DIR"
if [[ -f "$UNIT_DIR/$UNIT_NAME" ]] && cmp -s "$tmp" "$UNIT_DIR/$UNIT_NAME"; then
  echo "install-unit: $UNIT_NAME already installed (unchanged)"
  exit 0
fi
cp "$tmp" "$UNIT_DIR/$UNIT_NAME"
chmod 644 "$UNIT_DIR/$UNIT_NAME"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable "$UNIT_NAME" >/dev/null 2>&1 || true
echo "install-unit: installed $UNIT_NAME for ROOT=$ROOT PYTHON=$PYTHON"
