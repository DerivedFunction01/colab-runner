#!/usr/bin/env bash
# Job zero, shell edition: build one registered repo's environment on the
# VM — driven entirely by bash over the SSH shell channel (bin/colab-sh),
# with no kernel and no Python snippet job. Runs on the VM only.
#
# Repo-agnostic: nothing here names a target repo. The repo comes from a
# registry (config/repos.json locally; /content/repos.json on the VM —
# upload it before the build): clone URL, dest, declared venv path, the
# clone's own init_venv.py invocation, version checks, import-scan roots,
# and optional variant deltas. The clone's init_venv.py is the builder
# (it owns the package spec); this script owns what it does not: the uv
# install guarantee (Unit 0), pre-cleaning a legacy inherited venv the
# script would otherwise reuse, the isolation + version asserts, the
# import scan, and the env_report.json contract.
#
# Usage: env_build.sh [--repo ID] [--variant NAME] [--registry FILE]
#                     [--dest DIR] [--out DIR] [--skip-clone] [--skip-system]
#   --repo       registry repo id (default: the registry's default_repo)
#   --variant    build the <dest>-<variant> clone; the entry's variant
#                delta is applied to its venv after init (variant_delta)
#   --dest       clone/venv location (default /content/<dest slug>)
#   --out        report directory (default /content/env-build-out-shell)
#   --skip-clone reuse the tree already at --dest as-is
#   --skip-system accepted for compatibility; there is no system install
#                by default (or at all) in this build
set -uo pipefail

REPO_ID=""
VARIANT=""
REGISTRY=""
DEST=""
OUT="${SHELL_JOB_OUT:-/content/env-build-out-shell}"
SKIP_CLONE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_ID="$2"; shift 2 ;;
    --variant) VARIANT="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --skip-clone) SKIP_CLONE=1; shift ;;
    --skip-system) shift ;;
    --print-uv) PRINT_UV=1; shift ;;
    *) echo "env_build.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done
PRINT_UV="${PRINT_UV:-0}"

# Import name -> distribution name where the two differ (scan installs).
declare -A DIST_NAMES=( [yaml]=pyyaml [PIL]=pillow [sklearn]=scikit-learn
  [bs4]=beautifulsoup4 [cv2]=opencv-python [dotenv]=python-dotenv [jwt]=pyjwt )

