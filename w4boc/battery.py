"""Battery runtime estimate: how long until the BMS cuts the load off.

    remaining hours = residual capacity (Ah, from the BMS) / average discharge
                      current over the last `window_min` minutes

Only meaningful while the battery is actually carrying the load (average
current below `min_discharge_a`); otherwise `estimate_runtime()` returns
None. The BMS reports residual Ah relative to its own 0 % point, which is
where it opens the discharge FET — so this is time-to-cutoff, not
time-to-empty-chemistry. The monitor PC runs from the same battery, so it is
also the time until monitoring stops.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .mains import fmt_duration
from .storage import Storage

DEFAULT_WINDOW_MIN = 10
MIN_DISCHARGE_A = 0.5      # average draw below this (in magnitude) -> "not discharging"
MIN_SAMPLES = 3


@dataclass
class RuntimeEstimate:
    hours: float
    avg_current_a: float      # negative = discharging
    residual_ah: float
    soc_pct: int | None
    samples: int
    window_min: int

    @property
    def seconds(self) -> int:
        return int(self.hours * 3600)

    @property
    def text(self) -> str:
        """e.g. '≈ 2d 8h at 4.6 A'"""
        return f"≈ {fmt_duration(self.seconds)} at {abs(self.avg_current_a):.1f} A"

    @property
    def short(self) -> str:
        """ASCII, for APRS status text: '~58h' / '~4h10m' / '~35m'"""
        s = self.seconds
        if s >= 48 * 3600:
            return f"~{s // 3600}h"
        if s >= 3600:
            m = (s % 3600) // 60
            return f"~{s // 3600}h{m:02d}m" if m else f"~{s // 3600}h"
        return f"~{max(1, s // 60)}m"


def estimate_runtime(storage: Storage, window_min: int = DEFAULT_WINDOW_MIN,
                     now: datetime | None = None) -> RuntimeEstimate | None:
    bms = storage.latest_bms()
    if not bms or bms.get("residual_ah") is None:
        return None
    now = now or datetime.now(timezone.utc)
    avg, n = storage.avg_current_since(now - timedelta(minutes=window_min))
    if avg is None or n < MIN_SAMPLES or avg > -MIN_DISCHARGE_A:
        return None
    residual = float(bms["residual_ah"])
    if residual <= 0:
        return RuntimeEstimate(0.0, avg, residual, bms.get("soc_pct"), n, window_min)
    return RuntimeEstimate(residual / abs(avg), avg, residual, bms.get("soc_pct"), n, window_min)
