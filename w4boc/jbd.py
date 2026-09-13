"""JBD / Xiaoxiang BMS protocol codec.

Frame layout:
  Request:  DD A5 CMD LEN [DATA...] CHK_H CHK_L 77
  Response: DD CMD STATUS LEN [DATA...] CHK_H CHK_L 77

CHK = (0x10000 - sum(bytes from CMD through last DATA byte)) & 0xFFFF,
encoded big-endian. For response frames the covered range is STATUS..DATA.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date

START = 0xDD
END = 0x77
READ = 0xA5
WRITE = 0x5A
OK = 0x00

CMD_BASIC_INFO = 0x03
CMD_CELL_VOLTAGES = 0x04
CMD_HARDWARE_NAME = 0x05

PROTECTION_BITS = [
    "cell_over_voltage",
    "cell_under_voltage",
    "pack_over_voltage",
    "pack_under_voltage",
    "charge_over_temp",
    "charge_under_temp",
    "discharge_over_temp",
    "discharge_under_temp",
    "charge_over_current",
    "discharge_over_current",
    "short_circuit",
    "ic_error",
    "mos_lock",
]


def encode_read(cmd: int) -> bytes:
    body = bytes([cmd, 0x00])
    chk = (0x10000 - sum(body)) & 0xFFFF
    return bytes([START, READ]) + body + chk.to_bytes(2, "big") + bytes([END])


def parse_frame(frame: bytes) -> tuple[int, int, bytes]:
    """Validate a response frame; return (cmd, status, payload)."""
    if len(frame) < 7 or frame[0] != START or frame[-1] != END:
        raise ValueError(f"bad framing: {frame.hex()}")
    cmd, status, length = frame[1], frame[2], frame[3]
    if len(frame) != 4 + length + 3:
        raise ValueError(f"length mismatch: header says {length}, frame is {len(frame)} bytes")
    payload = frame[4 : 4 + length]
    chk_expected = int.from_bytes(frame[4 + length : 6 + length], "big")
    chk_actual = (0x10000 - sum(frame[2 : 4 + length])) & 0xFFFF
    if chk_expected != chk_actual:
        raise ValueError(f"bad checksum: expected {chk_actual:04x}, got {chk_expected:04x}")
    return cmd, status, payload


@dataclass
class BasicInfo:
    pack_voltage: float       # V
    pack_current: float       # A, positive = charging
    residual_capacity: float  # Ah
    nominal_capacity: float   # Ah
    cycle_count: int
    production_date: date | None
    balance_bitmap: int
    protection_bitmap: int
    protections: list[str]
    sw_version: int
    soc_percent: int
    charge_fet_on: bool
    discharge_fet_on: bool
    cell_count: int
    temperatures_c: list[float]

    @property
    def power_w(self) -> float:
        return self.pack_voltage * self.pack_current


def parse_basic_info(payload: bytes) -> BasicInfo:
    if len(payload) < 23:
        raise ValueError(f"basic-info payload too short: {len(payload)}")
    p = payload
    prod_raw = int.from_bytes(p[10:12], "big")
    try:
        prod = date(2000 + (prod_raw >> 9), (prod_raw >> 5) & 0x0F, prod_raw & 0x1F)
    except ValueError:
        prod = None
    balance = int.from_bytes(p[12:14], "big") | (int.from_bytes(p[14:16], "big") << 16)
    protection = int.from_bytes(p[16:18], "big")
    fet = p[20]
    ntc_count = p[22]
    if len(p) < 23 + 2 * ntc_count:
        raise ValueError(f"temperature data truncated (ntc={ntc_count}, payload={len(p)})")
    temps = [
        (int.from_bytes(p[23 + 2 * i : 25 + 2 * i], "big") - 2731) / 10.0
        for i in range(ntc_count)
    ]
    return BasicInfo(
        pack_voltage=int.from_bytes(p[0:2], "big") / 100.0,
        pack_current=int.from_bytes(p[2:4], "big", signed=True) / 100.0,
        residual_capacity=int.from_bytes(p[4:6], "big") / 100.0,
        nominal_capacity=int.from_bytes(p[6:8], "big") / 100.0,
        cycle_count=int.from_bytes(p[8:10], "big"),
        production_date=prod,
        balance_bitmap=balance,
        protection_bitmap=protection,
        protections=[n for i, n in enumerate(PROTECTION_BITS) if protection & (1 << i)],
        sw_version=p[18],
        soc_percent=p[19],
        charge_fet_on=bool(fet & 0x01),
        discharge_fet_on=bool(fet & 0x02),
        cell_count=p[21],
        temperatures_c=temps,
    )


def parse_cell_voltages(payload: bytes) -> list[float]:
    return [int.from_bytes(payload[i : i + 2], "big") / 1000.0 for i in range(0, len(payload), 2)]


if __name__ == "__main__":
    assert encode_read(CMD_BASIC_INFO).hex() == "dda50300fffd77"
    assert encode_read(CMD_CELL_VOLTAGES).hex() == "dda50400fffc77"
    print("jbd codec self-checks: OK")
