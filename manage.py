"""Operational management tool for the W4BOC Battery Monitor.

Run with no args for help. Safe to use while the monitor is running — uses
the same WAL-mode SQLite database; if the underlying condition for an alert
is still true after we clear it, the monitor will simply re-fire on the next
evaluator tick (which is correct behavior).

Examples:
  python manage.py status
  python manage.py reset-alerts
  python manage.py reset-alert mains_lost
  python manage.py reset-mode
  python manage.py force-digest
  python manage.py mains            (mains-power state + outage history)
  python manage.py check-update     (query GitHub for a newer release)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w4boc import __version__, config  # noqa: E402

KNOWN_TRIGGERS = [
    "soc_critical",
    "soc_dropping_fast",
    "protection_tripped",
    "charger_error",
    "mains_lost",
    "cell_imbalance",
    "temp_high",
    "temp_low",
    "battery_final",
]


def _open() -> sqlite3.Connection:
    db = sqlite3.connect(config.DB_PATH, timeout=10)
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _audit(db: sqlite3.Connection, detail: str) -> None:
    db.execute(
        "INSERT INTO events (ts, kind, severity, detail) VALUES (?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "manage",
            "info",
            detail,
        ),
    )


def cmd_status(_args) -> None:
    db = _open()
    print(f"=== W4BOC Battery Monitor v{__version__} ===")
    print("=== Mode / mains / liveness ===")
    for row in db.execute(
        "SELECT key, value FROM state "
        "WHERE key IN ('mode','degraded_since','recovery_start','charger_state',"
        "'mains_state','mains_since','last_heartbeat','monitoring_since')"
        " ORDER BY key"
    ):
        print(f"  {row[0]:<20} {row[1] or '(empty)'}")

    print()
    print("=== Currently ACTIVE (fired, not yet resolved) ===")
    rows = list(
        db.execute(
            "SELECT key, value FROM state WHERE key LIKE 'active_%'"
            " AND value != '' ORDER BY key"
        )
    )
    if rows:
        for k, v in rows:
            print(f"  {k.replace('active_', ''):<22} active since {v}")
    else:
        print("  (none — no incidents in flight)")

    print()
    print("=== Last urgent fire time per trigger (UTC) ===")
    rows = list(
        db.execute(
            "SELECT key, value FROM state WHERE key LIKE 'urgent_last_%' ORDER BY key"
        )
    )
    if rows:
        for k, v in rows:
            print(f"  {k.replace('urgent_last_', ''):<22} {v}")
    else:
        print("  (none — every trigger is armed and ready to fire)")

    print()
    row = db.execute("SELECT value FROM state WHERE key='last_digest_day'").fetchone()
    print(f"=== Last digest sent (local-day key): {row[0] if row else '(never)'}")
    db.close()


def cmd_reset_alerts(_args) -> None:
    db = _open()
    n_rate = db.execute("DELETE FROM state WHERE key LIKE 'urgent_last_%'").rowcount
    n_active = db.execute("DELETE FROM state WHERE key LIKE 'active_%'").rowcount
    _audit(db, f"reset-alerts cleared {n_rate} rate-limit and {n_active} active flag(s)")
    db.commit()
    db.close()
    print(f"Cleared {n_rate} rate-limit clock(s) and {n_active} active flag(s).")
    print("All triggers re-armed; resolution emails will fire if a flag was set.")


def cmd_reset_alert(args) -> None:
    trigger = args.trigger
    if trigger not in KNOWN_TRIGGERS:
        print(f"Unknown trigger: {trigger!r}")
        print(f"Known: {', '.join(KNOWN_TRIGGERS)}")
        sys.exit(2)
    db = _open()
    n_rate = db.execute(
        "DELETE FROM state WHERE key=?", (f"urgent_last_{trigger}",)
    ).rowcount
    n_active = db.execute(
        "DELETE FROM state WHERE key=?", (f"active_{trigger}",)
    ).rowcount
    _audit(db, f"reset-alert {trigger} (rate={n_rate}, active={n_active})")
    db.commit()
    db.close()
    print(f"reset-alert {trigger}: rate={n_rate}, active={n_active}.")


def cmd_reset_mode(_args) -> None:
    db = _open()
    cur = db.execute("SELECT value FROM state WHERE key='mode'").fetchone()
    prev = (cur[0] if cur else None) or "normal"
    if prev == "normal":
        db.close()
        print("Mode is already NORMAL; nothing to do.")
        return
    db.execute(
        "INSERT INTO state(key,value) VALUES('mode','normal')"
        " ON CONFLICT(key) DO UPDATE SET value='normal'"
    )
    db.execute("UPDATE state SET value='' WHERE key IN ('degraded_since','recovery_start')")
    _audit(db, f"reset-mode: forced {prev} -> normal")
    db.commit()
    db.close()
    print(f"Mode forced to NORMAL (was: {prev}).")


def cmd_force_digest(_args) -> None:
    db = _open()
    cur = db.execute("SELECT value FROM state WHERE key='last_digest_day'").fetchone()
    prev = cur[0] if cur else "(unset)"
    db.execute("DELETE FROM state WHERE key='last_digest_day'")
    _audit(db, f"force-digest cleared last_digest_day (was {prev})")
    db.commit()
    db.close()
    print(
        f"Cleared last_digest_day (was: {prev}). The next scheduled digest "
        "(after the configured digest_hour) will fire today."
    )


def cmd_mains(args) -> None:
    db = _open()
    print("=== Mains power ===")
    for k in ("mains_state", "mains_since", "mains_reason", "charger_silence_restart_ts"):
        row = db.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
        print(f"  {k:<28} {row[0] if row and row[0] else '(empty)'}")
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat(timespec="seconds")
    print(f"\n=== Power events, last {args.days} days (UTC) ===")
    rows = db.execute(
        "SELECT ts, state, confidence, outage_s, reason FROM mains_events WHERE ts >= ? ORDER BY ts",
        (since,),
    ).fetchall()
    if not rows:
        print("  (none)")
    for ts, state, conf, outage, reason in rows:
        dur = f"{outage // 3600}h{(outage % 3600) // 60:02d}m" if outage else ""
        print(f"  {ts}  {state:<9} {dur:>7}  [{conf}] {reason}")
    db.close()


def cmd_check_update(_args) -> None:
    from w4boc.updater import Updater, is_newer
    upd = Updater(config.UPDATER_REPO, __version__, config.UPDATES_DIR,
                  Path(__file__).resolve().parent, token=config.UPDATER_TOKEN)
    rel = upd.check()
    if rel is None:
        print(f"check failed: {upd.state.last_error}")
        sys.exit(1)
    print(f"running v{__version__}; latest release v{rel.version} ({rel.html_url})")
    if is_newer(rel.version, __version__):
        print("A newer version is available. The running monitor installs it on its next")
        print("check (auto_install) or via the dashboard's Install button.")
    else:
        print("Up to date.")


def main() -> None:
    p = argparse.ArgumentParser(
        description=f"W4BOC Battery Monitor v{__version__} -- management tools."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Show mode, alert rate-limits, last digest")
    sub.add_parser("reset-alerts", help="Clear all urgent alert rate-limit clocks")
    rp = sub.add_parser("reset-alert", help="Clear one specific trigger's rate-limit")
    rp.add_argument("trigger", help=f"one of: {', '.join(KNOWN_TRIGGERS)}")
    sub.add_parser("reset-mode", help="Force mode back to NORMAL (exit DEGRADED)")
    sub.add_parser("force-digest", help="Allow the next scheduled digest to fire today")
    mp = sub.add_parser("mains", help="Show mains-power state and outage history")
    mp.add_argument("--days", type=int, default=90)
    sub.add_parser("check-update", help="Query GitHub for a newer release")
    args = p.parse_args()
    {
        "status": cmd_status,
        "reset-alerts": cmd_reset_alerts,
        "reset-alert": cmd_reset_alert,
        "reset-mode": cmd_reset_mode,
        "force-digest": cmd_force_digest,
        "mains": cmd_mains,
        "check-update": cmd_check_update,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
