"""Live read of the W4BOC BMS (JBD/Xiaoxiang) via BLE. Prints one snapshot and exits.

Run: python bms.py
"""
import asyncio
import sys
from bleak import BleakClient, BleakScanner
from . import jbd
from . import config

sys.stdout.reconfigure(encoding="utf-8")

WRITE_CHAR = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ff01-0000-1000-8000-00805f9b34fb"


class JbdReader:
    def __init__(self, client: BleakClient):
        self.client = client
        self._buf = bytearray()
        self._event = asyncio.Event()
        self._frame: bytes | None = None

    async def start(self):
        await self.client.start_notify(NOTIFY_CHAR, self._on_notify)
        await asyncio.sleep(0.5)  # BMS needs a beat after CCCD write before it will answer

    def _on_notify(self, _sender, data: bytearray):
        self._buf.extend(data)
        # Align on START
        if self._buf and self._buf[0] != jbd.START:
            idx = self._buf.find(bytes([jbd.START]))
            if idx < 0:
                self._buf.clear()
                return
            del self._buf[:idx]
        if len(self._buf) < 4:
            return
        total = 4 + self._buf[3] + 3  # start,cmd,status,length + payload + chk(2) + end
        if len(self._buf) >= total:
            if self._buf[total - 1] == jbd.END:
                self._frame = bytes(self._buf[:total])
                del self._buf[:total]
                self._event.set()
            else:
                del self._buf[0]  # resync on next scan

    async def request(self, cmd: int, timeout: float = 5.0) -> bytes:
        self._event.clear()
        self._frame = None
        await self.client.write_gatt_char(WRITE_CHAR, jbd.encode_read(cmd), response=False)
        await asyncio.wait_for(self._event.wait(), timeout)
        assert self._frame is not None
        return self._frame

    async def basic_info(self) -> jbd.BasicInfo:
        cmd, status, payload = jbd.parse_frame(await self.request(jbd.CMD_BASIC_INFO))
        if status != jbd.OK:
            raise RuntimeError(f"BMS rejected basic-info: status=0x{status:02x}")
        return jbd.parse_basic_info(payload)

    async def cell_voltages(self) -> list[float]:
        cmd, status, payload = jbd.parse_frame(await self.request(jbd.CMD_CELL_VOLTAGES))
        if status != jbd.OK:
            raise RuntimeError(f"BMS rejected cell-voltages: status=0x{status:02x}")
        return jbd.parse_cell_voltages(payload)


async def main():
    print(f"Looking for BMS {config.BMS_MAC}...")
    device = await BleakScanner.find_device_by_address(config.BMS_MAC, timeout=10.0)
    if device is None:
        sys.exit("BMS not found. Is another app (phone) currently connected to it?")
    print("Found. Connecting...")
    async with BleakClient(device) as client:
        reader = JbdReader(client)
        await reader.start()
        info = await reader.basic_info()
        cells = await reader.cell_voltages()

    print()
    print(f"Pack voltage:  {info.pack_voltage:6.2f} V")
    print(f"Pack current:  {info.pack_current:+6.2f} A   ({info.power_w:+.1f} W)")
    print(f"State of chg:  {info.soc_percent}%")
    print(f"Capacity:      {info.residual_capacity:.1f} / {info.nominal_capacity:.1f} Ah")
    print(f"Cycle count:   {info.cycle_count}")
    print(f"Temperatures:  {', '.join(f'{t:.1f} °C' for t in info.temperatures_c)}")
    print(f"FETs:          charge={'ON' if info.charge_fet_on else 'OFF'}, "
          f"discharge={'ON' if info.discharge_fet_on else 'OFF'}")
    if info.protections:
        print(f"PROTECTIONS:   {', '.join(info.protections)}")
    print(f"Production:    {info.production_date}")
    print(f"SW version:    0x{info.sw_version:02x}")
    print()
    print(f"Cell voltages ({info.cell_count}S):")
    for i, v in enumerate(cells, 1):
        flag = "  [balancing]" if info.balance_bitmap & (1 << (i - 1)) else ""
        print(f"  C{i}: {v:.3f} V{flag}")
    if cells:
        print(f"Spread:        {(max(cells) - min(cells)) * 1000:.0f} mV")


if __name__ == "__main__":
    asyncio.run(main())
