"""Read the Victron Blue Smart IP22 charger via encrypted BLE advertisements.

Uses the `victron-ble` library, which decrypts Victron's Instant Readout
manufacturer-data records. No GATT pairing — purely passive listener — so this
coexists with VictronConnect on a phone.
"""
import asyncio
import sys
from bleak import BleakScanner
from victron_ble.devices import detect_device_type

from . import config

sys.stdout.reconfigure(encoding="utf-8")

VICTRON_MFG_ID = 0x02E1

# The Instant Readout advertisement has an encryption-prefix byte that tells
# victron-ble which device type to instantiate.
#   mfg_data[0:2]   prefix ("\x10\x00" = Product v1 frame)
#   mfg_data[2:4]   model ID, little-endian
#   mfg_data[4]     record type
#   mfg_data[5:7]   nonce / init counter (little-endian)
#   mfg_data[7]     first byte of the AES key, used as a fast check
#   mfg_data[8:]    ciphertext payload


async def main():
    mac = config.VICTRON_MAC.upper()
    key_hex = config.VICTRON_KEY
    got = asyncio.Event()

    def on_adv(device, adv):
        if device.address.upper() != mac:
            return
        mfg = adv.manufacturer_data.get(VICTRON_MFG_ID)
        if not mfg:
            return
        try:
            cls = detect_device_type(mfg)
            if cls is None:
                print(f"Unrecognized Victron device type; mfg_data={mfg.hex()}")
                return
            parsed = cls(key_hex).parse(mfg)
        except Exception as e:
            print(f"Parse failed: {e}")
            return

        print(f"\nReceived from {device.address}  (RSSI {adv.rssi} dBm)")
        print(f"Model: {parsed.get_model_name()}")
        for name in dir(parsed):
            if name.startswith("get_") and name != "get_model_name":
                try:
                    val = getattr(parsed, name)()
                except Exception:
                    continue
                if val is None:
                    continue
                print(f"  {name[4:]:<22} {val}")
        got.set()

    print(f"Listening for Victron advertisements from {mac}... (Ctrl-C to stop)")
    async with BleakScanner(on_adv):
        try:
            await asyncio.wait_for(got.wait(), timeout=30)
        except asyncio.TimeoutError:
            sys.exit("No advertisement seen in 30 s — is the charger powered and Instant Readout enabled?")


if __name__ == "__main__":
    asyncio.run(main())
