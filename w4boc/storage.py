"""SQLite storage for samples, events, mains-power history and alert state.

All timestamps are stored as UTC ISO-8601 text for readability; convert to
local time at display. WAL mode so readers (the dashboard) never block writes.

One `Storage` instance is shared by the asyncio tasks and — occasionally — by
dashboard/updater threads, so every statement runs under a lock.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bms_samples (
    ts              TEXT NOT NULL,     -- UTC ISO-8601
    pack_voltage    REAL,
    pack_current    REAL,
    soc_pct         INTEGER,
    residual_ah     REAL,
    cycle_count     INTEGER,
    temp_c          REAL,              -- first NTC, representative
    charge_fet      INTEGER,           -- 0/1
    discharge_fet   INTEGER,
    cells_json      TEXT,              -- JSON array of cell voltages
    protections_json TEXT              -- JSON array of active protection names
);
CREATE INDEX IF NOT EXISTS ix_bms_ts ON bms_samples(ts);

CREATE TABLE IF NOT EXISTS charger_samples (
    ts              TEXT NOT NULL,
    voltage         REAL,
    current         REAL,
    state           TEXT,              -- OperationMode name: BULK, ABSORPTION, FLOAT, STORAGE, OFF, ...
    error           TEXT               -- ChargerError name
);
CREATE INDEX IF NOT EXISTS ix_chg_ts ON charger_samples(ts);

CREATE TABLE IF NOT EXISTS events (
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL,     -- mode_change, alert_fired, error, digest_sent, ...
    severity        TEXT NOT NULL,     -- info, warn, urgent
    detail          TEXT               -- free text
);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_ev_kind ON events(kind);

CREATE TABLE IF NOT EXISTS state (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- Mains (AC) power transitions. One row per transition; `outage_s` is filled
-- on the 'restored' row with the length of the outage that just ended.
CREATE TABLE IF NOT EXISTS mains_events (
    ts              TEXT NOT NULL,
    state           TEXT NOT NULL,     -- 'lost' | 'restored' | 'pc_down'
    reason          TEXT,              -- how we know (charger silent, UPS, boot gap ...)
    confidence      TEXT,              -- 'high' | 'low'
    outage_s        INTEGER            -- duration of the outage that ended (restored rows)
);
CREATE INDEX IF NOT EXISTS ix_mains_ts ON mains_events(ts);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Storage:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else config.DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=10
        )  # autocommit
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    # ---- BMS ----

    def write_bms(self, info, cells: list[float]):
        self._exec(
            "INSERT INTO bms_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                info.pack_voltage,
                info.pack_current,
                info.soc_percent,
                info.residual_capacity,
                info.cycle_count,
                info.temperatures_c[0] if info.temperatures_c else None,
                int(info.charge_fet_on),
                int(info.discharge_fet_on),
                json.dumps(cells),
                json.dumps(info.protections),
            ),
        )

    _BMS_KEYS = ["ts", "pack_voltage", "pack_current", "soc_pct", "residual_ah",
                 "cycle_count", "temp_c", "charge_fet", "discharge_fet",
                 "cells_json", "protections_json"]

    def _bms_row_to_dict(self, row) -> dict:
        d = dict(zip(self._BMS_KEYS, row))
        d["cells"] = json.loads(d.pop("cells_json") or "[]")
        d["protections"] = json.loads(d.pop("protections_json") or "[]")
        return d

    def latest_bms(self) -> dict | None:
        row = self._exec(
            "SELECT ts, pack_voltage, pack_current, soc_pct, residual_ah, cycle_count,"
            " temp_c, charge_fet, discharge_fet, cells_json, protections_json"
            " FROM bms_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return self._bms_row_to_dict(row) if row else None

    def recent_bms(self, n: int) -> list[dict]:
        """Most recent `n` BMS samples, newest first."""
        rows = self._exec(
            "SELECT ts, pack_voltage, pack_current, soc_pct, residual_ah, cycle_count,"
            " temp_c, charge_fet, discharge_fet, cells_json, protections_json"
            " FROM bms_samples ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
        return [self._bms_row_to_dict(r) for r in rows]

    def avg_current_since(self, since: datetime) -> tuple[float | None, int]:
        """(average pack current, sample count) over samples newer than `since`."""
        row = self._exec(
            "SELECT AVG(pack_current), COUNT(pack_current) FROM bms_samples WHERE ts >= ?",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchone()
        if not row or row[0] is None:
            return None, 0
        return float(row[0]), int(row[1])

    def soc_at_or_before(self, when: datetime) -> int | None:
        row = self._exec(
            "SELECT soc_pct FROM bms_samples WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (when.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchone()
        return row[0] if row else None

    # ---- Charger ----

    def write_charger(self, voltage: float, current: float, state: str, error: str):
        self._exec(
            "INSERT INTO charger_samples VALUES (?, ?, ?, ?, ?)",
            (now_iso(), voltage, current, state, error),
        )

    def latest_charger(self) -> dict | None:
        row = self._exec(
            "SELECT ts, voltage, current, state, error FROM charger_samples"
            " ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return dict(zip(["ts", "voltage", "current", "state", "error"], row))

    def last_charger_seen(self) -> datetime | None:
        """When was the most recent charger advertisement? Primary mains signal."""
        row = self._exec("SELECT MAX(ts) FROM charger_samples").fetchone()
        return parse_ts(row[0]) if row and row[0] else None

    def last_bms_seen(self) -> datetime | None:
        """When was the most recent BMS sample written? Used by the watchdog."""
        row = self._exec("SELECT MAX(ts) FROM bms_samples").fetchone()
        return parse_ts(row[0]) if row and row[0] else None

    # ---- Events ----

    def log_event(self, kind: str, severity: str, detail: str = ""):
        self._exec(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            (now_iso(), kind, severity, detail),
        )

    def events_since(self, since: datetime) -> list[dict]:
        rows = self._exec(
            "SELECT ts, kind, severity, detail FROM events WHERE ts >= ? ORDER BY ts",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall()
        return [dict(zip(["ts", "kind", "severity", "detail"], r)) for r in rows]

    def recent_events(self, n: int = 30) -> list[dict]:
        rows = self._exec(
            "SELECT ts, kind, severity, detail FROM events ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(zip(["ts", "kind", "severity", "detail"], r)) for r in rows]

    # ---- KV state (for rate-limiting, mode tracking) ----

    def get_state(self, key: str) -> str | None:
        row = self._exec("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str):
        self._exec(
            "INSERT INTO state(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_state_ts(self, key: str) -> datetime | None:
        return parse_ts(self.get_state(key))

    # ---- Mains power ----

    def write_mains_event(self, state: str, reason: str, confidence: str,
                          outage_s: int | None = None, ts: datetime | None = None):
        when = ts.astimezone(timezone.utc).isoformat(timespec="seconds") if ts else now_iso()
        self._exec(
            "INSERT INTO mains_events VALUES (?, ?, ?, ?, ?)",
            (when, state, reason, confidence, outage_s),
        )

    def latest_mains_event(self) -> dict | None:
        row = self._exec(
            "SELECT ts, state, reason, confidence, outage_s FROM mains_events"
            " ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return dict(zip(["ts", "state", "reason", "confidence", "outage_s"], row))

    def mains_events_since(self, since: datetime, limit: int = 200) -> list[dict]:
        rows = self._exec(
            "SELECT ts, state, reason, confidence, outage_s FROM mains_events"
            " WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"), limit),
        ).fetchall()
        return [dict(zip(["ts", "state", "reason", "confidence", "outage_s"], r)) for r in rows]

    # ---- Summary queries for digests ----

    def bms_stats_since(self, since: datetime) -> dict:
        row = self._exec(
            "SELECT MIN(pack_voltage), MAX(pack_voltage), AVG(pack_voltage),"
            " MIN(soc_pct), MAX(soc_pct),"
            " MIN(temp_c), MAX(temp_c),"
            " MIN(cycle_count), MAX(cycle_count),"
            " COUNT(*)"
            " FROM bms_samples WHERE ts >= ?",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchone()
        keys = ["v_min", "v_max", "v_avg", "soc_min", "soc_max",
                "t_min", "t_max", "cyc_start", "cyc_end", "n"]
        return dict(zip(keys, row))

    def charger_state_time_since(self, since: datetime) -> dict[str, int]:
        """How many samples in each state since `since` (a proxy for time-in-state)."""
        rows = self._exec(
            "SELECT state, COUNT(*) FROM charger_samples WHERE ts >= ? GROUP BY state",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall()
        return dict(rows)

    # ---- Retention / downsampling ----

    def prune(self, raw_days: int = 7, minute_days: int = 30, max_days: int = 365) -> dict:
        """Apply tiered retention to bms_samples and charger_samples:
          - within last `raw_days`: keep raw resolution
          - in (raw_days, minute_days]: keep 1 row per minute (oldest in each bucket)
          - in (minute_days, max_days]: keep 1 row per hour
          - older than `max_days`: dropped entirely
        Events, mains history and KV state are untouched.
        Returns {table_name: rows_deleted}.
        """
        counts: dict[str, int] = {}
        with self._lock:
            for table in ("bms_samples", "charger_samples"):
                n = self._conn.execute(
                    f"DELETE FROM {table} WHERE ts < datetime('now', ?)",
                    (f"-{max_days} days",),
                ).rowcount
                n += self._conn.execute(
                    f"""
                    DELETE FROM {table} WHERE rowid IN (
                        SELECT rowid FROM (
                            SELECT rowid, ROW_NUMBER() OVER (
                                PARTITION BY substr(ts, 1, 13) ORDER BY ts
                            ) AS rn FROM {table}
                            WHERE ts <  datetime('now', ?)
                              AND ts >= datetime('now', ?)
                        ) WHERE rn > 1
                    )
                    """,
                    (f"-{minute_days} days", f"-{max_days} days"),
                ).rowcount
                n += self._conn.execute(
                    f"""
                    DELETE FROM {table} WHERE rowid IN (
                        SELECT rowid FROM (
                            SELECT rowid, ROW_NUMBER() OVER (
                                PARTITION BY substr(ts, 1, 16) ORDER BY ts
                            ) AS rn FROM {table}
                            WHERE ts <  datetime('now', ?)
                              AND ts >= datetime('now', ?)
                        ) WHERE rn > 1
                    )
                    """,
                    (f"-{raw_days} days", f"-{minute_days} days"),
                ).rowcount
                counts[table] = n
            # Truncate the WAL so deleted rows actually leave the on-disk footprint.
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return counts


@contextmanager
def open_storage(path: Path | None = None):
    s = Storage(path)
    try:
        yield s
    finally:
        s.close()
