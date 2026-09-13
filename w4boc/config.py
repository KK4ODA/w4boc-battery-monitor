"""Config loader. Parses config.toml + secrets.toml in the application
directory and exposes typed module constants. Defaults come from
settings.SCHEMA, so an older config.toml keeps working after an update.

Simulation mode (env W4BOC_SIMULATE=1, set by `main.py --simulate`) relaxes the
secrets requirement so the app can be exercised on a machine without the
real hardware. W4BOC_CONFIG_FILE overrides the config path (tests use it).
"""
import os
import sys
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from . import APP_DIR
from . import settings as S

CONFIG_PATH = Path(os.environ.get("W4BOC_CONFIG_FILE") or (APP_DIR / "config.toml"))
SECRETS_PATH = Path(os.environ.get("W4BOC_SECRETS_FILE") or (APP_DIR / "secrets.toml"))
_EXAMPLE_CONFIG = APP_DIR / "config.example.toml"
_EXAMPLE_SECRETS = APP_DIR / "secrets.toml.example"

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


if CONFIG_PATH.exists():
    try:
        _config = _load_toml(CONFIG_PATH)
    except tomllib.TOMLDecodeError as e:
        _fail(f"\n  {CONFIG_PATH} is not valid TOML:\n    {e}\n\n"
              f"  Fix the line (or restore config.toml.bak) and restart.")
elif SIMULATE and _EXAMPLE_CONFIG.exists():
    _config = _load_toml(_EXAMPLE_CONFIG)
else:
    _fail(
        f"\n  config.toml not found:\n    {CONFIG_PATH}\n\n"
        f"  Copy config.example.toml to config.toml and edit it for this site\n"
        f"  (or start once, open the dashboard and use the Settings page)."
    )

if SECRETS_PATH.exists():
    try:
        _secrets = _load_toml(SECRETS_PATH)
    except tomllib.TOMLDecodeError as e:
        _fail(f"\n  {SECRETS_PATH} is not valid TOML:\n    {e}")
elif SIMULATE:
    _secrets = {"victron": {"encryption_key": "ff" * 16}, "email": {"app_password": ""}}
else:
    hint = (
        "  secrets.toml.example exists -- copy it to secrets.toml and fill in\n"
        "  your real values."
        if _EXAMPLE_SECRETS.exists()
        else "  Create secrets.toml with your [victron] encryption_key and\n"
             "  [email] app_password."
    )
    _fail(
        f"\n  secrets.toml not found:\n    {SECRETS_PATH}\n\n"
        f"{hint}\n\n"
        f"  See docs/OPERATION_MANUAL.md for what each secret is and\n"
        f"  where it comes from. Then restart the monitor."
    )

_values, _ = S.current_values(_config, _secrets)


def _get(section: str, key: str):
    """Typed value with the schema default as fallback (bad types fall back too)."""
    f = S.field(section, key)
    raw = _values.get(f.id, f.default)
    try:
        if f.type == "bool":
            return bool(raw) if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        if f.type == "int":
            return int(raw)
        if f.type == "float":
            return float(raw)
        if f.type == "list":
            return [str(x) for x in (raw or [])]
        return "" if raw is None else str(raw)
    except (TypeError, ValueError):
        return f.default


# Validate the values that came from secrets.toml. The Victron key is required
# (except in simulation); the Gmail app password may legitimately be empty.
_victron_key = _get("victron", "encryption_key")
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

# ---- site ----
SITE_NAME: str = _get("site", "name")
try:
    TZ = ZoneInfo(_get("site", "timezone"))
except Exception:
    TZ = ZoneInfo("UTC")
DB_PATH: Path = APP_DIR / _get("site", "db_path")
LOG_DIR: Path = APP_DIR / "logs"
UPDATES_DIR: Path = APP_DIR / "updates"
CHARGER_SAMPLE_PERIOD_S: int = _get("site", "charger_sample_period_s")
BMS_SAMPLE_PERIOD_S: int = _get("site", "bms_sample_period_s")
RETENTION_RAW_DAYS: int = _get("site", "retention_raw_days")
RETENTION_MINUTE_DAYS: int = _get("site", "retention_minute_days")
RETENTION_MAX_DAYS: int = _get("site", "retention_max_days")
INSTANCE_LOCK_PORT: int = _get("site", "instance_lock_port")
START_WITH_WINDOWS: bool = _get("site", "start_with_windows")

# ---- devices ----
BMS_MAC: str = _get("bms", "mac")
BMS_NAME: str = _get("bms", "name")
VICTRON_MAC: str = _get("victron", "mac")
VICTRON_KEY: str = _victron_key

# ---- email ----
EMAIL_SENDER: str = _get("email", "sender")
EMAIL_PASSWORD: str = _get("email", "app_password")
EMAIL_RECIPIENTS: list[str] = _get("email", "recipients")
EMAIL_SMTP_HOST: str = _get("email", "smtp_host")
EMAIL_SMTP_PORT: int = _get("email", "smtp_port")
EMAIL_ENABLED: bool = bool(EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENTS)

