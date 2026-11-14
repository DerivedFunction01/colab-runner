"""Collection hold (2026-11-14, Step 2A specimen): a run whose
payload completed and whose declared outputs sit probed-present
remotely — one in its out dir, one at the session root — must
not close while the transfer of those outputs is failing. The
poll answers collecting (exit 3) with the outputs named, the
record stays non-terminal, and the next poll after the channel
recovers collects both and closes complete."""

import json
import subprocess
import sys
from pathlib import Path

from runner_harness import FAKE, run_bin, run_verb, runner_env, runs_dir  # noqa: F401
from test_finalize import _truncating_download_wrapper, install_stray_job


def test_poll_holds_a_complete_payload_whose_outputs_fail_to_transfer(tmp_path):
    env = runner_env(tmp_path)
    install_stray_job(env, "holdlane")
    r1 = subprocess.run([sys.executable, str(run_bin(env)), "holdlane"], capture_output=True, text=True, env=env, timeout=300)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    first = json.loads(r1.stdout)
    assert first["state"] == "complete"
    runs = runs_dir(env)
    first_rec = json.loads((runs / f"{first['run_id']}.json").read_text())
    # The first run is only the fixture builder for the payload
    # state; remove its record so the seeded record stands
    # alone over the payload, as in the specimen — a second
    # record of the same job declaring the same root name
    # would make the stray a contended co-tenant file (the
    # attribution guard's own tests pin that behavior, and
    # contended entries never hold a run).
    (runs / f"{first['run_id']}.json").unlink()
    # A second record over the same finished payload, in the
    # specimen's shape: polling, nothing pulled, the outputs
    # still sitting where the payload left them.
    seeded_id = "seeded-hold-20261114T000000Z"
    seeded = runs / f"{seeded_id}.json"
    seeded.write_text(json.dumps({
        "run_id": seeded_id,
        "job": "holdlane",
        "session": first_rec["session"],
        "state": "polling",
        "started_at": first_rec["started_at"],
        "remote_out": first_rec["remote_out"],
    }))
    env.update(COLAB_BIN=str(_truncating_download_wrapper(tmp_path)), FAKE_COLAB_REAL=str(FAKE))
    r2 = run_verb(env, "--poll", seeded_id)
    assert r2.returncode == 3, r2.stdout + r2.stderr
    out = json.loads(r2.stdout)
    assert out["state"] == "collecting", out
    assert out.get("collection_pending") == ["present.txt", "stray.txt"], out
    rec = json.loads(seeded.read_text())
    assert rec["state"] == "polling", rec
    assert rec.get("collection_pending") == ["present.txt", "stray.txt"], rec.get("collection_pending")
    assert rec.get("collection_since"), rec
    assert "landed 0 bytes" in (rec.get("last_pull_error") or ""), rec.get("last_pull_error")
    env2 = runner_env(tmp_path)
    r3 = run_verb(env2, "--poll", seeded_id)
    assert r3.returncode == 0, r3.stdout + r3.stderr
    out3 = json.loads(r3.stdout)
    assert out3["state"] == "complete", out3
    assert not out3.get("missing_outputs"), out3
    rec3 = json.loads(seeded.read_text())
    assert rec3["state"] == "complete", rec3
    assert "collection_pending" not in rec3 and "collection_since" not in rec3, rec3
    by_path = {e["path"]: e for e in rec3["pulled"]}
    assert by_path["present.txt"]["bytes"] == 5
    assert by_path["stray.txt"]["route"] == "misplaced" and by_path["stray.txt"]["bytes"] == 6
    results = Path(env["TEST_REPO_ROOT"]) / "results" / seeded_id
    assert (results / "present.txt").read_text() == "here\n"
    assert (results / "stray.txt").read_text() == "stray\n"
