"""Settings schema — the single source of truth for every configurable value.

`config.py` takes its defaults from here, the dashboard's Settings page is
generated from here, `config.example.toml` / `secrets.toml.example` are
rendered from here (a test keeps them in sync), and the Settings page writes
`config.toml` / `secrets.toml` through `save()`.

Values marked `secret=True` live in secrets.toml; everything else in
config.toml. Unknown keys found in an existing file are preserved on save.
"""
from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------- schema ----------

MAC_RE = r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
CALLSIGN_RE = r"^[A-Za-z0-9]{1,6}(-([0-9]|1[0-5]))?$"
REPO_RE = r"^[\w.-]+/[\w.-]+$"
HEX32_RE = r"^[0-9a-fA-F]{32}$"
HOST_RE = r"^[A-Za-z0-9.-]+$"


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    type: str                  # str | int | float | bool | list | secret
    default: Any
    label: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    validate: str | None = None   # "timezone" | "email" | "email_list" | "char"
    secret: bool = False
    placeholder: str = ""
    restart: bool = True          # change needs a monitor restart

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"


SECTIONS: dict[str, tuple[str, str]] = {
    # section -> (title, description)
    "site": ("Site", "Identity, sampling and data retention."),
    "bms": ("Battery BMS", "JBD/Xiaoxiang BMS inside the LiFePO4 pack (find the MAC with tools/scan.py)."),
    "victron": ("Charger", "Victron Blue Smart IP22. The encryption key comes from VictronConnect → Product Info → Encryption data."),
    "email": ("Email alerts", "Gmail SMTP with an app password. Leave the password blank to disable email."),
    "aprs": ("APRS telemetry", "RF via soundmodem's AGWPE server and optional APRS-IS uplink. Header/telemetry formats are fixed; only cadence and identity are settings."),
    "mains": ("Mains power detection", "How an AC outage is recognised (see the Power page for the full explanation)."),
    "alerts": ("Alert thresholds", "When to email and when to enter DEGRADED mode."),
    "dashboard": ("Dashboard & startup", "Web dashboard binding and what happens when the monitor starts."),
    "updater": ("Updates", "Automatic updates from GitHub Releases."),
}
SECTION_ORDER = list(SECTIONS)

