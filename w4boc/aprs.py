"""APRS telemetry — direct AGW (RF) and direct APRS-IS (Internet).

We bypass YAAC entirely. RF goes via soundmodem's AGWPE TCP server
(localhost:8000); Internet goes via a normal APRS-IS uplink. APRS-IS is
optional (passcode 0 → disabled) so the whole system still functions on
an offline network — RF telemetry continues regardless.

Packet kinds emitted (formats are locked by tests/test_aprs_golden.py —
they match frames that have been decoding correctly on aprs.fi since 2026-04):
  1. Position report — lat/lon + live comment with battery state. Every
     ~15 min so the station appears on aprs.fi with a current snapshot.
  2. Telemetry data — `T#SSS,aaa,bbb,ccc,ddd,eee,bbbbbbbb`. Every ~10 min.
  3. Telemetry headers — PARM/UNIT/EQNS/BITS message frames addressed to
     ourselves. Once at startup + hourly.
  4. Status report — `>text`, only on mains-power transitions (optional).

Analog channels (0-255 raw, decoded via EQNS as real_value = a·x² + b·x + c):
  1 Vbat   (Vdc)  b=0.1 c=0      -> 0 … 25.5 V,  0.1 V resolution
  2 Ibat   (Adc)  b=1   c=-128   -> -128 … +127 A, 1 A resolution (signed)
  3 SoC    (%)    b=1   c=0      -> 0 … 100 %
  4 Tbat   (degC) b=0.5 c=-20    -> -20 … +107 °C, 0.5 °C resolution
  5 IchgO  (Adc)  b=0.2 c=0      -> 0 … 51 A, 0.2 A resolution

Binary bits (leftmost = bit 1):
  1 ChargeFET    1 = on
  2 DischargeFET 1 = on
  3 MainsPresent 1 = charger seen within the mains "fast" window and no
                     outage declared by the mains detector
  4 BMSonline    1 = BMS sample in last 2 min
  5 Prot-OK      1 = no protections tripped  (inverted so 1 is healthy)
  6 Chg-OK       1 = charger error field is NO_ERROR  (inverted)
  7 SoC-Degraded 1 = SoC >= SOC_DEGRADED
  8 SoC-Urgent   1 = SoC >= SOC_URGENT
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from . import config
from .agwpe import AgwClient
from .aprsis import AprsIsClient
from .storage import Storage

log = logging.getLogger(__name__)

TOCALL = config.APRS_TOCALL        # "APZBAT": APZ = experimental/homebrew, BAT = battery monitor

# EQNS coefficients for the five analog channels (a, b, c)
EQNS_COEFFS = [
    (0, 0.1, 0),     # Vbat
    (0, 1, -128),    # Ibat
    (0, 1, 0),       # SoC
    (0, 0.5, -20),   # Tbat
    (0, 0.2, 0),     # IchgOut
]

PARM_NAMES = ["Vbat", "Ibat", "SoC", "Tbat", "IchgO",
              "ChgFET", "DisFET", "Mains", "BMSok", "ProtOK", "ChgOK", "Ok50", "Ok30"]
UNIT_NAMES = ["Vdc", "Adc", "Pct", "degC", "Adc",
              "on", "on", "ok", "ok", "ok", "ok", "ok", "ok"]
BITS_SENSE = "11111111"   # 1 = "active" sense for each bit; we report healthy=1
PROJECT_NAME = config.APRS_PROJECT_NAME   # "<site> Battery" unless overridden

# Minimum spacing between out-of-cadence (event-driven) frames of one kind.
EVENT_MIN_SPACING_S = 60


# ---------- formatters ----------

def latlon_aprs(lat: float, lon: float) -> str:
    """Return APRS uncompressed position string 'DDMM.hhN/DDDMM.hhW'."""
    lat_hem = "N" if lat >= 0 else "S"
    lon_hem = "E" if lon >= 0 else "W"
    lat = abs(lat); lon = abs(lon)
    lat_deg = int(lat); lon_deg = int(lon)
    lat_min = (lat - lat_deg) * 60
    lon_min = (lon - lon_deg) * 60
    return (f"{lat_deg:02d}{lat_min:05.2f}{lat_hem}"
            f"{config.APRS_SYM_TABLE}"
            f"{lon_deg:03d}{lon_min:05.2f}{lon_hem}"
            f"{config.APRS_SYM_CODE}")


def encode_analog(value: float | None, a: float, b: float, c: float) -> int:
    """Invert EQNS to go from real value → 0-255 raw. Clamps on overflow."""
    if value is None:
        return 0
    if a != 0:
        raise NotImplementedError("quadratic EQNS not supported")
    raw = round((value - c) / b)
    return max(0, min(255, raw))


def _age(sample: dict | None, now: datetime) -> timedelta | None:
    if not sample or not sample.get("ts"):
        return None
    ts = datetime.fromisoformat(sample["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts


def _build_bits(bms: dict | None, chg: dict | None, mains_lost: bool = False) -> str:
    """8-bit string, leftmost = bit 1. 1 = healthy/active."""
    now = datetime.now(timezone.utc)
    fresh = timedelta(minutes=2)
    mains_window = timedelta(minutes=config.MAINS_FAST_MINUTES)

    bms_age = _age(bms, now)
    chg_age = _age(chg, now)
    bms_fresh = bms_age is not None and bms_age < fresh
    chg_recent = chg_age is not None and chg_age < mains_window

    b = [
        int(bool(bms and bms["charge_fet"])),         # 1 ChargeFET
        int(bool(bms and bms["discharge_fet"])),      # 2 DischargeFET
        int(bool(chg_recent) and not mains_lost),      # 3 MainsPresent
        int(bool(bms_fresh)),                          # 4 BMSonline
        int(not (bms and bms["protections"])),         # 5 Prot-OK (1=no protections)
        int(not (chg and chg.get("error") not in ("", "NO_ERROR", None))),  # 6 Chg-OK
        int(not (bms and bms["soc_pct"] is not None and bms["soc_pct"] < config.SOC_DEGRADED)),  # 7
        int(not (bms and bms["soc_pct"] is not None and bms["soc_pct"] < config.SOC_URGENT)),    # 8
    ]
    return "".join(str(x) for x in b)


def _fmt_num(n: float) -> str:
    """Trim trailing .0 for cleaner EQNS output."""
    if n == int(n):
        return str(int(n))
    return f"{n:g}"


def build_data_info(seq: int, bms: dict | None, chg: dict | None,
                    mains_lost: bool = False) -> str:
    """Return APRS info field for one T# data frame."""
    if bms:
        analogs = [
            encode_analog(bms["pack_voltage"], *EQNS_COEFFS[0]),
            encode_analog(bms["pack_current"], *EQNS_COEFFS[1]),
            encode_analog(bms["soc_pct"],      *EQNS_COEFFS[2]),
            encode_analog(bms["temp_c"],       *EQNS_COEFFS[3]),
        ]
    else:
        analogs = [0, 128, 0, 40]
    if chg:
        analogs.append(encode_analog(chg["current"], *EQNS_COEFFS[4]))
    else:
        analogs.append(0)
    bits = _build_bits(bms, chg, mains_lost)
    return f"T#{seq % 1000:03d}," + ",".join(f"{v:03d}" for v in analogs) + f",{bits}"


