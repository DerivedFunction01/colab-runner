#!/usr/bin/env bash
# Start/stop the lease control UI server.
#   ui/ui.sh start|stop|restart|status
# The pidfile and log live in the lease var dir (COLAB_RUNNER_VAR,
# default <repo>/var) alongside the lease state they control.
set -euo pipefail

UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$UI_DIR")"
VAR_DIR="${COLAB_RUNNER_VAR:-$REPO/var}"
PIDFILE="$VAR_DIR/lease-ui.pid"
LOG="$VAR_DIR/lease-ui.log"
PORT="${COLAB_LEASE_UI_PORT:-8793}"

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
    "$0" stop
    "$0" start
    ;;
  status)
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
