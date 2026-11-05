#!/usr/bin/env bash
# Restore transient state, ensure the colab CLI is installed at the pinned
# floor, and report login state. Idempotent; safe in a PATH-stripped shell.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  echo "uv not found (looked on PATH, ~/.local/bin, ~/.cargo/bin)" >&2; exit 1
}
UV="$(find_uv)"
PY="$(command -v python3 || echo /usr/bin/python3)"

echo "== transient restore"
"$PY" "$REPO_ROOT/ops/mirror_transient.py" restore || true

# The tool interpreter must be pinned: with a stripped PATH, uv resolved a
# Python 3.11 on this machine and dependency resolution for google-colab-cli
# failed outright (it requires >=3.12). Pin uv tool to a >=3.12 interpreter.
TOOL_PY=""
for c in /usr/bin/python3 "$(command -v python3 || true)"; do
  [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null && { TOOL_PY="$c"; break; }
done
[ -z "$TOOL_PY" ] && TOOL_PY="$("$UV" python find 3.12)"
echo "tool python: $TOOL_PY ($("$TOOL_PY" --version 2>&1))"
MIN_VER="$(python3 -c "import json; print([i['min_version'] for i in json.load(open('$REPO_ROOT/config/transient.json'))['items'] if i['id']=='colab-tool'][0])")"
PKG="$(python3 -c "import json; print([i['package'] for i in json.load(open('$REPO_ROOT/config/transient.json'))['items'] if i['id']=='colab-tool'][0])")"
colab_bin() { for c in "$HOME/.local/share/uv/tools/google-colab-cli/bin/colab" "$(command -v colab || true)" "$HOME/.local/bin/colab"; do [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }; done; }
COLAB="$(colab_bin || true)"
ver_of() { "$1" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true; }
CUR=""
[ -n "$COLAB" ] && CUR="$(ver_of "$COLAB" || true)"
if [ -z "$CUR" ]; then
  echo "== installing $PKG via uv tool"
  "$UV" tool install --python "$TOOL_PY" "$PKG"
  COLAB="$(colab_bin || true)"; CUR="$(ver_of "$COLAB" || true)"
elif [ "$(printf '%s\n%s\n' "$MIN_VER" "$CUR" | sort -V | head -1)" != "$MIN_VER" ]; then
  echo "== upgrading $PKG ($CUR < $MIN_VER)"
  "$UV" tool upgrade "$PKG" || "$UV" tool install --force --python "$TOOL_PY" "$PKG"
  CUR="$(ver_of "$COLAB" || true)"
fi
echo "colab: ${COLAB:-not found} version=${CUR:-unknown} (floor $MIN_VER)"

echo "== login state (presence only)"
for f in "$HOME/.config/colab-cli/token.json" "$HOME/.config/colab-cli/sessions.json"; do
  [ -f "$f" ] && echo "present: $f" || echo "absent:  $f"
done
if [ -f "$REPO_ROOT/state/transient/cron-registered.json" ]; then
  echo "restore cron: registered (marker present)"
else
  echo "restore cron: NOT registered yet — see ops/restore-cron.md"
fi

echo "== lease machinery"
[ -f "$REPO_ROOT/config/lease.json" ] && echo "present: config/lease.json" || echo "absent:  config/lease.json"
[ -f "$REPO_ROOT/config/profiles.json" ] && echo "present: config/profiles.json" || echo "absent:  config/profiles.json — copy config/profiles.example.json there and edit it for this workspace"
"$PY" -m py_compile "$REPO_ROOT/sync/lease.py" "$REPO_ROOT/bin/colab-lease" && echo "lease module + CLI compile OK"
[ -f "$REPO_ROOT/var/lease.json" ] && echo "lease state: a lease record exists (see bin/colab-lease status)" || echo "lease state: no active lease"
