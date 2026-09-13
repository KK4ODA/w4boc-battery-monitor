"""W4BOC Battery Monitor — integrated monitor + dashboard + APRS telemetry."""
from pathlib import Path

# The application directory holds config.toml, secrets.toml, the SQLite DB,
# logs/ and updates/. It is the parent of this package.
APP_DIR = Path(__file__).resolve().parent.parent


def _read_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = _read_version()
