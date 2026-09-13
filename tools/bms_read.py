"""One-shot BMS read (stop the monitor first: only one GATT client at a time).
Run: python tools/bms_read.py"""
import asyncio

import _shim  # noqa: F401
from w4boc.bms import main

asyncio.run(main())
