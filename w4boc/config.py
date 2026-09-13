"""Config loader. Parses config.toml + secrets.toml in the application
directory. secrets.toml is shallow-merged into config so callers don't need to
know which file holds which value.

Every key has a default so an older config.toml keeps working after an
auto-update introduces new settings.

Simulation mode (env W4BOC_SIMULATE=1, set by `main.py --simulate`) relaxes the
secrets requirement so the app can be exercised on a machine without the
real hardware.
"""
import os
import sys
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from . import APP_DIR

# W4BOC_CONFIG_FILE overrides the config path (tests point it at the example).
_PATH = Path(os.environ.get("W4BOC_CONFIG_FILE") or (APP_DIR / "config.toml"))
_EXAMPLE_CONFIG = APP_DIR / "config.example.toml"
_SECRETS_PATH = APP_DIR / "secrets.toml"
_EXAMPLE_PATH = APP_DIR / "secrets.toml.example"

# Placeholder value from secrets.toml.example — if it survives into the live
# secrets.toml, the user copied the template and forgot to fill it in.
_PLACEHOLDER_VICTRON_KEY = "00000000000000000000000000000000"

SIMULATE: bool = os.environ.get("W4BOC_SIMULATE", "") not in ("", "0")


def _fail(msg: str):
    """Print a clear multi-line message to stderr and exit. Visible in the
    console window and captured by the launcher log."""
    sys.stderr.write("\n" + "=" * 60 + "\n")
    sys.stderr.write("  W4BOC Battery Monitor -- configuration error\n")
    sys.stderr.write("=" * 60 + "\n")
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.write("=" * 60 + "\n\n")
    sys.exit(4)   # not 2: the launcher would treat 2 as a quick-restart request


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


if _PATH.exists():
    _data = _load_toml(_PATH)
elif SIMULATE and _EXAMPLE_CONFIG.exists():
    _data = _load_toml(_EXAMPLE_CONFIG)
else:
    _fail(
        f"\n  config.toml not found:\n    {_PATH}\n\n"
        f"  Copy config.example.toml to config.toml and edit it for this site."
    )

if _SECRETS_PATH.exists():
    _secrets = _load_toml(_SECRETS_PATH)
elif SIMULATE:
    _secrets = {"victron": {"encryption_key": "ff" * 16}, "email": {"app_password": ""}}
else:
    hint = (
        "  secrets.toml.example exists -- copy it to secrets.toml and fill in\n"
        "  your real values."
        if _EXAMPLE_PATH.exists()
        else "  Create secrets.toml with your [victron] encryption_key and\n"
             "  [email] app_password."
    )
    _fail(
        f"\n  secrets.toml not found:\n    {_SECRETS_PATH}\n\n"
        f"{hint}\n\n"
        f"  See docs/OPERATION_MANUAL.md for what each secret is and\n"
        f"  where it comes from. Then restart the monitor."
    )

# Per-section shallow merge — secrets values win on key collision.
for _section, _items in _secrets.items():
    _data.setdefault(_section, {}).update(_items)

# Validate the values that came from secrets.toml. The Victron key is required
# (except in simulation); the Gmail app password may legitimately be empty.
_victron_key = _data.get("victron", {}).get("encryption_key", "")
if not SIMULATE:
    if not _victron_key:
        _fail(
            "\n  [victron] encryption_key is missing from secrets.toml.\n\n"
            "  Get the value from VictronConnect -> your charger -> Product Info\n"
            "  -> 'Encryption data'. It's a 32-character hex string."
        )
    if _victron_key == _PLACEHOLDER_VICTRON_KEY:
        _fail(
            "\n  [victron] encryption_key in secrets.toml is still the placeholder\n"
            f"  value ({_PLACEHOLDER_VICTRON_KEY}).\n\n"
            "  Replace it with the real key from VictronConnect -> your charger\n"
            "  -> Product Info -> 'Encryption data'."
        )


def _sec(name: str) -> dict:
    return _data.get(name, {}) or {}


_bms = _sec("bms")
BMS_MAC: str = _bms.get("mac", "")
BMS_NAME: str = _bms.get("name", "")

_victron = _sec("victron")
VICTRON_MAC: str = _victron.get("mac", "")
VICTRON_KEY: str = _victron_key

_site = _sec("site")
SITE_NAME: str = _site.get("name", "W4BOC")
TZ = ZoneInfo(_site.get("timezone", "America/New_York"))
DB_PATH: Path = APP_DIR / _site.get("db_path", "monitor.db")
LOG_DIR: Path = APP_DIR / "logs"
UPDATES_DIR: Path = APP_DIR / "updates"
CHARGER_SAMPLE_PERIOD_S: int = int(_site.get("charger_sample_period_s", 60))
BMS_SAMPLE_PERIOD_S: int = int(_site.get("bms_sample_period_s", 60))

_email = _sec("email")
EMAIL_SENDER: str = _email.get("sender", "")
EMAIL_PASSWORD: str = _email.get("app_password", "")
EMAIL_RECIPIENTS: list[str] = list(_email.get("recipients", []))
EMAIL_ENABLED: bool = bool(EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENTS)

