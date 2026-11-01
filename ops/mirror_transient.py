#!/usr/bin/env python3
"""Capture/restore transient files between canonical homes and the repo mirror.

Registry: config/transient.json. Canonical paths resolve against $HOME, or
$COLAB_RUNNER_HOME when set (sandboxed tests). File contents are never
printed; reports carry only existence, size, mtime, and action taken.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "config" / "transient.json"


def home() -> Path:
    override = os.environ.get("COLAB_RUNNER_HOME")
    return Path(override) if override else Path.home()


def load_registry() -> list[dict]:
    return json.loads(REGISTRY.read_text())["items"]


def resolve(item: dict) -> tuple[Path, Path]:
    canonical = Path(str(item["canonical"]).replace("~", str(home()), 1))
    return canonical, REPO / item["mirror"]


def atomic_copy(src: Path, dst: Path, mode: str | None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=dst.name + ".")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
        if mode:
            os.chmod(tmp, int(mode, 8))
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    # Preserve mtime so newest-wins comparisons stay honest across copies.
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))


def verify(path: Path, kind: str | None) -> bool:
    try:
        if kind == "json":
            json.loads(path.read_text())
        return path.exists()
    except Exception:
        return False


def token_valid(path: Path) -> bool:
    """The colab-token validation: parses as JSON and carries a
    refresh_token of plausible length. The interactive flow's stub (a
    lone 15-char refresh_token) fails it. Contents are never printed.
    Mirrors bin/colab-run's guard check."""
    try:
        parsed = json.loads(path.read_text())
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    rt = parsed.get("refresh_token")
    return isinstance(rt, str) and len(rt) >= 20


def file_report(item: dict) -> dict:
    canonical, mirror = resolve(item)
    rep = {"id": item["id"], "rule": item.get("rule"), "kind": item["kind"]}
    for label, p in (("canonical", canonical), ("mirror", mirror)):
        rep[label] = (
            {"exists": True, "bytes": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
            if p.exists()
            else {"exists": False}
        )
    if canonical.exists() and mirror.exists():
        rep["identical"] = canonical.read_bytes() == mirror.read_bytes()
    return rep


def capture(item: dict) -> str:
    if item["kind"] != "file" or not item.get("enabled", True):
        return "skipped"
    canonical, mirror = resolve(item)
    if not canonical.exists():
        return "no-canonical"
    if item.get("validate") == "colab-token":
        # A clobbered credential must never propagate into the mirror
        # (2026-11-01: the flow's stub overwrote the canonical token
        # and a capture carried the stub into the mirror, destroying
        # the last good copy). An invalid canonical with a valid
        # mirror heals from the mirror instead of capturing.
        if not token_valid(canonical):
            if mirror.exists() and token_valid(mirror):
                atomic_copy(mirror, canonical, item.get("mode"))
                return "canonical-healed-from-mirror"
            return "canonical-invalid-kept"
    if item.get("never_capture_empty") and mirror.exists():
        # A live binding clobbered to {} by a CLI side effect must not
        # overwrite the last good mirror copy (observed 2026-10-29: a
        # `colab run` emptied sessions.json while its assignment lived on).
        try:
            if json.loads(canonical.read_text()) in ({}, []) and json.loads(mirror.read_text()) not in ({}, []):
                return "mirror-nonempty-kept"
        except Exception:
            pass
    if mirror.exists():
        if item["rule"] == "newest-wins" and mirror.stat().st_mtime > canonical.stat().st_mtime:
            return "mirror-newer-kept"
        if item["rule"] in ("newest-wins", "restore-if-missing") and canonical.read_bytes() == mirror.read_bytes():
            return "in-sync"
    atomic_copy(canonical, mirror, item.get("mode"))
    return "captured" if verify(mirror, item.get("verify")) else "verify-failed"


def restore(item: dict) -> str:
    if item["kind"] != "file" or not item.get("enabled", True):
        return "skipped"
    canonical, mirror = resolve(item)
    if not mirror.exists():
        return "no-mirror"
    if item.get("validate") == "colab-token" and not token_valid(mirror):
        # An invalid mirror copy (a stub captured before the
        # validation rule, or a clobber that reached the mirror) never
        # overwrites the canonical file, whatever the mtimes say:
        # newest-wins applies to valid credentials only.
        return "mirror-invalid-kept"
    if canonical.exists():
        if item["rule"] == "restore-if-missing":
            return "canonical-present"
        if mirror.stat().st_mtime <= canonical.stat().st_mtime:
            return "canonical-newer-kept"
    atomic_copy(mirror, canonical, item.get("mode"))
    return "restored" if verify(canonical, item.get("verify")) else "verify-failed"


OPS = {"capture": capture, "restore": restore}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("op", choices=["capture", "restore", "status"])
    ap.add_argument("--item", help="only this registry id")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args()

    items = [i for i in load_registry() if not args.item or i["id"] == args.item]
    if not items:
        print(f"no registry item matches {args.item!r}", file=sys.stderr)
        return 2
    results, failed = [], False
    for item in items:
        if args.op == "status":
            results.append(file_report(item) if item["kind"] == "file" else {"id": item["id"], "kind": item["kind"]})
            continue
        action = OPS[args.op](item)
        failed |= action == "verify-failed"
        results.append({"id": item["id"], args.op: action})
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(" ".join(f"{k}={v}" for k, v in r.items() if not isinstance(v, dict)))
            for k, v in r.items():
                if isinstance(v, dict):
                    print(f"  {k}: {v}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
