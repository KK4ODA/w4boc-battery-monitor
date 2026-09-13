"""AGWPE client for soundmodem.

Connects to soundmodem's AGWPE TCP server (default port 8000), registers our
callsign, and sends APRS UI frames straight to RF — bypassing YAAC. soundmodem
assembles the AX.25 frame, keys PTT, and transmits.

We only implement what we need: connect, register callsign ('X' frame), send
UI frame with VIA path ('V' frame). No reception, no monitoring.

AGWPE binary header is 36 bytes, little-endian:
  offset 0..3   Port               uint32 (only low byte matters)
  offset 4      DataKind           ASCII letter ('X', 'V', etc.)
  offset 5      Reserved           0
  offset 6      PID                0xF0 for APRS (no L3 protocol)
  offset 7      Reserved           0
  offset 8..17  CallFrom           ASCII, null-padded to 10 bytes
  offset 18..27 CallTo             ASCII, null-padded to 10 bytes
  offset 28..31 DataLength         uint32 (length of data field that follows)
  offset 32..35 User               0
"""
from __future__ import annotations

import asyncio
import logging
import struct

log = logging.getLogger(__name__)

_HDR_FMT = "<I B B B B 10s 10s I I"
_HDR_LEN = struct.calcsize(_HDR_FMT)
assert _HDR_LEN == 36, _HDR_LEN


def _pad10(s: str) -> bytes:
    return s.encode("ascii", errors="replace")[:10].ljust(10, b"\x00")


def _header(kind: str, pid: int, call_from: str, call_to: str, data_len: int) -> bytes:
    return struct.pack(
        _HDR_FMT,
        0,                       # port (use AGW port 0 = first radio)
        ord(kind),               # data kind
        0,                       # reserved
        pid,                     # PID
        0,                       # reserved
        _pad10(call_from),
        _pad10(call_to),
        data_len,
        0,                       # user
    )


class AgwClient:
    """Persistent AGWPE client. Reconnects automatically on next send if dropped."""

    def __init__(self, host: str, port: int, callsign: str):
        self.host = host
        self.port = port
        self.callsign = callsign
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> bool:
        try:
            reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5.0,
            )
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            log.warning(
                f"AGW connect to {self.host}:{self.port} failed: "
                f"{type(e).__name__}: {e}"
            )
            self._writer = None
            return False
        # Register callsign with 'X' frame so the server knows who we are.
        self._writer.write(_header("X", 0, self.callsign, "", 0))
        await self._writer.drain()
        # AGW is full-duplex: we must drain whatever it sends back, otherwise
        # the kernel buffer fills and our writes eventually block.
        self._reader_task = asyncio.create_task(self._drain_reader(reader))
        log.info(f"AGW: connected to {self.host}:{self.port} as {self.callsign}")
        return True

    async def _drain_reader(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
        except Exception:
            pass

    async def send_ui(self, dest: str, vias: str, info: str) -> bool:
        """Send one APRS UI frame. `vias` is comma-separated (e.g. 'WIDE1-1,WIDE2-1')
        or empty. `info` is the APRS info field (no source/dest prefix)."""
        async with self._lock:
            if self._writer is None or self._writer.is_closing():
                if not await self._connect():
                    return False
            via_list = [v.strip() for v in vias.split(",") if v.strip()]
            data = bytes([len(via_list)])
            for v in via_list:
                data += _pad10(v)
            data += info.encode("ascii", errors="replace")
            try:
                self._writer.write(_header("V", 0xF0, self.callsign, dest, len(data)) + data)
                await self._writer.drain()
                return True
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                log.warning(
                    f"AGW write failed (will reconnect next send): "
                    f"{type(e).__name__}: {e}"
                )
                self._writer = None
                return False
