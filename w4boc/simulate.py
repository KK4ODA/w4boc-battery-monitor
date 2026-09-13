"""Simulation mode (`python main.py --simulate`): fake BMS and charger data
so the whole application — dashboard, mains detector, alerts, updater, APRS
formatting — can be exercised on a PC without the site hardware.

The dashboard exposes POST /api/sim/mains?on=0|1 in this mode to fake an
outage. No BLE, no email (unless secrets.toml is present), APRS only if
config enables it and an AGW server is reachable.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import date

from . import config
from .context import AppContext
from .jbd import BasicInfo

log = logging.getLogger("sim")


class SimState:
    """Fake site state. The toggles persist in the DB state table so a
    simulated outage survives the confirmation restart, like a real one."""

    def __init__(self, storage=None):
        self._s = storage
        self.soc = 100.0
        self.voltage = 13.78
        g = (lambda k, d: (storage.get_state(k) or d)) if storage else (lambda k, d: d)
        self._mains_on = g("sim_mains_on", "1") == "1"
        self._charger_ble = True      # deliberately NOT persisted: a restart revives a dead scanner
        self._ac_line = g("sim_ac_line", "unknown")

    def _put(self, key, value):
        if self._s is not None:
            self._s.set_state(key, value)

    @property
    def mains_on(self) -> bool:
        return self._mains_on

    @mains_on.setter
    def mains_on(self, v: bool):
        self._mains_on = bool(v)
        self._put("sim_mains_on", "1" if v else "0")

    @property
    def charger_ble(self) -> bool:      # False = scanner "dead" while mains on
        return self._charger_ble

    @charger_ble.setter
    def charger_ble(self, v: bool):
        self._charger_ble = bool(v)

    @property
    def ac_line(self) -> str:           # "offline" mimics a UPS reporting AC loss
        return self._ac_line

    @ac_line.setter
    def ac_line(self, v: str):
        self._ac_line = v
        self._put("sim_ac_line", v)

    def snapshot(self) -> dict:
        return {"mains_on": self.mains_on, "charger_ble": self.charger_ble,
                "soc": round(self.soc, 1), "voltage": round(self.voltage, 2),
                "ac_line": self.ac_line}


def _bms_info(sim: SimState, current: float) -> BasicInfo:
    return BasicInfo(
        pack_voltage=round(sim.voltage, 2),
        pack_current=round(current, 2),
        residual_capacity=round(280 * sim.soc / 100, 1),
        nominal_capacity=280.0,
        cycle_count=12,
        production_date=date(2025, 6, 1),
        balance_bitmap=0,
        protection_bitmap=0,
        protections=[],
        sw_version=0x20,
        soc_percent=int(round(sim.soc)),
        charge_fet_on=True,
        discharge_fet_on=True,
        cell_count=4,
        temperatures_c=[23.0 + random.uniform(-0.3, 0.3)],
    )


async def sim_bms_task(ctx: AppContext, period_s: int | None = None):
    sim = ctx.sim
    period = period_s or config.BMS_SAMPLE_PERIOD_S
    while True:
        if sim.mains_on:
            # charger carries the load; occasional STORAGE-mode hand-off blip
            current = random.choice([0.0, 0.0, 0.0, 0.92, -3.5])
            sim.soc = min(100.0, sim.soc + 0.5)
            sim.voltage = 13.78 if sim.soc >= 99 else 13.6 + 0.2 * sim.soc / 100
        else:
            current = -4.6 + random.uniform(-0.3, 0.3)
            sim.soc = max(0.0, sim.soc - 100.0 * period / (280 * 3600 / 4.6))
            sim.voltage = 12.9 + 0.5 * sim.soc / 100
        info = _bms_info(sim, current)
        cell = sim.voltage / 4
        cells = [round(cell + d, 3) for d in (0.002, 0.001, 0.0, -0.002)]
        ctx.storage.write_bms(info, cells)
        log.info(f"SIM BMS: {info.pack_voltage:.2f} V {info.pack_current:+.2f} A SoC {info.soc_percent}%")
        await asyncio.sleep(period)


async def sim_charger_task(ctx: AppContext, period_s: int = 20):
    sim = ctx.sim
    while True:
        if sim.mains_on and sim.charger_ble:
            i = 4.5 + random.uniform(-0.5, 2.5)
            ctx.storage.write_charger(13.8, round(i, 1), "STORAGE", "NO_ERROR")
            log.info(f"SIM Charger: STORAGE 13.8 V @ {i:.1f} A")
        await asyncio.sleep(period_s)