SCHEMA: list[Field] = [
    # site
    Field("site", "name", "str", "W4BOC", "Site name", "Used in email subjects, APRS comments and the dashboard title.", pattern=r"^[A-Za-z0-9 _.-]{1,32}$"),
    Field("site", "timezone", "str", "America/New_York", "Time zone", "IANA name, e.g. America/New_York.", validate="timezone"),
    Field("site", "db_path", "str", "monitor.db", "Database file", "SQLite file, relative to the application folder.", pattern=r"^[^\\/:*?\"<>|]+$"),
    Field("site", "bms_sample_period_s", "int", 60, "BMS poll interval (s)", "One Bluetooth connection per poll; be gentle on the BMS.", min=10, max=3600),
    Field("site", "charger_sample_period_s", "int", 60, "Charger sample interval (s)", "The charger broadcasts ~1 Hz; a sample is stored at least this often, sooner on significant change.", min=10, max=3600),
    Field("site", "retention_raw_days", "int", 7, "Keep raw samples (days)", "Full resolution for this many days.", min=1, max=365),
    Field("site", "retention_minute_days", "int", 30, "Keep per-minute samples (days)", "Then one sample per minute until this age.", min=1, max=3650),
    Field("site", "retention_max_days", "int", 365, "Keep hourly samples (days)", "Then one per hour; older samples are deleted.", min=1, max=36500),
    Field("site", "instance_lock_port", "int", 50001, "Instance lock port", "Local TCP port used to make sure only one monitor runs.", min=1024, max=65535),
    Field("site", "start_with_windows", "bool", False, "Start with Windows", "Keep a Startup-folder shortcut to run.bat (created at startup if missing). The switch on the Settings page applies immediately.", restart=False),
    # bms
    Field("bms", "mac", "str", "AA:BB:CC:DD:EE:FF", "BMS Bluetooth MAC", "", pattern=MAC_RE),
    Field("bms", "name", "str", "DP04S007L4S200A", "BMS name", "Informational.", pattern=r"^.{0,64}$"),
    # victron
    Field("victron", "mac", "str", "AA:BB:CC:DD:EE:00", "Charger Bluetooth MAC", "", pattern=MAC_RE),
    Field("victron", "encryption_key", "secret", "", "Encryption key", "32 hex characters. Regenerates if the charger's Bluetooth PIN is reset.", pattern=HEX32_RE, secret=True, placeholder="32 hex characters"),
    # email
    Field("email", "sender", "str", "you@gmail.com", "Sender (Gmail address)", "", validate="email"),
    Field("email", "recipients", "list", ["operator1@example.com", "operator2@example.com"], "Recipients", "One address per line.", validate="email_list"),
    Field("email", "smtp_host", "str", "smtp.gmail.com", "SMTP server", "", pattern=HOST_RE),
    Field("email", "smtp_port", "int", 587, "SMTP port", "587 = STARTTLS.", min=1, max=65535),
    Field("email", "app_password", "secret", "", "Gmail app password", "16 characters from myaccount.google.com/apppasswords (2FA required). Blank disables email.", secret=True, placeholder="blank = email disabled"),
    # aprs
    Field("aprs", "enabled", "bool", True, "APRS enabled", ""),
    Field("aprs", "callsign", "str", "W4BOC-1", "Callsign-SSID", "", pattern=CALLSIGN_RE),
    Field("aprs", "agw_host", "str", "localhost", "AGWPE host", "soundmodem's AGWPE TCP server (we bypass YAAC, which refused to RF-route our packets).", pattern=HOST_RE),
    Field("aprs", "agw_port", "int", 8000, "AGWPE port", "", min=1, max=65535),
    Field("aprs", "aprsis_host", "str", "rotate.aprs2.net", "APRS-IS server", "", pattern=HOST_RE),
    Field("aprs", "aprsis_port", "int", 14580, "APRS-IS port", "", min=1, max=65535),
    Field("aprs", "aprsis_passcode", "int", 0, "APRS-IS passcode", "0 disables the Internet uplink (fully offline operation). Standard passcode generators apply.", min=0, max=99999),
    Field("aprs", "path", "str", "WIDE1-1,WIDE2-1", "Digipeat path", "Comma-separated, appended to RF frames only.", pattern=r"^[A-Za-z0-9,-]*$"),
    Field("aprs", "lat", "float", 33.805520, "Latitude", "Decimal degrees, north positive.", min=-90, max=90),
    Field("aprs", "lon", "float", -84.146500, "Longitude", "Decimal degrees, east positive.", min=-180, max=180),
    Field("aprs", "symbol_table", "str", "I", "Symbol table", "One character ('/', '\\' or an overlay letter).", validate="char"),
    Field("aprs", "symbol_code", "str", "#", "Symbol code", "One character ('#' = digipeater).", validate="char"),
    Field("aprs", "tocall", "str", "APZBAT", "Destination (tocall)", "APZ = experimental. Changing this changes every packet — leave it.", pattern=r"^[A-Z0-9]{1,6}$"),
    Field("aprs", "project_name", "str", "", "Telemetry project name", "Sent in the BITS header. Blank = '<site name> Battery'.", pattern=r"^[ -~]{0,23}$"),
    Field("aprs", "comment_prefix", "str", "", "Position comment prefix", "Blank = '<site name> batt'. The comment becomes e.g. 'W4BOC batt 13.78V 100% 23C'.", pattern=r"^[ -~]{0,20}$"),
    Field("aprs", "data_interval_min", "int", 10, "Telemetry data every (min)", "T#NNN frames.", min=1, max=1440),
    Field("aprs", "headers_interval_min", "int", 60, "Telemetry headers every (min)", "PARM/UNIT/EQNS/BITS set.", min=5, max=1440),
    Field("aprs", "position_interval_min", "int", 15, "Position every (min)", "", min=1, max=1440),
    Field("aprs", "send_position", "bool", True, "Send position packets", "Off when sharing the callsign with a YAAC beacon that already sends it."),
    # mains
    Field("mains", "fast_minutes", "int", 5, "Charger silent (min) → suspect outage", "While the BMS still answers. One Bluetooth restart follows; still silent this long after it → MAINS LOST.", min=2, max=120),
    Field("mains", "ble_alive_minutes", "int", 3, "BMS 'still answering' window (min)", "", min=1, max=60),
    Field("mains", "slow_minutes", "int", 45, "Charger silent (min) → outage even if BMS silent", "Low-confidence fallback (may be a Bluetooth failure).", min=5, max=1440),
    Field("mains", "restore_minutes", "int", 2, "Charger heard within (min) → mains ON", "", min=1, max=60),
    Field("mains", "discharge_current_a", "float", -1.0, "Report 'discharging' below (A)", "Informational text in alerts only.", min=-100, max=0),
    Field("mains", "use_windows_power_status", "bool", True, "Use Windows power status", "Instant detection if the PC is on a USB-connected UPS."),
    Field("mains", "aprs_status_packet", "bool", True, "APRS status packet on change", "'>AC MAINS LOST …' / '>AC MAINS RESTORED …'."),
    Field("mains", "aprs_kick_telemetry", "bool", True, "Immediate APRS position + telemetry on change", ""),
    Field("mains", "aprs_comment_flag", "bool", True, "Append ' MAINS LOST' to the APRS comment", "While the outage lasts."),
    # alerts
    Field("alerts", "soc_urgent_pct", "int", 30, "SoC urgent below (%)", "", min=0, max=100),
    Field("alerts", "soc_degraded_pct", "int", 50, "SoC DEGRADED below (%)", "Enters DEGRADED mode (daily digest).", min=0, max=100),
    Field("alerts", "soc_recovery_pct", "int", 80, "SoC recovered above (%)", "Needed (with mains OK) to leave DEGRADED.", min=0, max=100),
    Field("alerts", "soc_drop_per_hour_pct", "int", 20, "SoC drop per hour → urgent (%)", "", min=1, max=100),
    Field("alerts", "cell_spread_mv", "int", 30, "Cell spread event above (mV)", "Logged, no email.", min=1, max=1000),
    Field("alerts", "temp_high_c", "float", 35, "Battery temperature high (°C)", "", min=-40, max=100),
    Field("alerts", "temp_low_c", "float", 0, "Battery temperature low (°C)", "", min=-40, max=100),
    Field("alerts", "battery_final_soc_pct", "int", 10, "Final warning at SoC ≤ (%)", "Last email before the BMS cuts the load (and this PC) off. Clears once SoC is 5 points higher.", min=0, max=50),
    Field("alerts", "battery_final_voltage_v", "float", 12.0, "Final warning at pack voltage ≤ (V)", "While discharging. Clears 0.3 V higher.", min=9, max=14),
    Field("alerts", "battery_final_aprs_status", "bool", True, "APRS status packet with the final warning", "'>BATTERY LOW 12.31V 9% ~4h10m'."),
    Field("alerts", "watchdog_email_threshold", "int", 12, "BLE restarts per 24 h before emailing", "", min=1, max=1000),
    Field("alerts", "digest_hour", "int", 8, "Daily digest hour (local)", "Only while DEGRADED.", min=0, max=23),
    Field("alerts", "digest_minute", "int", 0, "Daily digest minute", "", min=0, max=59),
    Field("alerts", "urgent_rate_limit_minutes", "int", 60, "One urgent email per trigger per (min)", "", min=1, max=1440),
    Field("alerts", "mode_recovery_minutes", "int", 60, "Condition clear for (min) before NORMAL", "Hysteresis.", min=1, max=1440),
    # dashboard
    Field("dashboard", "host", "str", "127.0.0.1", "Bind address", "127.0.0.1 = this PC only (expose with Tailscale serve).", pattern=r"^[A-Za-z0-9.:-]+$"),
    Field("dashboard", "port", "int", 8080, "Port", "", min=1, max=65535),
    Field("dashboard", "open_browser", "bool", True, "Open the dashboard in a browser at startup", "Only if no browser tab already has it open.", restart=False),
    Field("dashboard", "browser_check_delay_s", "int", 15, "Wait for an existing tab (s)", "", min=3, max=120, restart=False),
    # updater
    Field("updater", "enabled", "bool", True, "Automatic update checks", ""),
    Field("updater", "repo", "str", "KK4ODA/w4boc-battery-monitor", "GitHub repository", "owner/name publishing the releases.", pattern=REPO_RE),
    Field("updater", "check_interval_hours", "float", 6, "Check every (hours)", "", min=0.1, max=168),
    Field("updater", "startup_check_delay_min", "float", 3, "First check after start (min)", "", min=0.1, max=1440),
    Field("updater", "auto_install", "bool", True, "Install automatically", "Otherwise wait for the Install button."),
    Field("updater", "github_token", "secret", "", "GitHub token", "Only for a private repository (fine-grained token, Contents: read).", secret=True, placeholder="blank for a public repo"),
]

