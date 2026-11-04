#!/usr/bin/env bash
# Start/stop the lease control UI server.
#   ui/ui.sh start|stop|restart|status
# When the systemd unit is installed (ui/install-unit.sh), every
# verb delegates to systemctl — the unit is the supervisor, and a
# second nohup instance must never race it for the port. Without
# the unit, the original nohup + pidfile path applies; the pidfile
# and log live in the lease var dir (COLAB_RUNNER_VAR, default
# <repo>/var) alongside the lease state they control.
# Test seams (env): COLAB_UI_UNIT_DIR, COLAB_UI_SYSTEMCTL.
set -euo pipefail

UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$UI_DIR")"
# The panel's actor identity for the core's session shape gate
# (the systemd unit carries the same claim in its Environment).
export COLAB_LEASE_ACTOR=panel
VAR_DIR="${COLAB_RUNNER_VAR:-$REPO/var}"
PIDFILE="$VAR_DIR/lease-ui.pid"
LOG="$VAR_DIR/lease-ui.log"
PORT="${COLAB_LEASE_UI_PORT:-8793}"
UNIT_NAME="colab-lease-ui.service"
UNIT_DIR="${COLAB_UI_UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${COLAB_UI_SYSTEMCTL:-systemctl}"

unit_installed() {
  [[ -f "$UNIT_DIR/$UNIT_NAME" ]]
}

service_active() {
  "$SYSTEMCTL" is-active --quiet "$UNIT_NAME" 2>/dev/null
}

ui_pid() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s' "$pid"
      return 0
    fi
  fi
  return 1
}

case "${1:-}" in
  start)
    if unit_installed; then
      if service_active; then
        echo "lease-ui already running (systemd $UNIT_NAME) on http://127.0.0.1:$PORT"
        exit 0
      fi
      "$SYSTEMCTL" start "$UNIT_NAME"
      sleep 1
      if service_active; then
        echo "lease-ui started (systemd $UNIT_NAME) on http://127.0.0.1:$PORT — log: $LOG"
      else
        echo "lease-ui failed to start via systemd; see $LOG" >&2
        exit 1
      fi
      exit 0
    fi
    if pid="$(ui_pid)"; then
      echo "lease-ui already running (pid $pid) on http://127.0.0.1:$PORT"
      exit 0
    fi
    mkdir -p "$VAR_DIR"
    nohup /usr/bin/python3 "$UI_DIR/server.py" >>"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    if pid="$(ui_pid)"; then
      echo "lease-ui started (pid $pid) on http://127.0.0.1:$PORT — log: $LOG"
    else
      echo "lease-ui failed to start; see $LOG" >&2
      exit 1
    fi
    ;;
  stop)
    if unit_installed; then
      if service_active; then
        "$SYSTEMCTL" stop "$UNIT_NAME"
        echo "lease-ui stopped (systemd $UNIT_NAME)"
      else
        echo "lease-ui is not running"
      fi
      rm -f "$PIDFILE"
      exit 0
    fi
    if pid="$(ui_pid)"; then
      kill "$pid"
      rm -f "$PIDFILE"
      echo "lease-ui stopped (pid $pid)"
    else
      echo "lease-ui is not running"
      rm -f "$PIDFILE"
    fi
    ;;
  restart)
    # Brief pause first: when the restart is triggered over HTTP
    # (POST /api/server/restart), the requesting server is this very
    # process — the pause lets its response land before the stop does.
    sleep 1
    if unit_installed; then
      "$SYSTEMCTL" restart "$UNIT_NAME"
      echo "lease-ui restarted (systemd $UNIT_NAME)"
      exit 0
    fi
    "$0" stop
    "$0" start
    ;;
  status)
    if unit_installed; then
      if service_active; then
        echo "lease-ui running (systemd $UNIT_NAME) on http://127.0.0.1:$PORT"
      else
        echo "lease-ui not running"
      fi
      exit 0
    fi
    if pid="$(ui_pid)"; then
      echo "lease-ui running (pid $pid) on http://127.0.0.1:$PORT"
    else
      echo "lease-ui not running"
    fi
    ;;
  *)
    echo "usage: $0 start|stop|restart|status" >&2
    exit 2
    ;;
esac