def build_header_infos() -> list[str]:
    """Four PARM/UNIT/EQNS/BITS info fields addressed to ourselves."""
    addr = f"{config.APRS_CALLSIGN:<9}"  # spec: 9 chars, space-padded right
    parm = f":{addr}:PARM." + ",".join(PARM_NAMES)
    unit = f":{addr}:UNIT." + ",".join(UNIT_NAMES)
    eqns_vals: list[str] = []
    for (a, b, c) in EQNS_COEFFS:
        eqns_vals += [_fmt_num(a), _fmt_num(b), _fmt_num(c)]
    eqns = f":{addr}:EQNS." + ",".join(eqns_vals)
    bits = f":{addr}:BITS.{BITS_SENSE},{PROJECT_NAME}"
    return [parm, unit, eqns, bits]


def build_position_info(bms: dict | None, mains_lost: bool = False) -> str:
    """`=lat/lon-` with a short live-state comment. While a mains outage is
    declared (and the comment flag is enabled) the comment ends in
    ' MAINS LOST'; otherwise it is byte-identical to the v1 format."""
    pos = latlon_aprs(config.APRS_LAT, config.APRS_LON)
    if bms and bms["soc_pct"] is not None:
        temp = f" {bms['temp_c']:.0f}C" if bms.get("temp_c") is not None else ""
        comment = f"{config.APRS_COMMENT_PREFIX} {bms['pack_voltage']:.2f}V {bms['soc_pct']}%{temp}"
    else:
        comment = f"{config.SITE_NAME} battery monitor"
    if mains_lost and config.MAINS_APRS_COMMENT:
        comment += " MAINS LOST"
    return f"={pos}{comment[:43]}"