_BY_ID = {f.id: f for f in SCHEMA}

# Keys that older config files may contain; mapped onto the current schema.
LEGACY = {("alerts", "mains_lost_minutes"): ("mains", "slow_minutes")}


def field(section: str, key: str) -> Field:
    return _BY_ID[f"{section}.{key}"]


def default(section: str, key: str) -> Any:
    f = _BY_ID.get(f"{section}.{key}")
    return f.default if f else None


def fields_in(section: str) -> list[Field]:
    return [f for f in SCHEMA if f.section == section]


# ---------- load ----------

def load_toml(path: Path) -> dict:
    try:
        with Path(path).open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def current_values(config_data: dict, secrets_data: dict) -> tuple[dict[str, Any], dict[str, dict]]:
    """Return ({field_id: value}, {section: {key: value}} of unknown keys)."""
    values: dict[str, Any] = {}
    for f in SCHEMA:
        src = secrets_data if f.secret else config_data
        sec = src.get(f.section) or {}
        values[f.id] = sec.get(f.key, f.default)
    for (lsec, lkey), (nsec, nkey) in LEGACY.items():
        v = (config_data.get(lsec) or {}).get(lkey)
        if v is not None and nkey not in (config_data.get(nsec) or {}):
            values[f"{nsec}.{nkey}"] = v
    extra: dict[str, dict] = {}
    for sec, items in config_data.items():
        if not isinstance(items, dict):
            continue
        for k, v in items.items():
            if f"{sec}.{k}" not in _BY_ID and (sec, k) not in LEGACY:
                extra.setdefault(sec, {})[k] = v
    return values, extra


