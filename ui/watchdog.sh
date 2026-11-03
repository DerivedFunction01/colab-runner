#!/usr/bin/env bash
# Lease UI watchdog — the restore ladder for the panel's companion
# server (ui/server.py on 127.0.0.1:8793), in the sibling servers'
# shape (scheduler ops/watchdog.sh, workbench ops/watchdog.sh).
#
# Why this exists: the server was started by ui.sh under nohup with
# no supervision. Runtime deployments replace this VM (PID 1
# restarts, /etc is wiped, every unsupervised process dies) —
# on 2026-11-02 that killed this server at least four times
# (12:41, 14:39, 16:50, 22:02 EST), each time alongside the USMC
# serve and the workbench viewer, each time with the server log
# ending mid-traffic on a 200 and no fatal traceback: the process
# never crashed, it was annihilated with the VM. The reaper hook
# restarted it by hand each time and woke the operator each time.
#
# The ladder:
#   1. Healthy probe (any HTTP response from the static page).
#      Healthy but the systemd unit file is missing (a VM
#      replacement wipes /etc while a process may live on, or the
#      unit was never installed): reinstall it quietly.
#   2. Down: (re)install the unit from the tracked template,
#      start the service under systemd (Restart=always then
#      guards it within the VM's lifetime), poll-verify.
#   3. Fallback: ui.sh start (the pre-systemd nohup path), then
#      poll-verify again.
# Exit 0: server healthy (or restored and verified).
# Exit 1: server down and every ladder rung failed — the caller
#         (the reaper hook's UI duty) escalates/wakes.
#
# Test seams (env): COLAB_UI_UNIT_DIR, COLAB_UI_SYSTEMCTL,
# COLAB_UI_URL, COLAB_UI_POLL_SECS.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="colab-lease-ui.service"
UNIT_DIR="${COLAB_UI_UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${COLAB_UI_SYSTEMCTL:-systemctl}"
URL="${COLAB_UI_URL:-http://127.0.0.1:8793/}"
POLL_SECS="${COLAB_UI_POLL_SECS:-60}"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"

healthy() {
  curl -s --max-time 5 -o /dev/null "$URL"
}

wait_healthy() {
  local deadline=$((SECONDS + POLL_SECS))
  while (( SECONDS < deadline )); do
    if healthy; then
      return 0
    fi
    sleep 2
  done
  healthy
}

if healthy; then
  if [[ ! -f "$UNIT_FILE" ]]; then
    if bash "$UI_DIR/install-unit.sh"; then
      echo "watchdog: server healthy; unit file was missing, reinstalled"
    else
      echo "watchdog: server healthy but unit reinstall failed" >&2
      exit 1
    fi
  fi
  exit 0
fi

echo "watchdog: lease UI server down; running restore ladder" >&2

# Rung 1: systemd supervision.
if bash "$UI_DIR/install-unit.sh"; then
  "$SYSTEMCTL" start "$UNIT_NAME" 2>/dev/null || true
  if wait_healthy; then
    echo "watchdog: restored via systemd and verified healthy" >&2
    exit 0
  fi
fi

# Rung 2: the pre-systemd direct start.
timeout 60 bash "$UI_DIR/ui.sh" start 2>/dev/null || true
if wait_healthy; then
  echo "watchdog: restored via ui.sh fallback and verified healthy" >&2
  exit 0
fi

echo "watchdog: ladder failed; server still unhealthy after polling" >&2
exit 1
