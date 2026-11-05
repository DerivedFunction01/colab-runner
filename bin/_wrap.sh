# Shared wrapper plumbing: sourced by bin/colab-* (not executed directly).
# Every wrapper restores transient state on entry and captures it on exit.
# Machine seam (proven 2026-10-29): this VM's egress proxy mangles the CLI's
# empty-body session-assignment POST (Google returns 411 despite a correct
# Content-Length: 0). Colab/Google traffic must bypass the proxy.
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}google.com,googleapis.com,googleusercontent.com,colab.dev"
export no_proxy="${no_proxy:+$no_proxy,}google.com,googleapis.com,googleusercontent.com,colab.dev"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
COLAB_BIN="${COLAB_BIN:-$(for c in "$HOME/.local/share/uv/tools/google-colab-cli/bin/colab" "$(command -v colab || true)" "$HOME/.local/bin/colab"; do [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; break; }; done)}"

wrap_enter() { "$PY" "$REPO_ROOT/ops/mirror_transient.py" restore >/dev/null 2>&1 || true; }
wrap_exit() { "$PY" "$REPO_ROOT/ops/mirror_transient.py" capture >/dev/null 2>&1 || true; }
trap wrap_exit EXIT

default_shape() { # -> the config's default shape id; fails loudly when absent
  "$PY" - "$REPO_ROOT/config/lease.json" <<'EOF'
import json, sys
name = json.load(open(sys.argv[1])).get("default_shape")
if not name:
    print("error: config/lease.json has no default_shape", file=sys.stderr)
    sys.exit(1)
print(name)
EOF
}

plan_for() { # shape id -> two lines: the generated session name, then the `colab new` args after `new`
  # Name generation and shape flags live in the lease core alone;
  # its plan verb is the one place both are derived.
  "$REPO_ROOT/bin/colab-lease" plan --shape "$1" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
print(d["session"])
print(" ".join(d["args"][1:]))
'
}

gate_check() { # shape id -> the session shape gate's admission verdict JSON; exit 0 admitted, 1 refused
  # The wrappers that create sessions bypass the lease core, so they
  # consult its single admission seam (bin/colab-lease) before any
  # CLI call. Prints the verdict JSON either way. The driver-set
  # $COLAB_CALLER claim rides the consult when present, so the
  # verdict and its journal entry name the thread.
  if [ -n "${COLAB_CALLER:-}" ]; then
    "$REPO_ROOT/bin/colab-lease" admission --shape "$1" --caller "$COLAB_CALLER"
  else
    "$REPO_ROOT/bin/colab-lease" admission --shape "$1"
  fi
}