# ---------- validate ----------

def _coerce(f: Field, raw: Any) -> Any:
    if f.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if f.type == "int":
        if isinstance(raw, bool):
            raise ValueError("not a number")
        return int(str(raw).strip())
    if f.type == "float":
        if isinstance(raw, bool):
            raise ValueError("not a number")
        return float(str(raw).strip())
    if f.type == "list":
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw]
        else:
            items = re.split(r"[,\n;]+", str(raw))
        return [x.strip() for x in items if x.strip()]
    return str(raw).strip()


def _check(f: Field, v: Any) -> str | None:
    if f.type in ("int", "float"):
        if f.min is not None and v < f.min:
            return f"must be ≥ {f.min:g}"
        if f.max is not None and v > f.max:
            return f"must be ≤ {f.max:g}"
        if f.type == "float" and v != v:
            return "not a number"
    if f.type in ("str", "secret"):
        if f.pattern and v and not re.match(f.pattern, v):
            return "invalid format"
        if f.type == "str" and f.pattern and not v and not re.match(f.pattern, ""):
            return "required"
    if f.validate == "timezone":
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return "unknown time zone"
    if f.validate == "email" and not re.match(EMAIL_RE, v):
        return "not an email address"
    if f.validate == "email_list":
        bad = [x for x in v if not re.match(EMAIL_RE, x)]
        if bad:
            return f"not an email address: {', '.join(bad)}"
    if f.validate == "char" and len(v) != 1:
        return "exactly one character"
    return None


