# Shared wrapper plumbing: sourced by bin/colab-* (not executed directly).
# Every wrapper restores transient state on entry and captures it on exit.
# Machine seam (proven 2026-10-29): this VM's egress proxy mangles the CLI's
# empty-body session-assignment POST (Google returns 411 despite a correct
# Content-Length: 0). Colab/Google traffic must bypass the proxy.
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}google.com,googleapis.com,googleusercontent.com,colab.dev"
export no_proxy="${no_proxy:+$no_proxy,}google.com,googleapis.com,googleusercontent.com,colab.dev"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
COLAB_BIN="${COLAB_BIN:-$(for c in "$(command -v colab || true)" "$HOME/.local/bin/colab"; do [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; break; }; done)}"

wrap_enter() { "$PY" "$REPO_ROOT/ops/mirror_transient.py" restore >/dev/null 2>&1 || true; }
wrap_exit() { "$PY" "$REPO_ROOT/ops/mirror_transient.py" capture >/dev/null 2>&1 || true; }
trap wrap_exit EXIT

profile_args() { # profile name -> CLI args for `colab new`
  "$PY" - "$REPO_ROOT/config/profiles.json" "${1:-workload}" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
p = cfg["profiles"][sys.argv[2]]
args = ["-s", p["session"]]
if p.get("high_mem"): args.append("--high-mem")
if p.get("gpu"): args += ["--gpu", p["gpu"]]
if p.get("tpu"): args += ["--tpu", p["tpu"]]
print(" ".join(args))
EOF
}
profile_field() { # profile name, field -> value
  "$PY" -c "import json,sys; print(json.load(open('$REPO_ROOT/config/profiles.json'))['profiles'][sys.argv[1]][sys.argv[2]])" "${1:-workload}" "$2"
}
