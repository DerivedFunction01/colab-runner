"""Lease unit tests: shorten — the operator's deadline
edit, extend's mirror, with stub colab callables (no
subprocess)."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

from unit_harness import NOW, gate_permissive, lease, orphan_stub, repo, seed_record  # noqa: F401

# --- Shorten --------------------------------------------------------------------
#
# The operator's explicit deadline edit, extend's mirror. The
# stamp discipline: while shortened_at stands, the extend
# guard skips the lease; an operator extend clears the stamp
# (the latest action wins) and the guard treats the lease
# normally again.


def test_shorten_moves_deadline_earlier_and_stamps(repo):
    seed_record(repo)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=2, caller="alpha", now_dt=NOW)
    assert out["action"] == "shortened"
    assert out["old_deadline"] == "2026-10-30T01:00:00+00:00"
    assert out["new_deadline"] == "2026-10-29T23:00:00+00:00"
    assert out["hours"] == 2 and out["caller"] == "alpha"
    rec = lease.load_record(repo, "standard")
    assert rec["deadline"] == "2026-10-29T23:00:00+00:00"
    assert rec["shortened_at"] == NOW.isoformat()
    assert rec["shortened_by"] == "alpha"
    assert rec["shortened_from"] == "2026-10-30T01:00:00+00:00"


def test_shorten_absolute_deadline_form(repo):
    seed_record(repo)
    target = NOW + timedelta(hours=2)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=target, now_dt=NOW)
    assert out["action"] == "shortened"
    assert out["new_deadline"] == target.isoformat()
    assert "hours" not in out


def test_shorten_not_earlier_refuses(repo):
    seed_record(repo)
    later = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=NOW + timedelta(hours=9), now_dt=NOW)
    assert later["action"] == "shorten-not-earlier"
    same = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc), now_dt=NOW)
    assert same["action"] == "shorten-not-earlier"
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-30T01:00:00+00:00"


def test_shorten_deadline_past_refuses(repo):
    seed_record(repo)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", deadline=NOW - timedelta(hours=1), now_dt=NOW)
    assert out["action"] == "shorten-deadline-past"
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=100, now_dt=NOW)
    assert out["action"] == "shorten-deadline-past"
    rec = lease.load_record(repo, "standard")
    assert rec["deadline"] == "2026-10-30T01:00:00+00:00" and "shortened_at" not in rec


def test_shorten_no_record_disabled_and_no_target(repo):
    out = lease.shorten(orphan_stub([]), repo, "ghost-9", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-no-lease"
    seed_record(repo)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", now_dt=NOW)
    assert out["action"] == "shorten-no-target"
    (repo / "config" / "lease.json").write_text(json.dumps({"enabled": False}))
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "lease-disabled"


def test_shorten_unknown_and_absent(repo):
    seed_record(repo)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("colab", 120)

    out = lease.shorten(boom, repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-unknown"
    assert out["attempts"] == 3 and "TimeoutExpired" in out["read_error"]
    out = lease.shorten(orphan_stub([]), repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-no-lease"  # absent, and the seed is no window


def test_shorten_standing_managed_refuses(repo):
    rec = seed_record(repo)
    rec["standing"] = True
    lease.save_record(repo, rec)
    lease.write_grant(repo, 24, now_dt=NOW)
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=1, now_dt=NOW)
    assert out["action"] == "shorten-standing-managed"


def test_shorten_kit_reinstall_reported(repo, tmp_path, monkeypatch):
    rec = seed_record(repo)
    rec["kit"] = "installed"
    rec["renew"] = True
    lease.save_record(repo, rec)
    monkeypatch.setenv("HOME", str(tmp_path))  # no token here: the kit reinstall reports token-missing, non-fatally
    out = lease.shorten(orphan_stub(["standard"]), repo, "standard", hours=2, now_dt=NOW)
    assert out["action"] == "shortened"
    assert out["kit"] == {"kit": "install-failed", "stage": "token-missing"}
    assert lease.load_record(repo, "standard")["deadline"] == "2026-10-29T23:00:00+00:00"


def test_extend_clears_shorten_stamp(repo):
    rec = seed_record(repo)
    rec["shortened_at"] = NOW.isoformat()
    rec["shortened_by"] = "alpha"
    rec["shortened_from"] = "2026-10-30T03:00:00+00:00"
    lease.save_record(repo, rec)
    out = lease.extend(orphan_stub(["standard"]), repo, "standard", 2, caller="alpha", now_dt=NOW)
    assert out["action"] == "extended"
    saved = lease.load_record(repo, "standard")
    assert "shortened_at" not in saved and "shortened_by" not in saved and "shortened_from" not in saved


def test_shorten_extend_shorten_restamps(repo):
    seed_record(repo)
    stub = orphan_stub(["standard"])
    first = lease.shorten(stub, repo, "standard", hours=2, caller="alpha", now_dt=NOW)
    assert first["action"] == "shortened"
    ext = lease.extend(stub, repo, "standard", 1, caller="alpha", now_dt=NOW)
    assert ext["action"] == "extended"
    mid = lease.load_record(repo, "standard")
    assert mid["deadline"] == "2026-10-30T00:00:00+00:00" and "shortened_at" not in mid
    second = lease.shorten(stub, repo, "standard", hours=1, caller="beta", now_dt=NOW)
    assert second["action"] == "shortened"
    saved = lease.load_record(repo, "standard")
    assert saved["deadline"] == "2026-10-29T23:00:00+00:00"
    assert saved["shortened_from"] == "2026-10-30T00:00:00+00:00"
    assert saved["shortened_by"] == "beta" and saved["shortened_at"] == NOW.isoformat()