def validate(form: dict[str, Any], *, secrets_keep: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Coerce + validate a {field_id: raw} mapping (missing bools = False,
    missing others = default). Secrets left blank keep their stored value
    (`secrets_keep`) unless `<id>__clear` is set. Returns (values, errors)."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    secrets_keep = secrets_keep or {}
    for f in SCHEMA:
        raw = form.get(f.id)
        if f.type == "bool":
            raw = False if raw is None else raw
        elif raw is None:
            raw = f.default
        if f.secret:
            if form.get(f"{f.id}__clear"):
                values[f.id] = ""
                continue
            if raw is None or str(raw).strip() == "":
                values[f.id] = secrets_keep.get(f.id, "")
                continue
        try:
            v = _coerce(f, raw)
        except ValueError:
            errors[f.id] = "not a number"
            values[f.id] = raw
            continue
        err = _check(f, v)
        if err:
            errors[f.id] = err
        values[f.id] = v
    if not errors:
        if values["alerts.soc_urgent_pct"] > values["alerts.soc_degraded_pct"]:
            errors["alerts.soc_urgent_pct"] = "must be ≤ the DEGRADED threshold"
        if values["alerts.soc_recovery_pct"] < values["alerts.soc_degraded_pct"]:
            errors["alerts.soc_recovery_pct"] = "must be ≥ the DEGRADED threshold"
        if values["alerts.temp_low_c"] >= values["alerts.temp_high_c"]:
            errors["alerts.temp_low_c"] = "must be below the high threshold"
        if values["site.retention_raw_days"] > values["site.retention_minute_days"] or \
                values["site.retention_minute_days"] > values["site.retention_max_days"]:
            errors["site.retention_raw_days"] = "raw ≤ per-minute ≤ hourly retention"
        if values["mains.restore_minutes"] >= values["mains.fast_minutes"]:
            errors["mains.restore_minutes"] = "must be below the 'charger silent' window"
        if values["site.instance_lock_port"] == values["dashboard.port"]:
            errors["site.instance_lock_port"] = "must differ from the dashboard port"
    return values, errors


# ---------- render ----------

def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("non-finite float")
        return repr(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[\n" + "".join(f"  {json.dumps(str(x))},\n" for x in v) + "]"
    return json.dumps(str(v))


def _comment(text: str) -> str:
    return "".join(f"# {line}\n" for line in text.splitlines()) if text else ""


def render_config_toml(values: dict[str, Any], extra: dict[str, dict] | None = None) -> str:
    out = [
        "# W4BOC Battery Monitor — config.toml\n",
        "# Edit here or on the dashboard's Settings page (which rewrites this file,\n",
        "# keeping any keys it does not know). Secrets live in secrets.toml.\n",
        "# Changes take effect after a monitor restart.\n",
    ]
    extra = extra or {}
    sections = SECTION_ORDER + [s for s in extra if s not in SECTION_ORDER]
    for sec in sections:
        title, desc = SECTIONS.get(sec, (sec, ""))
        out.append(f"\n[{sec}]\n")
        if desc:
            out.append(_comment(desc))
        for f in fields_in(sec):
            if f.secret:
                out.append(f"# {f.key} lives in secrets.toml\n")
                continue
            v = values.get(f.id, f.default)
            help_line = f"{f.label}." + (f" {f.help}" if f.help else "")
            out.append(_comment(help_line))
            out.append(f"{f.key} = {_toml_value(v)}\n")
        for k, v in (extra.get(sec) or {}).items():
            out.append("# (not a known setting — kept as-is)\n")
            out.append(f"{k} = {_toml_value(v)}\n")
    return "".join(out)


def render_secrets_toml(values: dict[str, Any], placeholders: bool = False) -> str:
    out = [
        "# W4BOC Battery Monitor — secrets.toml\n",
        "# Never share this file; it is git-ignored and excluded from release zips.\n",
    ]
    for sec in SECTION_ORDER:
        secs = [f for f in fields_in(sec) if f.secret]
        if not secs:
            continue
        out.append(f"\n[{sec}]\n")
        for f in secs:
            out.append(_comment(f"{f.label}. {f.help}".strip()))
            v = f.placeholder if placeholders else values.get(f.id, "")
            if placeholders and f.key == "encryption_key":
                v = "0" * 32
            elif placeholders:
                v = ""
            out.append(f"{f.key} = {_toml_value(v)}\n")
    return "".join(out)


# ---------- save ----------

def save(values: dict[str, Any], config_path: Path, secrets_path: Path,
         extra: dict[str, dict] | None = None) -> None:
    """Write both files atomically-ish (temp + replace) keeping one .bak each."""
    for path, text in ((config_path, render_config_toml(values, extra)),
                       (secrets_path, render_secrets_toml(values))):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tomllib.loads(text)  # never write a file we cannot read back
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        tmp.replace(path)