# ---- uv resolution (Unit 0 machinery; also --print-uv for probes) ----
# Candidates in order: $COLAB_UV_BIN override, PATH, the per-user install
# dirs. Only a verified absolute path is ever invoked — never bare uv.
resolve_uv() {
  local c
  for c in "${COLAB_UV_BIN:-}" "$(command -v uv 2>/dev/null || true)" \
           "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -n "$c" ] && [ -x "$c" ] && timeout 30 "$c" --version >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

UV=""
UV_ROUTE="present"
UV="$(resolve_uv)" || UV=""
if [ -z "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
  UV="$(resolve_uv)" || UV=""
  UV_ROUTE="installer"
fi
if [ -z "$UV" ]; then
  if [ "$PRINT_UV" = "1" ]; then
    echo '{"uv_path": "", "uv_version": "", "uv_install_route": "failed"}'
  else
    echo "env_build.sh: uv could not be resolved or installed" >&2
  fi
  exit 1
fi
UV_VERSION="$("$UV" --version 2>/dev/null | awk '{print $2}')"
if [ "$PRINT_UV" = "1" ]; then
  python3 -c 'import json,sys; print(json.dumps({"uv_path": sys.argv[1], "uv_version": sys.argv[2], "uv_install_route": sys.argv[3]}))' "$UV" "$UV_VERSION" "$UV_ROUTE"
  exit 0
fi
UV_DIR="$(dirname "$UV")"

# ---- Registry resolution ----
if [ -z "$REGISTRY" ]; then
  for cand in "${ENV_BUILD_REGISTRY:-}" "/content/repos.json" \
              "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/config/repos.json" \
              "config/repos.json"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then REGISTRY="$cand"; break; fi
  done
fi
if [ -z "$REGISTRY" ] || [ ! -f "$REGISTRY" ]; then
  echo "env_build.sh: no repo registry found (pass --registry FILE; on the VM, upload config/repos.json to /content/repos.json)" >&2; exit 2
fi
ENTRY_JSON="$(python3 - "$REGISTRY" "$REPO_ID" "$VARIANT" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
repos = data.get("repos") or {}
rid = sys.argv[2] or data.get("default_repo")
if not rid or rid not in repos:
    sys.exit(f"env_build: repo {rid!r} not registered (have: {sorted(repos)})")
entry = repos[rid]
forbidden = {"packages", "package_list", "pkgs", "package_spec", "spec",
             "requirements", "dependencies", "install"} & set(entry)
if forbidden:
    sys.exit(f"env_build: registry entry {rid!r} carries {sorted(forbidden)}; specs live in init_venv.py")
variant = sys.argv[3]
delta = []
if variant:
    vent = (entry.get("variants") or {}).get(variant)
    if vent is None:
        sys.exit(f"env_build: variant {variant!r} not defined for repo {rid!r}")
    delta = list(vent.get("delta") or [])
out = {
    "id": rid, "clone_url": entry["clone_url"], "dest": entry["dest"],
    "venv": entry["venv"], "init_argv": entry["init"]["argv"],
    "init_cwd": entry["init"].get("cwd", "clone"),
    "version_checks": entry.get("version_checks") or {},
    "scan_roots": entry.get("scan_roots") or [],
    "package": entry.get("package"),
    "derive_resources": entry.get("derive_resources"),
    "variant": variant or None, "variant_delta": delta,
}
print(json.dumps(out))
PYEOF
)" || exit 2
jget() { python3 -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print(v if isinstance(v,str) else json.dumps(v))' "$ENTRY_JSON" "$1"; }
REPO_ID="$(jget id)"
CLONE_URL="$(jget clone_url)"
DEST_SLUG="$(jget dest)"
if [ -n "$VARIANT" ]; then DEST_SLUG="$DEST_SLUG-$VARIANT"; fi
VENV_REL="$(jget venv)"
[ -z "$DEST" ] && DEST="/content/$DEST_SLUG"
VENV_DIR="$DEST/$VENV_REL"
VENV_PY="$VENV_DIR/bin/python"

mkdir -p "$OUT"
KV="$OUT/report.kv"
: > "$KV"
kv() { echo "$1=$2" >> "$KV"; }
beat() { echo "$(date +%H:%M:%S) unit$2 $1"; }
fail() { beat "FAILED: $1" "$2"; write_report; exit 1; }

write_report() {
  python3 - "$KV" "$OUT/env_report.json" <<'PYEOF'
import json, sys
kv = {}
for line in open(sys.argv[1]):
    if "=" in line:
        k, v = line.rstrip("\n").split("=", 1)
        kv[k] = v
def as_bool(s):
    return s == "true"
def as_json(s, default):
    try:
        return json.loads(s)
    except Exception:
        return default
report = {
    "repo": kv.get("repo", ""),
    "variant": as_json(kv.get("variant", "null"), None),
    "dest": kv.get("dest", ""),
    "vm_python": kv.get("vm_python", ""),
    "cloned_head": kv.get("cloned_head", ""),
    "cloned_tree": kv.get("cloned_tree", ""),
    "uv_path": kv.get("uv_path", ""),
    "uv_version": kv.get("uv_version", ""),
    "uv_install_route": kv.get("uv_install_route", ""),
    "init_route": kv.get("init_route", ""),
    "venv_python": kv.get("venv_python", ""),
    "venv_rebuilt_legacy": as_bool(kv.get("venv_rebuilt_legacy", "")),
    "venv_inherits_system_packages": as_bool(kv.get("venv_inherits_system_packages", "")),
    "venv_isolated": as_bool(kv.get("venv_isolated", "")),
    "variant_delta": as_json(kv.get("variant_delta", "[]"), []),
    "versions": as_json(kv.get("versions", "{}"), {}),
    "versions_fingerprint": kv.get("versions_fingerprint", ""),
    "system_version_map_before": as_json(kv.get("system_version_map_before", "{}"), {}),
    "system_version_map_after": as_json(kv.get("system_version_map_after", "{}"), {}),
    "import_scan": {
        "modules": int(kv.get("import_scan_modules", "0") or 0),
        "missing_before": as_json(kv.get("missing_before", "[]"), []),
        "missing_after": as_json(kv.get("missing_after", "[]"), []),
    },
    "asserts": {k[7:]: v for k, v in kv.items() if k.startswith("assert_")},
    "assertions_passed": as_bool(kv.get("assertions_passed", "")),
}
try:
    report["derive_resources"] = json.loads(kv.get("derive_resources", "{}"))
except Exception:
    report["derive_resources"] = {"error": "unparseable"}
json.dump(report, open(sys.argv[2], "w"), indent=2)
PYEOF
}

kv repo "$REPO_ID"
kv package "$(jget package)"
if [ -n "$VARIANT" ]; then kv variant "\"$VARIANT\""; else kv variant "null"; fi
kv dest "$DEST"
kv uv_path "$UV"
kv uv_version "$UV_VERSION"
kv uv_install_route "$UV_ROUTE"
kv venv_python "$VENV_PY"
VM_PY="$(python3 --version 2>&1 | awk '{print $2}')"
kv vm_python "$VM_PY"
SYS_PY="$(command -v python3)"
beat "uv $UV_VERSION at $UV (route $UV_ROUTE)" 0

checked_versions() { # $1 = python; echoes JSON {pkg: version-or-null} for checked pkgs
  timeout 120 "$1" -c '
import importlib.metadata, json, sys
checks = json.loads(sys.argv[1])
out = {}
for pkg in checks:
    try:
        out[pkg] = importlib.metadata.version(pkg)
    except Exception:
        out[pkg] = None
print(json.dumps(out))
' "$(jget version_checks)" 2>/dev/null || echo '{}'
}
kv system_version_map_before "$(checked_versions "$SYS_PY")"

# ---- Unit 1: base clone at the origin tip (or ff-only refresh) ----
if [ "$SKIP_CLONE" -eq 0 ]; then
  if [ ! -d "$DEST/.git" ]; then
    timeout 1800 git clone "$CLONE_URL" "$DEST" >/dev/null 2>&1 \
      || fail "clone" 0
  else
    timeout 600 git -C "$DEST" fetch origin main >/dev/null 2>&1 || true
    git -C "$DEST" checkout main >/dev/null 2>&1 || true
    timeout 300 git -C "$DEST" merge --ff-only origin/main >/dev/null 2>&1 || true
  fi
fi
[ -d "$DEST" ] || fail "dest missing: $DEST" 0
kv cloned_head "$(git -C "$DEST" rev-parse HEAD 2>/dev/null)"
kv cloned_tree "$(git -C "$DEST" rev-parse 'HEAD^{tree}' 2>/dev/null)"
beat "clone ok head=$(git -C "$DEST" rev-parse --short=12 HEAD 2>/dev/null)" 1

# ---- Unit 2: drive the clone's own init_venv.py ----
# (1) Pre-clean: every known init script reuses an existing venv, so a
# legacy inherited or pip-era venv at the declared path is deleted, not
# reused — installing into it would keep the system substrate silently.
REBUILT=false
if [ -f "$VENV_DIR/pyvenv.cfg" ]; then
  legacy=0
  grep -q '^include-system-site-packages = true' "$VENV_DIR/pyvenv.cfg" && legacy=1
  if [ -x "$VENV_DIR/bin/pip" ] && ! grep -q '^uv = ' "$VENV_DIR/pyvenv.cfg"; then legacy=1; fi
  if [ "$legacy" -eq 1 ]; then
    rm -rf "$VENV_DIR"
    REBUILT=true
    beat "pre-clean: legacy venv at $VENV_REL removed" 2
  fi
fi
kv venv_rebuilt_legacy "$REBUILT"

# (2) Invoke the registry's recorded invocation in the clone, with the
# resolved uv's directory seeded on PATH for the script's own resolver.
INIT_ARGV_JSON="$(jget init_argv)"
read -r -a INIT_ARGV <<< "$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1])))' "$INIT_ARGV_JSON")"
INIT_CWD_REL="$(jget init_cwd)"
INIT_CWD="$DEST"
[ "$INIT_CWD_REL" != "clone" ] && INIT_CWD="$DEST/$INIT_CWD_REL"
INIT_LOG="$OUT/init_venv.log"
kv init_route "${INIT_ARGV[*]}"
( cd "$INIT_CWD" && PATH="$UV_DIR:$PATH" timeout 3600 python3 "${INIT_ARGV[@]}" ) > "$INIT_LOG" 2>&1 \
  || fail "init_venv.py exited non-zero (see init_venv.log)" 2
# (3) Refuse a pip fallback: on this image the fallback builds a pip-less
# venv; any fallback the script reports fails the build.
if grep -qi 'falling back' "$INIT_LOG"; then
  fail "init_venv.py reported a fallback (pip route is broken on this image)" 2
fi
[ -x "$VENV_PY" ] || fail "init_venv.py did not produce $VENV_PY" 2
beat "init_venv.py drive ok (${INIT_ARGV[*]})" 2

# Variant delta: the one targeted install — after init, via resolved uv.
DELTA_JSON="$(jget variant_delta)"
if [ "$DELTA_JSON" != "[]" ]; then
  read -r -a DELTA <<< "$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1])))' "$DELTA_JSON")"
  timeout 1800 "$UV" pip install --python "$VENV_PY" "${DELTA[@]}" >/dev/null 2>&1 \
    || fail "variant delta install" 2
  beat "variant delta applied: ${DELTA[*]}" 2
fi
kv variant_delta "$DELTA_JSON"

# Coverage beyond the declared spec: every third-party module the tree
# imports (scan roots from the registry) must be importable in the venv;
# anything missing is installed by name via resolved uv.
WANTED="$OUT/wanted.json"
python3 - "$DEST" "$(jget scan_roots)" > "$WANTED" <<'PYEOF'
import ast, json, sys
from pathlib import Path
root = Path(sys.argv[1])
roots = json.loads(sys.argv[2])
mods = set()
for rel in roots:
    sub = root / rel
    if not sub.exists():
        continue
    for path in sub.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    mods.add(node.module.split(".")[0])
local = {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if p.is_dir()}
wanted = sorted(mods - sys.stdlib_module_names - local)
print(json.dumps(wanted))
PYEOF

venv_missing() {
  timeout 600 "$VENV_PY" -c '
import importlib, json, sys
missing = []
for m in json.loads(sys.argv[1]):
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
print(json.dumps(missing))
' "$(cat "$WANTED")"
}
missing_before="$(venv_missing)"
kv import_scan_modules "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$WANTED")"
echo "missing_before=$missing_before" >> "$KV"
if [ "$missing_before" != "[]" ]; then
  dists=()
  for m in $(python3 -c 'import json,sys; [print(x) for x in json.load(sys.stdin)]' <<< "$missing_before"); do
    dists+=("${DIST_NAMES[$m]:-$m}")
  done
  timeout 1800 "$UV" pip install --python "$VENV_PY" "${dists[@]}" >/dev/null 2>&1 || true
fi
beat "drive + scan installs ok" 2

# ---- Unit 3: toolchain assertions ----
asserts_ok=1
check_one() { kv "assert_$1" "$2"; [ -n "$2" ] || asserts_ok=0; }

# Isolation: no system site-packages dir on the venv's sys.path.
sys_sites="$("$SYS_PY" -c 'import site; print("\n".join(site.getsitepackages()))' 2>/dev/null)"
venv_path="$("$VENV_PY" -c 'import sys; print("\n".join(sys.path))' 2>/dev/null)"
inherits=false
while IFS= read -r p; do
  [ -n "$p" ] && grep -Fxq "$p" <<< "$venv_path" && inherits=true
done <<< "$sys_sites"
cfg_inherits=false
grep -q '^include-system-site-packages = true' "$VENV_DIR/pyvenv.cfg" 2>/dev/null && cfg_inherits=true
[ "$cfg_inherits" = "true" ] && inherits=true
kv venv_inherits_system_packages "$inherits"
isolated=false
[ "$inherits" = "false" ] && isolated=true
kv venv_isolated "$isolated"
check_one venv_isolated "$([ "$isolated" = "true" ] && echo ok || echo '')"

# Version checks from the registry, evaluated against the venv's map.
versions_json="$(timeout 300 "$VENV_PY" -c '
import importlib.metadata, json
out = {}
for dist in importlib.metadata.distributions():
    name = (dist.metadata.get("Name") or "").lower()
    if name:
        out[name] = dist.version
print(json.dumps(out, sort_keys=True))
' 2>/dev/null)"
[ -n "$versions_json" ] || versions_json='{}'
echo "versions=$versions_json" >> "$KV"
kv versions_fingerprint "$(python3 -c 'import hashlib,json,sys; print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]), sort_keys=True).encode()).hexdigest())' "$versions_json")"
checks_result="$(python3 - "$versions_json" "$(jget version_checks)" <<'PYEOF'
import json, sys
versions = {k.lower(): v for k, v in json.loads(sys.argv[1]).items()}
checks = json.loads(sys.argv[2])
def parse(t):
    out = []
    for piece in str(t).split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)
