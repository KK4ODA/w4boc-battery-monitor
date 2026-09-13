"""Listen for one Victron Instant Readout advertisement and print every field.
Run: python tools/victron_read.py"""
import asyncio

import _shim  # noqa: F401
from w4boc.victron import main

asyncio.run(main())
