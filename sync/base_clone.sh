#!/usr/bin/env bash
# Run ON the Colab VM: base HTTPS clone of the origin tip, the substrate the
# snapshot bundle fast-forwards. Usage: base_clone.sh [repo_url] [dest]
set -euo pipefail
URL="${1:-${COLAB_REPO_URL:-}}"
DEST="${2:-}"
if [ -z "$URL" ]; then
  echo "base_clone: no repo URL given (arg 1 or COLAB_REPO_URL)" >&2; exit 2
fi
if [ -z "$DEST" ]; then
  echo "base_clone: no dest given (arg 2; the registry entry's dest under /content)" >&2; exit 2
fi
if [ ! -d "$DEST/.git" ]; then
  git clone "$URL" "$DEST"
fi
git -C "$DEST" fetch origin main
git -C "$DEST" checkout main
# The clone is a scratch substrate whose only job is to sit at the origin
# tip before a snapshot apply; a previous apply leaves local main on a
# snapshot commit that diverges once origin moves, so pin by reset —
# a fast-forward merge fatals on that divergence and strands the clone.
git -C "$DEST" reset --hard origin/main
echo "base clone at $(git -C "$DEST" rev-parse HEAD) tree $(git -C "$DEST" rev-parse 'HEAD^{tree}')"
