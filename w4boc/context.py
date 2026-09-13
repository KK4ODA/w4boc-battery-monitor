"""Shared runtime context: the one object the asyncio tasks, the dashboard
thread and the updater use to talk to each other."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from . import __version__
from .aprs import AprsBus
from .storage import Storage

log = logging.getLogger(__name__)

EXIT_CLEAN = 0
EXIT_RESTART = 2      # launcher restarts after a short delay
EXIT_UPDATE = 3       # launcher installs the staged update, then restarts


class AppContext:
    def __init__(self, storage: Storage, loop: asyncio.AbstractEventLoop, simulate: bool = False):
        self.storage = storage
        self.loop = loop
        self.simulate = simulate
        self.version = __version__
        self.started = datetime.now(timezone.utc)
        self.started_mono = time.monotonic()
        self.stop_event = asyncio.Event()
        self.exit_code: int = EXIT_CLEAN
        self.exit_reason: str = ""
        self.aprs_bus = AprsBus()
        self.mains = None          # mains.MainsTracker, set by app.py
        self.updater = None        # updater.Updater, set by app.py
        self.sim = None            # simulate.SimState in --simulate mode
        self.power_status = None   # power.PowerStatus, refreshed by evaluator
        self.dashboard_url = ""
        self._lock = threading.Lock()
        self._client_seen_mono: float | None = None
        self._loop_alive_mono: float = time.monotonic()
        self.browser_opened: str = ""   # "opened" | "skipped" | ""

    # ---- lifetime ----

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_mono

    def request_exit(self, code: int, reason: str):
        """Thread-safe: may be called from the dashboard thread."""
        def _do():
            if self.stop_event.is_set():
                return
            self.exit_code, self.exit_reason = code, reason
            log.warning(f"exit requested: code={code} ({reason})")
            self.stop_event.set()
        try:
            self.loop.call_soon_threadsafe(_do)
        except RuntimeError:   # loop already closed
            _do()

    # ---- dashboard client presence (used by the browser-open check) ----

    def note_client(self):
        with self._lock:
            self._client_seen_mono = time.monotonic()

    def client_seen_ago(self) -> float | None:
        with self._lock:
            if self._client_seen_mono is None:
                return None
            return time.monotonic() - self._client_seen_mono

    # ---- event-loop liveness (thread watchdog) ----

    def loop_alive(self):
        with self._lock:
            self._loop_alive_mono = time.monotonic()

    def loop_stalled_s(self) -> float:
        with self._lock:
            return time.monotonic() - self._loop_alive_mono
