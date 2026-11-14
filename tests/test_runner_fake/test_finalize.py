"""Pull truthfulness and finalize accounting, against the
fake colab CLI: a download that delivers fewer bytes than
the reader sizes the remote output at is not a pull, a held
0-byte pull is named at finalize when the remote has
content, and declared outputs that never arrived are
reported as missing — never a silent short pull."""

import json
import subprocess
import sys
from pathlib import Path

from runner_harness import FAKE, _fire_smoke, run_bin, run_verb, runner_env, runs_dir  # noqa: F401


def install_partial_job(env, name):
    """A bare job (install_bare_job's shape) that writes only
    one of its two declared outputs and exits 0."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["present.txt", "absent.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def _truncating_download_wrapper(tmp_path):
    """A COLAB_BIN wrapper whose downloads report success and
    deliver an empty file — the s4diag2 manufacture (2026-11-11:
    a completed job's 1,762-byte heartbeat and 38-byte checkpoint
    were pulled as 0-byte entries and the run read complete)."""
    wrapper = tmp_path / "truncating_colab.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "rc = subprocess.run([sys.executable, os.environ['FAKE_COLAB_REAL'], *args]).returncode\n"
        "if 'download' in args and rc == 0:\n"
        "    Path(args[-1]).write_bytes(b'')\n"
        "sys.exit(rc)\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def test_a_download_that_delivers_empty_is_not_a_pull(tmp_path):
    """Pull truthfulness (2026-11-11): when the channel delivers
    fewer bytes than the reader sizes the remote output at, the
    pull is refused — no entry is certified, the error names the
    mismatch, and the shortfall surfaces at finalize as missing
    or empty, never as a clean complete on 0-byte artifacts.
    Collection hold (2026-11-14): the foreground loop now holds
    and retries the pull before closing; this manufacture's
    channel never recovers, so the hold is shrunk to seconds
    and the expiry close must name the failure as
    collection_failed, apart from the payload's outcome."""
    env = runner_env(tmp_path)
    env.update(COLAB_BIN=str(_truncating_download_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE), COLAB_RUN_COLLECTION_HOLD_S="3")
    r = subprocess.run([sys.executable, str(run_bin(env)), "smoke"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert "heartbeat.log" in (out.get("collection_failed") or []), out
    assert "checkpoint.json" in (out.get("collection_failed") or []), out
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    surfaced = set(out.get("missing_outputs", [])) | set(out.get("empty_outputs", []))
    assert "heartbeat.log" in surfaced, out
    assert "checkpoint.json" in surfaced, out
    for entry in rec.get("pulled", []):
        if entry["bytes"] == 0:
            assert entry["path"] in set(rec.get("empty_outputs", [])), f"a 0-byte pull of {entry['path']} was certified silently"
    assert "landed 0 bytes against a remote size of" in (rec.get("last_pull_error") or ""), rec.get("last_pull_error")
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert not list(results.glob("*.pulling")), "a refused pull left a staging file behind"


def test_finalize_holds_a_held_empty_pull_until_it_collects(tmp_path):
    """A record already holding a 0-byte pulled entry (landed
    before the size cross-check existed) whose remote output
    has content: the 2026-11-11 belt named it in empty_outputs
    at a terminal finalize. The 2026-11-14 collection hold
    supersedes the close: while the pull keeps failing the run
    does not close at all — the poll answers collecting with
    the output named in collection_pending — and once the
    channel recovers, the next poll collects the real bytes
    and closes complete with no hold left on the record."""
    env = runner_env(tmp_path)
    first = _fire_smoke(env)
    runs = runs_dir(env)
    first_rec = json.loads((runs / f"{first['run_id']}.json").read_text())
    seeded_id = "seeded-empty-20261111T000000Z"
    seeded = runs / f"{seeded_id}.json"
    seeded.write_text(json.dumps({
        "run_id": seeded_id,
        "job": "smoke",
        "session": first_rec["session"],
        "state": "polling",
        "started_at": "2026-11-11T00:00:00+00:00",
        "remote_out": first_rec["remote_out"],
        "pulled": [{"path": "heartbeat.log", "bytes": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "route": "single"}],
    }))
    env.update(COLAB_BIN=str(_truncating_download_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r = run_verb(env, "--poll", seeded_id)
    assert r.returncode == 3, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "collecting", out
    # The truncating wrapper fails EVERY transfer this pass, so
    # the hold names every declared output whose remote content
    # failed to land — the held 0-byte heartbeat plus the four
    # outputs never yet pulled.
    all_outputs = ["checkpoint.json", "heartbeat.log", "unit-0.bin", "unit-1.bin", "unit-2.bin"]
    assert out.get("collection_pending") == all_outputs, out
    rec = json.loads(seeded.read_text())
    assert rec["state"] == "polling", rec
    assert rec.get("collection_pending") == all_outputs, rec.get("collection_pending")
    assert rec.get("collection_since"), rec
    env2 = runner_env(tmp_path)
    r2 = run_verb(env2, "--poll", seeded_id)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    out2 = json.loads(r2.stdout)
    assert out2["state"] == "complete", out2
    rec2 = json.loads(seeded.read_text())
    assert rec2["state"] == "complete", rec2
    assert "collection_pending" not in rec2 and "collection_since" not in rec2, rec2
    held = {e["path"]: e for e in rec2.get("pulled", [])}
    assert held["heartbeat.log"]["bytes"] > 0, held


def install_stray_job(env, name):
    """A bare job whose payload writes one declared output to
    its out dir and the other to its cwd root — the
    cwd-relative addressing slip behind the 2026-11-11
    missing-outputs specimens."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "Path('stray.txt').write_text('stray\\n')\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["present.txt", "stray.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_finalize_names_an_output_that_sits_at_the_cwd_root(tmp_path):
    """Misplaced naming + guarded collection (2026-11-11
    naming; 2026-11-12 collection): a declared output the
    payload wrote to its cwd root instead of its out dir is
    named in misplaced_outputs with its path and size — and,
    being FRESH (written during this run, judged against the
    run's own started stamp), it is also collected through the
    pull: it lands in the results, joins the pulled list with
    route "misplaced", and leaves missing_outputs. Five
    consumer runs lost or hand-recovered verdicts to the
    named-never-pulled shape before the collection landed."""
    env = runner_env(tmp_path)
    install_stray_job(env, "stray")
    r = subprocess.run([sys.executable, str(run_bin(env)), "stray"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert not out.get("missing_outputs"), out
    misplaced = out.get("misplaced_outputs")
    assert misplaced and len(misplaced) == 1, out
    assert misplaced[0]["name"] == "stray.txt" and misplaced[0]["bytes"] == 6
    assert misplaced[0]["path"].endswith("/stray.txt")
    assert misplaced[0]["fresh"] is True and misplaced[0]["collected"] is True
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec.get("misplaced_outputs") == misplaced
    assert not rec.get("missing_outputs"), rec
    by_path = {e["path"]: e for e in rec["pulled"]}
    assert set(by_path) == {"present.txt", "stray.txt"}
    assert by_path["stray.txt"]["route"] == "misplaced" and by_path["stray.txt"]["bytes"] == 6
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert (results / "stray.txt").read_text() == "stray\n"


def install_stale_stray_job(env, name):
    """A bare job whose payload back-dates its cwd-root output
    by an hour — the shape of an earlier run's leftover sitting
    at the root when a later run declares the same name."""
    root = Path(env["TEST_REPO_ROOT"])
    job_dir = root / "jobs" / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, os, sys, time\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "p = Path('stray.txt')\n"
        "p.write_text('stale\\n')\n"
        "old = time.time() - 3600\n"
        "os.utime(p, (old, old))\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["present.txt", "stray.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 180}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_finalize_names_but_never_collects_a_stale_root_output(tmp_path):
    """The freshness guard: a root file older than the run is
    an earlier run's leftover — it is named in
    misplaced_outputs with collected False, stays in
    missing_outputs, and is NOT delivered as this run's
    output. An unguarded collect would certify a prior run's
    artifact as this run's result."""
    env = runner_env(tmp_path)
    install_stale_stray_job(env, "stalestray")
    r = subprocess.run([sys.executable, str(run_bin(env)), "stalestray"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert out.get("missing_outputs") == ["stray.txt"], out
    misplaced = out.get("misplaced_outputs")
    assert misplaced and len(misplaced) == 1, out
    assert misplaced[0]["fresh"] is False and misplaced[0]["collected"] is False
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert [e["path"] for e in rec["pulled"]] == ["present.txt"]
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert not (results / "stray.txt").exists()


def install_sleepy_stray_job(env, name):
    """A job that declares stray.txt but never writes it, and
    sleeps — a live co-tenant whose execution window covers
    another lane's root write of the same name."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys, time\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "time.sleep(120)\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": ["present.txt", "stray.txt"], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 300}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_finalize_flags_a_root_output_contended_by_a_live_co_tenant(tmp_path):
    """Cross-lane collision guard (defect routed 2026-11-12:
    two lanes' drives on one session both wrote a generic
    drive-result name cwd-relative, and the first finalize
    collected the other lane's content — fresh, right-sized,
    wrong run). With a live co-tenant declaring the same name
    and executing when the root file was written, attribution
    is genuinely ambiguous: the entry is flagged contended,
    NOT collected, and stays in missing_outputs — named for
    the owning lane to disambiguate instead of silently
    delivered. The second lane's launch also carries the
    output-name warning naming the first run."""
    env = runner_env(tmp_path)
    install_sleepy_stray_job(env, "lanea")
    install_stray_job(env, "laneb")
    ra = subprocess.run([sys.executable, str(run_bin(env)), "lanea", "--detach"], capture_output=True, text=True, env=env, timeout=300)
    assert ra.returncode == 0, ra.stdout + ra.stderr
    lanea_run = json.loads(ra.stdout)["run_id"]
    rb = subprocess.run([sys.executable, str(run_bin(env)), "laneb"], capture_output=True, text=True, env=env, timeout=300)
    assert rb.returncode == 0, rb.stdout + rb.stderr
    out = json.loads(rb.stdout)
    assert out["state"] == "complete"
    warnings = out.get("output_name_warnings") or json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text()).get("output_name_warnings")
    assert warnings and any(w["name"] == "stray.txt" and w["shared_with"] == lanea_run for w in warnings), out
    assert "stray.txt" in (out.get("missing_outputs") or []), out
    misplaced = out.get("misplaced_outputs")
    assert misplaced and len(misplaced) == 1, out
    entry = misplaced[0]
    assert entry["name"] == "stray.txt" and entry["fresh"] is True
    assert entry["contended"] is True and entry["contended_with"] == lanea_run
    assert entry["collected"] is False
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert not (results / "stray.txt").exists()
    assert (results / "present.txt").exists()


def test_finalize_collects_despite_a_dead_co_tenant_with_no_finished_stamp(tmp_path):
    """Refinement specimen (routed 2026-11-12 from Lane 10A's
    collision verification): the co-tenant record is the
    lane's own first attempt — failed and terminal, but with
    no finished_at stamped, so the guard priced its window
    end as NOW and refused the re-run's fresh root output as
    contended with the dead run, 82 minutes after the dead
    run's last evidence. A terminal run's window ends at its
    evidence, never at now: the re-run's output collects.
    The setup runs the job once, then rewrites its record
    into the specimen's exact shape (failed, finished_at
    removed, evidence stamps aged) before the re-run."""
    import time as _time
    from datetime import datetime, timezone

    env = runner_env(tmp_path)
    install_stray_job(env, "retrylane")
    r1 = subprocess.run([sys.executable, str(run_bin(env)), "retrylane"], capture_output=True, text=True, env=env, timeout=300)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    first_run = json.loads(r1.stdout)["run_id"]
    runs = runs_dir(env)
    dead_path = runs / f"{first_run}.json"
    dead = json.loads(dead_path.read_text())
    now = _time.time()
    dead["state"] = "failed"
    dead.pop("finished_at", None)
    dead["started_at"] = datetime.fromtimestamp(now - 7200, tz=timezone.utc).isoformat()
    dead["last_evidence_at"] = datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat()
    dead["collector_at"] = dead["last_evidence_at"]
    dead_path.write_text(json.dumps(dead))
    r2 = subprocess.run([sys.executable, str(run_bin(env)), "retrylane"], capture_output=True, text=True, env=env, timeout=300)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    out = json.loads(r2.stdout)
    assert out["state"] == "complete"
    misplaced = out.get("misplaced_outputs")
    assert misplaced and len(misplaced) == 1, out
    entry = misplaced[0]
    assert entry["name"] == "stray.txt" and entry["fresh"] is True
    assert entry.get("contended") is not True and entry["collected"] is True, entry
    assert "stray.txt" not in (out.get("missing_outputs") or []), out
    results = Path(env["TEST_REPO_ROOT"]) / "results" / out["run_id"]
    assert (results / "stray.txt").exists()


def install_sleepy_mapped_job(env, name):
    """The sleepy co-tenant with MAPPED outputs ({"local",
    "remote"} entries instead of plain names) — the manifest
    shape that crashed the first overlap implementation when
    a consumer launch hashed the raw dict entries."""
    root = Path(env["TEST_REPO_ROOT"])
    jobs_dir = root / "jobs"
    if jobs_dir.is_symlink():
        jobs_dir.unlink()
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "payload.py").write_text(
        "import argparse, sys, time\n"
        "from pathlib import Path\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--out', required=True)\n"
        "a = ap.parse_args()\n"
        "out = Path(a.out)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'present.txt').write_text('here\\n')\n"
        "time.sleep(120)\n"
        "sys.exit(0)\n"
    )
    manifest = {"name": name, "script": f"jobs/{name}/payload.py", "args": [], "outputs": [{"local": "present.txt", "remote": "/content/x/present.txt"}, {"local": "stray.txt", "remote": "/content/x/stray.txt"}], "shape": "cpu-highram", "checkpoint_pull_seconds": 1, "timeout": 300}
    (job_dir / "job.json").write_text(json.dumps(manifest))
    return job_dir


def test_launch_overlap_check_tolerates_mapped_manifest_outputs(tmp_path):
    """Regression (defect routed 2026-11-12, hours after the
    overlap check first ran live): a neighbour manifest
    declaring mapped outputs crashed a consumer's launch
    client-side (TypeError: unhashable type 'dict') — an
    advisory check must never break the launch it rides on.
    With the mapped co-tenant live, the plain job launches,
    its warning names the shared local name, and finalize
    still flags the root file contended."""
    env = runner_env(tmp_path)
    install_sleepy_mapped_job(env, "lanem")
    install_stray_job(env, "laneb")
    ra = subprocess.run([sys.executable, str(run_bin(env)), "lanem", "--detach"], capture_output=True, text=True, env=env, timeout=300)
    assert ra.returncode == 0, ra.stdout + ra.stderr
    lanem_run = json.loads(ra.stdout)["run_id"]
    rb = subprocess.run([sys.executable, str(run_bin(env)), "laneb"], capture_output=True, text=True, env=env, timeout=300)
    assert rb.returncode == 0, rb.stdout + rb.stderr
    out = json.loads(rb.stdout)
    assert out["state"] == "complete"
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    warnings = rec.get("output_name_warnings")
    assert warnings and any(w["name"] == "stray.txt" and w["shared_with"] == lanem_run for w in warnings), rec
    misplaced = out.get("misplaced_outputs")
    assert misplaced and misplaced[0]["contended"] is True and misplaced[0]["collected"] is False, out


def test_finalize_reports_declared_outputs_that_never_arrived(tmp_path):
    """Declared-vs-pulled accounting (the pull-gap): a payload
    that exits 0 without writing one of its declared outputs
    still completes — the exit code is the outcome truth — but
    the shortfall is recorded on the run and returned in the
    result, never a silent short pull."""
    env = runner_env(tmp_path)
    install_partial_job(env, "partial")
    r = subprocess.run([sys.executable, str(run_bin(env)), "partial"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["state"] == "complete"
    assert out.get("missing_outputs") == ["absent.txt"], out
    rec = json.loads((runs_dir(env) / f"{out['run_id']}.json").read_text())
    assert rec.get("missing_outputs") == ["absent.txt"]
    assert [e["path"] for e in rec["pulled"]] == ["present.txt"]
    # The collection hold must not fire here: absent.txt was
    # never written anywhere, so its failed pull is an absence,
    # not a transfer shortfall (2026-11-14 partition).
    assert "collection_pending" not in rec and "collection_failed" not in rec, rec
    assert "collection_failed" not in out, out
