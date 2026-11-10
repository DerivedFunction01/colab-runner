#!/usr/bin/env bash
# Generic unit installer: render the repo's systemd unit
# template and install it, driven entirely by the tracked
# config beside this script (unit.conf — template name,
# value source, interpreter and home modes, seam env names,
# check mode). A repo adopting this script copies it and
# writes its own unit.conf; the script refuses to run
# without one. Idempotent: re-rendering over an installed
# unit with unchanged content is a no-op. Modes:
#   (none)    install the current render
#   --check   CHECK_MODE=exit: exit 0 when the installed
#             unit matches the render, 1 with a message
#             when missing or stale. CHECK_MODE=print:
#             print the three-state vocabulary "missing",
#             "stale" or "current", always exit 0.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
CONF="$SCRIPT_DIR/unit.conf"
if [[ ! -f "$CONF" ]]; then
  echo "install-unit: unit.conf missing beside the script" >&2
  exit 2
fi
# shellcheck disable=SC1090
. "$CONF"

case "${PYTHON_MODE:-system}" in
  venv) PYTHON="$ROOT/var/venv/bin/python"
        [[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)" ;;
  dotvenv) PYTHON="$ROOT/.venv/bin/python"
        [[ -x "$PYTHON" ]] || PYTHON="/usr/bin/python3" ;;
  *) PYTHON="/usr/bin/python3" ;;
esac
if [[ -n "${PYTHON_ENV:-}" && -n "${!PYTHON_ENV:-}" ]]; then
  PYTHON="${!PYTHON_ENV}"
fi

BINDINGS=()
if [[ -n "${VALUES_MODULE:-}" ]]; then
  VALUES_OUT="$(cd "$ROOT" && "$PYTHON" -m $VALUES_MODULE)"
  eval "$VALUES_OUT"
  for key in ${REQUIRE_KEYS:-}; do
    if [[ -z "${!key:-}" ]]; then
      echo "install-unit: settings values unavailable (build the venv first: python3 init_venv.py)" >&2
      exit 1
    fi
  done
  while IFS='=' read -r key _; do
    [[ "$key" =~ ^[A-Z_]+$ ]] && BINDINGS+=("$key")
  done <<< "$VALUES_OUT"
fi

UNIT_FILE_NAME="${UNIT_FILE:-}"
if [[ -z "$UNIT_FILE_NAME" ]]; then
  UNIT_FILE_NAME="${UNIT:-}.service"
fi
case "${HOME_MODE:-user}" in
  workspace-parent) HOME_VALUE="$(dirname "$(dirname "$ROOT")")" ;;
  *) HOME_VALUE="$HOME" ;;
esac
UNIT_DIR="/etc/systemd/system"
if [[ -n "${UNIT_DIR_ENV:-}" && -n "${!UNIT_DIR_ENV:-}" ]]; then
  UNIT_DIR="${!UNIT_DIR_ENV}"
fi
SYSTEMCTL="systemctl"
if [[ -n "${SYSTEMCTL_ENV:-}" && -n "${!SYSTEMCTL_ENV:-}" ]]; then
  SYSTEMCTL="${!SYSTEMCTL_ENV}"
fi
UNIT_PATH="$UNIT_DIR/$UNIT_FILE_NAME"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
SED_ARGS=(-e "s|@ROOT@|$ROOT|g" -e "s|@PYTHON@|$PYTHON|g"
          -e "s|@HOME@|$HOME_VALUE|g")
for key in "${BINDINGS[@]}"; do
  SED_ARGS+=(-e "s|@$key@|${!key}|g")
done
sed "${SED_ARGS[@]}" "$SCRIPT_DIR/$TEMPLATE" > "$tmp"

if [[ "${1:-}" == "--check" ]]; then
  if [[ "${CHECK_MODE:-exit}" == "print" ]]; then
    if [[ ! -f "$UNIT_PATH" ]]; then
      echo "missing"
    elif cmp -s "$tmp" "$UNIT_PATH"; then
      echo "current"
    else
      echo "stale"
    fi
    exit 0
  fi
  if [[ -f "$UNIT_PATH" ]] && cmp -s "$tmp" "$UNIT_PATH"; then
    echo "install-unit: $UNIT_FILE_NAME matches the current render"
    exit 0
  fi
  echo "install-unit: $UNIT_FILE_NAME missing or differs from the current render"
  exit 1
fi

mkdir -p "$UNIT_DIR"
if [[ -n "${STATE_DIR:-}" ]]; then
  mkdir -p "$ROOT/$STATE_DIR"
fi
if [[ -f "$UNIT_PATH" ]] && cmp -s "$tmp" "$UNIT_PATH"; then
  echo "install-unit: $UNIT_FILE_NAME already installed (unchanged)"
  exit 0
fi
cp "$tmp" "$UNIT_PATH"
chmod 644 "$UNIT_PATH"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable "$UNIT_FILE_NAME" >/dev/null 2>&1 || true
echo "install-unit: installed $UNIT_FILE_NAME for ROOT=$ROOT PYTHON=$PYTHON"