def pad(a, b):
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
def satisfies(version, spec):
    v = parse(version)
    for clause in str(spec).split(","):
        clause = clause.strip()
        if not clause:
            continue
        op = next((o for o in ("==", "!=", ">=", "<=", ">", "<") if clause.startswith(o)), None)
        if op is None:
            return False
        a, b = pad(v, parse(clause[len(op):]))
        ok = {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False
    return True
bad = []
for pkg, spec in checks.items():
    got = versions.get(pkg.lower())
    if not got or not satisfies(got, spec):
        bad.append(f"{pkg}={got} vs {spec}")
print(" ".join(bad))
PYEOF
)"
check_one version_checks "$([ -z "$checks_result" ] && echo ok || echo '')"
[ -n "$checks_result" ] && beat "version checks FAILED: $checks_result" 3

missing_after="$(venv_missing)"
echo "missing_after=$missing_after" >> "$KV"
[ "$missing_after" = "[]" ] && kv assert_venv_import_scan "ok" || kv assert_venv_import_scan ""
[ "$missing_after" = "[]" ] || asserts_ok=0
kv assertions_passed "$([ "$asserts_ok" -eq 1 ] && echo true || echo false)"
kv system_version_map_after "$(checked_versions "$SYS_PY")"
beat "asserts passed=$([ "$asserts_ok" -eq 1 ] && echo True || echo False)" 3