# ---- aprs ----
# Simulation never transmits unless explicitly asked (a developer PC may well
# have a soundmodem running): set W4BOC_SIM_APRS=1 to allow it.
APRS_ENABLED: bool = _get("aprs", "enabled") and (
    not SIMULATE or os.environ.get("W4BOC_SIM_APRS") == "1")
APRS_AGW_HOST: str = _get("aprs", "agw_host")
APRS_AGW_PORT: int = _get("aprs", "agw_port")
APRS_APRSIS_HOST: str = _get("aprs", "aprsis_host")
APRS_APRSIS_PORT: int = _get("aprs", "aprsis_port")
APRS_APRSIS_PASSCODE: int = _get("aprs", "aprsis_passcode")
APRS_CALLSIGN: str = _get("aprs", "callsign")
APRS_PATH: str = _get("aprs", "path")
APRS_LAT: float = _get("aprs", "lat")
APRS_LON: float = _get("aprs", "lon")
APRS_SYM_TABLE: str = _get("aprs", "symbol_table")
APRS_SYM_CODE: str = _get("aprs", "symbol_code")
APRS_TOCALL: str = _get("aprs", "tocall")
APRS_PROJECT_NAME: str = _get("aprs", "project_name") or f"{SITE_NAME} Battery"
APRS_COMMENT_PREFIX: str = _get("aprs", "comment_prefix") or f"{SITE_NAME} batt"
APRS_DATA_INTERVAL_S: int = _get("aprs", "data_interval_min") * 60
APRS_HEADERS_INTERVAL_S: int = _get("aprs", "headers_interval_min") * 60
APRS_POSITION_INTERVAL_S: int = _get("aprs", "position_interval_min") * 60
APRS_SEND_POSITION: bool = _get("aprs", "send_position")

# ---- mains ----
MAINS_FAST_MINUTES: int = _get("mains", "fast_minutes")
MAINS_BLE_ALIVE_MINUTES: int = _get("mains", "ble_alive_minutes")
MAINS_SLOW_MINUTES: int = _get("mains", "slow_minutes")
MAINS_RESTORE_MINUTES: int = _get("mains", "restore_minutes")
MAINS_DISCHARGE_CURRENT_A: float = _get("mains", "discharge_current_a")
MAINS_USE_WINDOWS_POWER: bool = _get("mains", "use_windows_power_status")
MAINS_APRS_STATUS: bool = _get("mains", "aprs_status_packet")
MAINS_APRS_KICK: bool = _get("mains", "aprs_kick_telemetry")
MAINS_APRS_COMMENT: bool = _get("mains", "aprs_comment_flag")

# ---- alerts ----
SOC_URGENT: int = _get("alerts", "soc_urgent_pct")
SOC_DEGRADED: int = _get("alerts", "soc_degraded_pct")
SOC_RECOVERY: int = _get("alerts", "soc_recovery_pct")
SOC_DROP_PER_HOUR: int = _get("alerts", "soc_drop_per_hour_pct")
DIGEST_HOUR: int = _get("alerts", "digest_hour")
DIGEST_MINUTE: int = _get("alerts", "digest_minute")
URGENT_RATE_LIMIT_MINUTES: int = _get("alerts", "urgent_rate_limit_minutes")
MODE_RECOVERY_MINUTES: int = _get("alerts", "mode_recovery_minutes")
CELL_SPREAD_MV: int = _get("alerts", "cell_spread_mv")
TEMP_HIGH_C: float = _get("alerts", "temp_high_c")
TEMP_LOW_C: float = _get("alerts", "temp_low_c")
WATCHDOG_EMAIL_THRESHOLD: int = _get("alerts", "watchdog_email_threshold")
BATTERY_FINAL_SOC: int = _get("alerts", "battery_final_soc_pct")
BATTERY_FINAL_VOLTAGE: float = _get("alerts", "battery_final_voltage_v")
BATTERY_FINAL_APRS: bool = _get("alerts", "battery_final_aprs_status")

# ---- dashboard ----
DASH_HOST: str = _get("dashboard", "host")
DASH_PORT: int = int(os.environ.get("W4BOC_PORT") or _get("dashboard", "port"))
DASH_OPEN_BROWSER: bool = _get("dashboard", "open_browser")
DASH_BROWSER_CHECK_DELAY_S: int = _get("dashboard", "browser_check_delay_s")

# ---- updater ----
UPDATER_ENABLED: bool = _get("updater", "enabled")
UPDATER_REPO: str = _get("updater", "repo")
UPDATER_CHECK_INTERVAL_S: int = int(_get("updater", "check_interval_hours") * 3600)
UPDATER_STARTUP_DELAY_S: int = int(_get("updater", "startup_check_delay_min") * 60)
UPDATER_AUTO_INSTALL: bool = _get("updater", "auto_install")
UPDATER_TOKEN: str = _get("updater", "github_token")
