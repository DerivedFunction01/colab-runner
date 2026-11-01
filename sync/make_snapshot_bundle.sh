#!/usr/bin/env bash
# Build an origin-lineage snapshot bundle of the tree being shipped.
#
# The snapshot commit's parent is the CURRENT origin/main and its tree is the
# current HEAD tree of the source repo — never a bundle on the local lineage
# (local main is hundreds of commits diverged from origin; such a bundle
# cannot fast-forward a fresh origin clone).
#
# Env: COLAB_REPO_SRC (required — the source checkout's local path, taken by
# callers from the selected repo's registry entry; there is no default, and
# no repo is named here), BUNDLE_OUT (default
# <colab-runner>/var/snapshot-<ts>.bundle). Prints a JSON manifest and writes
# <bundle>.manifest.json beside the bundle.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${COLAB_REPO_SRC:-}"
if [ -z "$SRC" ]; then
  echo "make_snapshot_bundle: COLAB_REPO_SRC is not set (pass the registry entry's local_path)" >&2; exit 2
fi
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BUNDLE_OUT:-$REPO_ROOT/var/snapshot-$TS.bundle}"
mkdir -p "$(dirname "$OUT")"

ORIGIN_TIP="$(git -C "$SRC" rev-parse origin/main)"
TREE="$(git -C "$SRC" rev-parse 'HEAD^{tree}')"
SNAPSHOT="$(git -C "$SRC" -c user.name="DL" -c user.email="" commit-tree "$TREE" -p "$ORIGIN_TIP" -m "colab-runner snapshot $TS (tree $TREE)")"
# git bundle refuses a bare SHA ("Refusing to create empty bundle"); it
# needs a ref. Create a scratch ref, bundle it, then drop the ref.
REF="refs/colab-snapshot/$TS"
git -C "$SRC" update-ref "$REF" "$SNAPSHOT"
git -C "$SRC" bundle create "$OUT" "$REF" >/dev/null
git -C "$SRC" update-ref -d "$REF"
BYTES="$(stat -c %s "$OUT")"
SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
python3 - "$OUT" "$BYTES" "$SHA" "$SNAPSHOT" "$ORIGIN_TIP" "$TREE" <<'EOF'
import json, sys
out, b, sha, snap, parent, tree = sys.argv[1:7]
m = {"bundle": out, "bytes": int(b), "sha256": sha, "snapshot_commit": snap,
     "parent_origin_tip": parent, "tree": tree,
     "apply": f"git fetch {out} {snap} && git merge --ff-only FETCH_HEAD"}
open(out + ".manifest.json", "w").write(json.dumps(m, indent=2))
print(json.dumps(m, indent=2))
EOF
