"""Provisioning on orphan adoption, at the CLI against the
fake colab CLI.

Pins the reachability half of the retry contract on the
operator's restore path: adopting an orphan is a first
registration, so it fires the provisioning ensure and the
acquire result carries the ensure's answer. Before the fix,
adopt_orphan never fired the ensure at all and the result had
no provisioning key. The CLI fixture config disables
provisioning (the fake CLI executes exec'd scripts for real,
so the payload must never run inside a suite — see
env_fixtures._disable_provisioning), so the answer pinned
here is "disabled": its presence is the reachability proof,
and the window/verdict values are pinned by the in-process
unit suites (tests/test_provisioning_unit.py,
tests/test_lease_unit/test_provisioning_adopt.py).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fake_harness import lease_env, make_orphan, plant_cli_kit_config, run_lease  # noqa: F401


def _plant_failed_stamp(env, session, *, attempted_ago):
    attempted = datetime.now(timezone.utc) - attempted_ago
    stamp = {"attempted_at": attempted.isoformat(), "state": "verdict", "chromium": "failed", "install_rc": 100}
    var = Path(env["COLAB_RUNNER_VAR"])
    var.mkdir(parents=True, exist_ok=True)
    (var / f"provision-{session}.json").write_text(json.dumps(stamp))


def _adopt_with_stamp(tmp_path, session, attempted_ago):
    env = lease_env(tmp_path)
    make_orphan(env, session)
    now = datetime.now(timezone.utc)
    plant_cli_kit_config(env, session, deadline=now + timedelta(hours=6), acquired_at=now - timedelta(hours=1))
    _plant_failed_stamp(env, session, attempted_ago=attempted_ago)
    return run_lease(env, "adopt", session, "--caller", "alpha")


def test_adopt_orphan_fires_the_provisioning_ensure(tmp_path):
    out = _adopt_with_stamp(tmp_path, "stray-prov-1", timedelta(hours=2))
    assert out["action"] == "adopted"
    assert out["provisioning"] == "disabled"


def test_adopt_orphan_without_a_stamp_also_fires_it(tmp_path):
    env = lease_env(tmp_path)
    make_orphan(env, "stray-prov-2")
    now = datetime.now(timezone.utc)
    plant_cli_kit_config(env, "stray-prov-2", deadline=now + timedelta(hours=6), acquired_at=now - timedelta(hours=1))
    out = run_lease(env, "adopt", "stray-prov-2", "--caller", "alpha")
    assert out["action"] == "adopted"
    assert out["provisioning"] == "disabled"
