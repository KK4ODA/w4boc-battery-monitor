"""BLE discovery scan. Prints every advertising device nearby with enough
metadata to identify a BMS vendor (name, address, RSSI, service UUIDs,
manufacturer data). Run: python scan.py [seconds]"""
import asyncio, sys
from bleak import BleakScanner

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 15

async def main():
    print(f"Scanning for {DURATION}s...\n")
    seen = {}

    def cb(device, adv):
        prev = seen.get(device.address, {})
        seen[device.address] = {
            "name": device.name or prev.get("name") or adv.local_name or "(no name)",
            "rssi": adv.rssi,
            "services": sorted(set((prev.get("services") or []) + list(adv.service_uuids))),
            "mfg": {**(prev.get("mfg") or {}), **{k: v.hex() for k, v in adv.manufacturer_data.items()}},
            "sdata": {**(prev.get("sdata") or {}), **{k: v.hex() for k, v in adv.service_data.items()}},
        }

    async with BleakScanner(cb):
        await asyncio.sleep(DURATION)

    rows = sorted(seen.items(), key=lambda kv: kv[1]["rssi"], reverse=True)
    for addr, d in rows:
        print(f"{d['rssi']:>4} dBm  {addr}  {d['name']}")
        for s in d["services"]:
            print(f"              service: {s}")
        for cid, hx in d["mfg"].items():
            print(f"              mfg[0x{cid:04x}]: {hx}")
        for uuid, hx in d["sdata"].items():
            print(f"              sdata[{uuid}]: {hx}")
        print()

asyncio.run(main())