def build_status_info(text: str) -> str:
    """APRS status report (data type '>'). Max 62 chars of text; '|' and '~'
    are reserved in status reports and get replaced."""
    clean = text.replace("|", "/").replace("~", "-")
    return ">" + clean[:62]


def tnc2_for_aprsis(info: str) -> str:
    """Format an info field as a TNC2 line for APRS-IS uplink. We omit any
    digipeat path — APRS-IS doesn't transmit on RF, so the path is meaningless
    there and only adds noise to the relayed packet."""
    return f"{config.APRS_CALLSIGN}>{TOCALL}:{info}\r\n"


# ---------- event bus ----------

class AprsBus:
    """Lets other tasks nudge the APRS task without touching its cadence
    logic: `kick()` asks for an immediate position + data frame, and
    `queue_status()` enqueues a one-off status report."""

    def __init__(self):
        self.wake = asyncio.Event()
        self.kick_requested = False
        self.status_queue: deque[str] = deque()
        self.mains_lost = False   # mirrored by the evaluator every tick

    def kick(self):
        self.kick_requested = True
        self.wake.set()

    def queue_status(self, text: str):
        self.status_queue.append(text)
        self.wake.set()


# ---------- task ----------

async def aprs_task(storage: Storage, bus: AprsBus | None = None):
    if not config.APRS_ENABLED:
        log.info("APRS task: disabled in config, not running")
        return
    bus = bus or AprsBus()

    agw = AgwClient(config.APRS_AGW_HOST, config.APRS_AGW_PORT, config.APRS_CALLSIGN)
    aprsis = None
    if config.APRS_APRSIS_PASSCODE:
        aprsis = AprsIsClient(
            config.APRS_APRSIS_HOST,
            config.APRS_APRSIS_PORT,
            config.APRS_CALLSIGN,
            config.APRS_APRSIS_PASSCODE,
            software=f"{config.SITE_NAME}-Battery",
        )

    log.info(
        f"APRS task: sending as {config.APRS_CALLSIGN}, "
        f"RF via AGW {config.APRS_AGW_HOST}:{config.APRS_AGW_PORT}, "
        f"APRS-IS {'enabled' if aprsis else 'disabled'}, path {config.APRS_PATH}"
    )

    async def emit(info: str, label: str) -> tuple[bool, bool]:
        """Send one info field to RF and APRS-IS. Returns (rf_ok, is_ok).
        APRS-IS failure is non-fatal — RF is the priority."""
        rf_ok = await agw.send_ui(TOCALL, config.APRS_PATH, info)
        is_ok = True
        if aprsis is not None:
            is_ok = await aprsis.send(tnc2_for_aprsis(info))
        log.info(f"APRS {label}: rf={rf_ok} is={is_ok}  {info[:90]}")
        return rf_ok, is_ok

    # Restore sequence counter from KV storage so it keeps climbing across restarts.
    seq = int(storage.get_state("aprs_seq") or 0)
    last_headers = 0.0
    last_position = 0.0
    last_data = 0.0

    loop = asyncio.get_event_loop()
    # Small initial pause so other tasks (BMS, charger) get a first sample in
    # before we try to build telemetry frames.
    await asyncio.sleep(5)

    async def send_position(bms):
        nonlocal last_position
        await emit(build_position_info(bms, bus.mains_lost), "position")
        last_position = loop.time()
        storage.log_event("aprs_sent", "info", "position")

    async def send_data(bms, chg):
        nonlocal seq, last_data
        info = build_data_info(seq, bms, chg, bus.mains_lost)
        await emit(info, "data")
        last_data = loop.time()
        seq = (seq + 1) % 1000
        storage.set_state("aprs_seq", str(seq))
        storage.log_event("aprs_sent", "info", f"T#{(seq - 1) % 1000:03d}")

    while True:
        # Clear first so a kick that arrives while we are sending is not lost:
        # it re-sets the event and the wait below returns immediately.
        bus.wake.clear()
        now_mono = loop.time()
        bms = storage.latest_bms()
        chg = storage.latest_charger()

        # Headers
        if now_mono - last_headers >= config.APRS_HEADERS_INTERVAL_S:
            for info in build_header_infos():
                await emit(info, "header")
                await asyncio.sleep(1)  # gentle spacing between four frames
            last_headers = now_mono
            storage.log_event("aprs_sent", "info", "headers")

        # Position
        if config.APRS_SEND_POSITION and now_mono - last_position >= config.APRS_POSITION_INTERVAL_S:
            if last_position == 0:
                await asyncio.sleep(10)
            await send_position(bms)

        # Data
        if now_mono - last_data >= config.APRS_DATA_INTERVAL_S:
            if last_data == 0:
                await asyncio.sleep(20)
            await send_data(bms, chg)

        # Event-driven frames (mains transitions). Same emit path, just
        # out of cadence and rate-limited so a flapping input can't spam RF.
        while bus.status_queue:
            text = bus.status_queue.popleft()
            await emit(build_status_info(text), "status")
            storage.log_event("aprs_sent", "info", f"status: {text}")
            await asyncio.sleep(1)
        if bus.kick_requested:
            bus.kick_requested = False
            now_mono = loop.time()
            if config.APRS_SEND_POSITION and now_mono - last_position >= EVENT_MIN_SPACING_S:
                await send_position(bms)
                await asyncio.sleep(1)
            if loop.time() - last_data >= EVENT_MIN_SPACING_S:
                await send_data(bms, chg)

        try:
            await asyncio.wait_for(bus.wake.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


# ---------- self-test ----------

if __name__ == "__main__":
    # Pure-function sanity checks, no AGW/APRS-IS needed.
    print("latlon:", latlon_aprs(config.APRS_LAT, config.APRS_LON))
    print()
    for info in build_header_infos():
        print(info)
    print()
    fake_bms = {"pack_voltage": 13.78, "pack_current": 0.0, "soc_pct": 99,
                "temp_c": 21.4, "charge_fet": 1, "discharge_fet": 1,
                "protections": [], "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    fake_chg = {"voltage": 13.8, "current": 4.4, "state": "STORAGE", "error": "NO_ERROR",
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    print(build_position_info(fake_bms))
    print(build_data_info(42, fake_bms, fake_chg))
    print(build_status_info("AC MAINS LOST 13.42V 96% -4.6A"))
    print()
    print("APRS-IS form:", tnc2_for_aprsis(build_data_info(42, fake_bms, fake_chg)).strip())
