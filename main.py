"""Entry point: python main.py [--simulate] [--no-browser] [--port N]

Normally started by launcher.py (via run.bat), which supervises this process.
Running it directly is fine for development.
"""
import argparse
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def main() -> int:
    p = argparse.ArgumentParser(description="W4BOC Battery Monitor")
    p.add_argument("--simulate", action="store_true",
                   help="fake BMS/charger data; no Bluetooth or secrets needed")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open the dashboard in a browser at startup")
    p.add_argument("--port", type=int, help="dashboard port (overrides config.toml)")
    args = p.parse_args()

    if args.simulate:
        os.environ["W4BOC_SIMULATE"] = "1"
    if args.port:
        os.environ["W4BOC_PORT"] = str(args.port)
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from w4boc.app import run
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