_aprs = _sec("aprs")
# Simulation never transmits unless explicitly asked (a developer PC may well
# have a soundmodem running): set W4BOC_SIM_APRS=1 to allow it.
APRS_ENABLED: bool = bool(_aprs.get("enabled", False)) and (
    not SIMULATE or os.environ.get("W4BOC_SIM_APRS") == "1")
APRS_AGW_HOST: str = _aprs.get("agw_host", "localhost")
APRS_AGW_PORT: int = int(_aprs.get("agw_port", 8000))
APRS_APRSIS_HOST: str = _aprs.get("aprsis_host", "rotate.aprs2.net")
APRS_APRSIS_PORT: int = int(_aprs.get("aprsis_port", 14580))
APRS_APRSIS_PASSCODE: int = int(_aprs.get("aprsis_passcode", 0))
APRS_CALLSIGN: str = _aprs.get("callsign", "N0CALL-1")
APRS_PATH: str = _aprs.get("path", "WIDE1-1,WIDE2-1")
APRS_LAT: float = float(_aprs.get("lat", 0.0))
APRS_LON: float = float(_aprs.get("lon", 0.0))
APRS_SYM_TABLE: str = _aprs.get("symbol_table", "/")
APRS_SYM_CODE: str = _aprs.get("symbol_code", "-")
APRS_DATA_INTERVAL_S: int = int(_aprs.get("data_interval_min", 10)) * 60
APRS_HEADERS_INTERVAL_S: int = int(_aprs.get("headers_interval_min", 60)) * 60
APRS_POSITION_INTERVAL_S: int = int(_aprs.get("position_interval_min", 15)) * 60
APRS_SEND_POSITION: bool = bool(_aprs.get("send_position", True))

_mains = _sec("mains")
MAINS_FAST_MINUTES: int = int(_mains.get("fast_minutes", 5))
MAINS_BLE_ALIVE_MINUTES: int = int(_mains.get("ble_alive_minutes", 3))
MAINS_SLOW_MINUTES: int = int(_mains.get("slow_minutes", 45))
MAINS_RESTORE_MINUTES: int = int(_mains.get("restore_minutes", 2))
MAINS_DISCHARGE_CURRENT_A: float = float(_mains.get("discharge_current_a", -1.0))
MAINS_USE_WINDOWS_POWER: bool = bool(_mains.get("use_windows_power_status", True))
MAINS_APRS_STATUS: bool = bool(_mains.get("aprs_status_packet", True))
MAINS_APRS_KICK: bool = bool(_mains.get("aprs_kick_telemetry", True))
MAINS_APRS_COMMENT: bool = bool(_mains.get("aprs_comment_flag", True))

_a = _sec("alerts")
SOC_URGENT: int = int(_a.get("soc_urgent_pct", 30))
SOC_DEGRADED: int = int(_a.get("soc_degraded_pct", 50))
SOC_RECOVERY: int = int(_a.get("soc_recovery_pct", 80))
SOC_DROP_PER_HOUR: int = int(_a.get("soc_drop_per_hour_pct", 20))
# Legacy key; if present in an old config.toml it overrides the slow rule.
MAINS_SLOW_MINUTES = int(_a.get("mains_lost_minutes", MAINS_SLOW_MINUTES))
DIGEST_HOUR: int = int(_a.get("digest_hour", 8))
DIGEST_MINUTE: int = int(_a.get("digest_minute", 0))
URGENT_RATE_LIMIT_MINUTES: int = int(_a.get("urgent_rate_limit_minutes", 60))
MODE_RECOVERY_MINUTES: int = int(_a.get("mode_recovery_minutes", 60))
CELL_SPREAD_MV: int = int(_a.get("cell_spread_mv", 30))
TEMP_HIGH_C: float = float(_a.get("temp_high_c", 35))
TEMP_LOW_C: float = float(_a.get("temp_low_c", 0))
WATCHDOG_EMAIL_THRESHOLD: int = int(_a.get("watchdog_email_threshold", 12))

_dash = _sec("dashboard")
DASH_HOST: str = _dash.get("host", "127.0.0.1")
DASH_PORT: int = int(os.environ.get("W4BOC_PORT") or _dash.get("port", 8080))
DASH_OPEN_BROWSER: bool = bool(_dash.get("open_browser", True))
DASH_BROWSER_CHECK_DELAY_S: int = int(_dash.get("browser_check_delay_s", 15))

_upd = _sec("updater")
UPDATER_ENABLED: bool = bool(_upd.get("enabled", True))
UPDATER_REPO: str = _upd.get("repo", "KK4ODA/w4boc-battery-monitor")
UPDATER_CHECK_INTERVAL_S: int = int(float(_upd.get("check_interval_hours", 6)) * 3600)
UPDATER_STARTUP_DELAY_S: int = int(float(_upd.get("startup_check_delay_min", 3)) * 60)
UPDATER_AUTO_INSTALL: bool = bool(_upd.get("auto_install", True))
UPDATER_TOKEN: str = _upd.get("github_token", "") or ""