# ---- Unit 4: resource probe + remote registry merge ----
DERIVE="$(jget derive_resources)"
derive='{}'
if [ "$DERIVE" != "null" ] && [ -n "$DERIVE" ]; then
  mod="${DERIVE%%:*}"; fn="${DERIVE##*:}"
  derive="$(cd "$DEST" && timeout 300 "$VENV_PY" -c "
import json, dataclasses, importlib
f = getattr(importlib.import_module('$mod'), '$fn')
print(json.dumps(dataclasses.asdict(f())))
" 2>/dev/null | tail -1)"
  [ -n "$derive" ] || derive='{"error": "derive_resources probe failed"}'
fi
echo "derive_resources=$derive" >> "$KV"

# Merge this build's record into the remote registry beside the clones.
python3 - "$(dirname "$DEST")/env-registry.json" "$KV" <<'PYEOF' || fail "remote registry merge" 4
import hashlib, json, sys
kv = {}
for line in open(sys.argv[2]):
    if "=" in line:
        k, v = line.rstrip("\n").split("=", 1)
        kv[k] = v
try:
    versions = json.loads(kv.get("versions", "{}"))
except Exception:
    versions = {}
record = {
    "repo": kv.get("repo", ""),
    "package": (lambda v: None if v in ("", "null") else v)(kv.get("package", "")),
    "variant": json.loads(kv.get("variant", "null")),
    "dest": kv.get("dest", "").rstrip("/").split("/")[-1],
    "venv_python": kv.get("venv_python", ""),
    "cloned_head": kv.get("cloned_head", ""),
    "cloned_tree": kv.get("cloned_tree", ""),
    "vm_python": kv.get("vm_python", ""),
    "init_route": kv.get("init_route", ""),
    "versions_fingerprint": hashlib.sha256(
        json.dumps(versions, sort_keys=True).encode()).hexdigest(),
}
path = sys.argv[1]
try:
    registry = json.load(open(path))
except Exception:
    registry = {}
registry[record["dest"]] = record
tmp = path + ".tmp"
json.dump(registry, open(tmp, "w"), indent=2)
import os
os.replace(tmp, path)
PYEOF

write_report
beat "env-build complete" 4
grep -q '^assertions_passed=true$' "$KV"
