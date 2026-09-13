"""Connect to the BMS and enumerate all GATT services/characteristics."""
import asyncio
from bleak import BleakClient, BleakScanner

import _shim  # noqa: F401
from w4boc import config

BMS_MAC = config.BMS_MAC


async def main():
    device = await BleakScanner.find_device_by_address(BMS_MAC, timeout=10.0)
    if device is None:
        raise SystemExit("not found")
    async with BleakClient(device) as client:
        for svc in client.services:
            print(f"\nService {svc.uuid}  ({svc.description})")
            for c in svc.characteristics:
                props = ",".join(c.properties)
                print(f"  char {c.uuid}  [{props}]  handle={c.handle}")
                for d in c.descriptors:
                    print(f"    desc {d.uuid}  handle={d.handle}")


asyncio.run(main())
