"""Reference shape for the stack-dump contract (SPEC.md,
`stackdump-contract: v1`): a checker thread stamps each
health success; a watchdog thread observes the stamp's
age and dumps through a startup-opened handle."""

import faulthandler
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class StackDumpWatchdog:
    def __init__(self, name, health, dump_path, cadence_s=5.0,
                 threshold_s=15.0, repeat_s=60.0,
                 max_bytes=16 * 1024 * 1024):
        self._name = name
        self._health = health
        self._path = Path(dump_path)
        self._cadence = cadence_s
        self._threshold = threshold_s
        self._repeat = repeat_s
        self._max_bytes = max_bytes
        self._last_ok = time.monotonic()
        self._heartbeat = self._last_ok
        self._last_dump = 0.0
        self.last_dump_at = None
        self._handle = None

    def start(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")
        threading.Thread(target=self._check, daemon=True,
                         name="stackdump-check").start()
        threading.Thread(target=self._watch, daemon=True,
                         name="stackdump-watch").start()

    def _check(self):
        while True:
            self._heartbeat = time.monotonic()
            try:
                ok = self._health()
            except Exception:
                ok = False
            if ok:
                self._last_ok = time.monotonic()
            time.sleep(self._cadence)

    def _watch(self):
        while True:
            time.sleep(self._cadence)
            lag = time.monotonic() - self._last_ok
            now = time.monotonic()
            if lag >= self._threshold and \
                    now - self._last_dump >= self._repeat:
                self._dump(lag)
                self._last_dump = now

    def _dump(self, lag):
        if self._handle.tell() > self._max_bytes:
            self._handle.seek(0)
            self._handle.truncate()
        stamp = datetime.now(timezone.utc).isoformat()
        heartbeat_age = time.monotonic() - self._heartbeat
        self._handle.write(
            f"\n=== stackdump {stamp} server={self._name} "
            f"contract=v1 lag={lag:.1f}s "
            f"heartbeat_age={heartbeat_age:.1f}s ===\n")
        faulthandler.dump_traceback(file=self._handle)
        self._handle.flush()
        self.last_dump_at = stamp
