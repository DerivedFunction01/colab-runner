"""Stack-dump contract tests for the lease panel
(stackdump-contract: v1, clause 5): (a) a health path parked
past the threshold produces a dump naming the parked frame,
with the watchdog never blocking on the parked path's locks;
(b) a healthy server produces no dump over several cadences;
(c) the dump file's size cap holds under repeated lag events.
Plus the panel's seams: the config loader's defaults/overrides
and the state-payload timestamp field's health callable.
"""

import importlib.util
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "ui"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(UI))
stackdump = _load("ui_stackdump", UI / "stackdump.py")
server = _load("ui_server_stackdump", UI / "server.py")


def wait_for(predicate, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_parked_health_dumps_the_parked_frame(tmp_path):
    gate = threading.Event()
    held = threading.Lock()

    def parked_health():
        with held:
            gate.wait(30)
        return True

    watchdog = stackdump.StackDumpWatchdog(
        "test-panel", parked_health, tmp_path / "stackdumps.log",
        cadence_s=0.05, threshold_s=0.3, repeat_s=0.25,
    )
    watchdog.start()
    try:
        assert wait_for(lambda: watchdog.last_dump_at is not None, 5)
        # The dump landed while the health path still holds its
        # lock: the watchdog never blocked on it.
        assert held.locked()
        text = (tmp_path / "stackdumps.log").read_text()
        assert "=== stackdump" in text
        assert "server=test-panel" in text
        assert "contract=v1" in text
        assert "parked_health" in text
    finally:
        gate.set()


def test_healthy_server_produces_no_dump(tmp_path):
    watchdog = stackdump.StackDumpWatchdog(
        "test-panel", lambda: True, tmp_path / "stackdumps.log",
        cadence_s=0.05, threshold_s=0.3, repeat_s=0.25,
    )
    watchdog.start()
    time.sleep(1.0)  # several cadences, past the threshold span
    assert watchdog.last_dump_at is None
    assert "=== stackdump" not in (tmp_path / "stackdumps.log").read_text()


def test_size_cap_holds_under_repeated_lag(tmp_path):
    gate = threading.Event()

    def parked_health():
        gate.wait(30)
        return True

    measure_path = tmp_path / "measure.log"
    measure = stackdump.StackDumpWatchdog(
        "test-panel", parked_health, measure_path,
        cadence_s=0.05, threshold_s=0.2, repeat_s=60.0,
    )
    measure.start()
    capped_path = tmp_path / "capped.log"
    capped = None
    try:
        assert wait_for(lambda: measure.last_dump_at is not None, 5)
        one_dump = measure_path.stat().st_size
        assert one_dump > 0
        capped = stackdump.StackDumpWatchdog(
            "test-panel", parked_health, capped_path,
            cadence_s=0.05, threshold_s=0.2, repeat_s=0.15,
            max_bytes=one_dump,
        )
        capped.start()
        stamps = set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and len(stamps) < 3:
            if capped.last_dump_at is not None:
                stamps.add(capped.last_dump_at)
            time.sleep(0.02)
        assert len(stamps) >= 3  # dumps kept repeating under the cap
        size = capped_path.stat().st_size
        # Bound: at most the cap plus one dump written past it
        # before the next truncate (stack sizes vary slightly).
        assert 0 < size <= one_dump + one_dump + 1024
        assert "=== stackdump" in capped_path.read_text()
    finally:
        gate.set()


def test_stackdump_config_defaults_and_overrides(tmp_path, monkeypatch):
    missing = tmp_path / "absent.json"
    monkeypatch.setattr(server, "CONFIG_PATH", missing)
    knobs = server.load_stackdump_config()
    assert knobs == {"cadence_s": 5.0, "threshold_s": 15.0, "repeat_s": 60.0, "max_bytes": 16 * 1024 * 1024}
    custom = tmp_path / "config.json"
    custom.write_text('{"stackdump_cadence_s": 2, "stackdump_threshold_s": 9}')
    monkeypatch.setattr(server, "CONFIG_PATH", custom)
    knobs = server.load_stackdump_config()
    assert knobs["cadence_s"] == 2.0
    assert knobs["threshold_s"] == 9.0
    assert knobs["repeat_s"] == 60.0
    assert knobs["max_bytes"] == 16 * 1024 * 1024


def test_stackdump_health_reads_the_state_path(monkeypatch):
    monkeypatch.setattr(server, "run_core", lambda *args: (0, {"records": []}, ""))
    assert server.stackdump_health() is True
    monkeypatch.setattr(server, "run_core", lambda *args: (1, None, "boom"))
    assert server.stackdump_health() is False
