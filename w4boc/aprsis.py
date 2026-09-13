"""Direct APRS-IS uplink client.

Connects to an APRS-IS server (typically rotate.aprs2.net:14580), logs in with
our callsign + passcode, and sends pre-formed TNC2 lines. We never need to
receive anything — login banner is drained in the background.

NOTE: APRS-IS uplink requires Internet. The project's "fully offline" rule is
preserved by keeping the APRS-IS path optional: callers should treat send
failures as non-fatal and continue with RF (AGW) regardless.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class AprsIsClient:
    def __init__(
        self,
        host: str,
        port: int,
        callsign: str,
        passcode: int,
        software: str = "W4BOC-Battery",
        version: str = "1.0",
    ):
        self.host = host
        self.port = port
        self.callsign = callsign
        self.passcode = passcode
        self.software = software
        self.version = version
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> bool:
        try:
            reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=10.0,
            )
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            # Some socket errors stringify empty (especially TimeoutError).
            log.warning(
                f"APRS-IS connect to {self.host}:{self.port} failed: "
                f"{type(e).__name__}: {e}"
            )
            self._writer = None
            return False
        # Filter "t/m" = nothing (we only uplink). Servers ignore an absent filter
        # for tx-only clients but include it explicitly for politeness.
        login = (
            f"user {self.callsign} pass {self.passcode} "
            f"vers {self.software} {self.version} filter t/m\r\n"
        )
        self._writer.write(login.encode("ascii"))
        await self._writer.drain()
        self._reader_task = asyncio.create_task(self._drain_reader(reader))
        log.info(f"APRS-IS: connected to {self.host}:{self.port} as {self.callsign}")
        return True

    async def _drain_reader(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
        except Exception:
            pass

    async def send(self, line: str) -> bool:
        """Send one TNC2-format line ('CALL>TOCALL:info'). CRLF added if missing."""
        async with self._lock:
            if self._writer is None or self._writer.is_closing():
                if not await self._connect():
                    return False
            if not line.endswith("\r\n"):
                line = line.rstrip("\r\n") + "\r\n"
            try:
                self._writer.write(line.encode("ascii", errors="replace"))
                await self._writer.drain()
                return True
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                log.warning(
                    f"APRS-IS send failed (will reconnect next send): "
                    f"{type(e).__name__}: {e}"
                )
                self._writer = None
                return False
