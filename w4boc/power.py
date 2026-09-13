"""Host power facts from Windows: AC-line status (instant mains detection when
the PC sits on a USB-connected UPS), system boot time, and whether the last
shutdown was unexpected (System event log 6008/41).

Everything here is best-effort and returns "unknown" on non-Windows hosts or
on any failure — the mains detector treats unknown as "no opinion".
"""
from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


@dataclass
class PowerStatus:
    ac_line: str            # "online" | "offline" | "unknown"
    has_battery: bool       # True if Windows reports a system battery / UPS
    battery_pct: int | None
    source: str             # human description


def _unknown(why: str) -> PowerStatus:
    return PowerStatus("unknown", False, None, why)


def system_power_status() -> PowerStatus:
    """Wrap kernel32.GetSystemPowerStatus. On a desktop without a UPS the
    OS reports ACLineStatus=1 and BatteryFlag=128 (no system battery) —
    we return 'unknown' in that case because the value carries no
    information about the site's mains."""
    if sys.platform != "win32":
        return _unknown("not windows")

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", ctypes.c_uint32),
            ("BatteryFullLifeTime", ctypes.c_uint32),
        ]

    try:
        sps = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            return _unknown("GetSystemPowerStatus failed")
    except Exception as e:  # pragma: no cover - defensive
        return _unknown(f"GetSystemPowerStatus error: {e}")

    has_battery = not (sps.BatteryFlag & 128) and sps.BatteryFlag != 255
    pct = sps.BatteryLifePercent if sps.BatteryLifePercent != 255 else None
    if not has_battery:
        return PowerStatus("unknown", False, None, "no UPS/battery reported by Windows")
    if sps.ACLineStatus == 0:
        return PowerStatus("offline", True, pct, "Windows: on battery/UPS")
    if sps.ACLineStatus == 1:
        return PowerStatus("online", True, pct, "Windows: AC online")
    return PowerStatus("unknown", True, pct, "Windows: AC status unknown")


def boot_time() -> datetime | None:
    """System boot time (UTC) from the tick counter."""
    if sys.platform != "win32":
        return None
    try:
        ms = ctypes.windll.kernel32.GetTickCount64()
        return datetime.now(timezone.utc) - timedelta(milliseconds=int(ms))
    except Exception:
        return None


def last_shutdown_was_unexpected(hours: int = 48) -> tuple[bool | None, str]:
    """Query the System event log for an unexpected-shutdown record newer
    than the last clean-shutdown record. Returns (None, reason) when the
    answer cannot be determined. Runs PowerShell, so allow ~2 s."""
    if sys.platform != "win32":
        return None, "not windows"
    script = (
        "$e = Get-WinEvent -FilterHashtable @{LogName='System'; Id=6008,41,1074; "
        f"StartTime=(Get-Date).AddHours(-{int(hours)})}} -ErrorAction SilentlyContinue "
        "| Sort-Object TimeCreated -Descending | Select-Object -First 1; "
        "if ($e) { '{0}|{1:o}' -f $e.Id, $e.TimeCreated } else { 'none' }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"event log query failed: {type(e).__name__}"
    out = (r.stdout or "").strip()
    if not out or out == "none":
        return None, "no shutdown events in window"
    try:
        eid, when = out.split("|", 1)
        eid = int(eid)
    except ValueError:
        return None, f"unparsable event log output: {out[:60]}"
    if eid in (6008, 41):
        return True, f"Windows event {eid} (unexpected shutdown) at {when[:19]}"
    return False, f"Windows event {eid} (clean shutdown/restart) at {when[:19]}"


def monotonic_s() -> float:
    return time.monotonic()
