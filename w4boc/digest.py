"""Weekly (Mondays) and daily (while DEGRADED) digest email.

`maybe_send_scheduled(s)` is safe to call every minute — it only fires once
per local-day, at or after the configured digest time.
"""
import logging
from datetime import datetime, timedelta, timezone

from . import config
from . import mailer
from .battery import estimate_runtime
from .storage import Storage

log = logging.getLogger(__name__)

MODE_DEGRADED = "degraded"


def _utcnow():
    return datetime.now(timezone.utc)


def _fmt_local(dt_iso: str) -> str:
    return datetime.fromisoformat(dt_iso).astimezone(config.TZ).strftime("%Y-%m-%d %H:%M %Z")


def maybe_send_scheduled(s: Storage):
    now_local = datetime.now(config.TZ)
    today_key = now_local.strftime("%Y-%m-%d")
    if s.get_state("last_digest_day") == today_key:
        return
    fire_time = now_local.replace(
        hour=config.DIGEST_HOUR, minute=config.DIGEST_MINUTE, second=0, microsecond=0
    )
    if now_local < fire_time:
        return  # too early today

    mode = s.get_state("mode") or "normal"
    if mode == MODE_DEGRADED:
        _send(s, "Daily", timedelta(hours=24))
    # NOTE (2026-04-26): weekly digest disabled per operator preference. The
    # operator can check the dashboard via Tailscale at any time, and any
    # real incident still produces an URGENT email. We still mark today as
    # "digest day" so the scheduler doesn't re-evaluate every minute.
    s.set_state("last_digest_day", today_key)


def _send(s: Storage, kind: str, period: timedelta):
    since = _utcnow() - period
    stats = s.bms_stats_since(since)
    chg_time = s.charger_state_time_since(since)
    events = s.events_since(since)
    bms = s.latest_bms()
    chg = s.latest_charger()
    mode = s.get_state("mode") or "normal"

    soc_now = bms["soc_pct"] if bms else "—"
    n_urgent = sum(1 for e in events if e["severity"] == "urgent")
    tail = f", {n_urgent} alert" + ("s" if n_urgent != 1 else "") if n_urgent else ", no events"
    subject = f"[{config.SITE_NAME}] {kind} summary — {soc_now}% SoC{tail}"

    body = _body(kind, period, stats, chg_time, events, bms, chg, mode,
                 runtime=estimate_runtime(s))
    sent = mailer.send(subject, body)
    s.log_event("digest_sent", "info", f"{kind.lower()}: sent={sent}; {subject}")


def _body(kind, period, stats, chg_time, events, bms, chg, mode, runtime=None) -> str:
    lines = [
        f"Site: {config.SITE_NAME}",
        f"Digest: {kind.lower()} ({period.days}-day window)",
        f"Mode: {mode.upper()}",
        "",
        "=== Current state ===",
    ]
    if bms:
        temp = f"{bms['temp_c']:.1f} °C" if bms.get("temp_c") is not None else "—"
        lines += [
            f"SoC: {bms['soc_pct']}%  ({bms['residual_ah']:.1f} Ah residual)",
            f"Voltage: {bms['pack_voltage']:.2f} V   Current: {bms['pack_current']:+.2f} A",
            f"Temperature: {temp}",
            f"Cells: {', '.join(f'{v:.3f}' for v in bms['cells'])} V",
            f"FETs: charge={'ON' if bms['charge_fet'] else 'OFF'}, "
            f"discharge={'ON' if bms['discharge_fet'] else 'OFF'}",
        ]
    if chg:
        lines += [
            f"Charger: {chg['state']}, {chg['voltage']:.1f} V @ {chg['current']:.1f} A, err={chg['error']}",
        ]
    if runtime is not None:
        lines.append(f"Estimated runtime: {runtime.text} (monitoring stops when the BMS cuts off)")

    lines += ["", "=== BMS over period ==="]
    if stats and stats.get("n"):
        lines += [
            f"Samples: {stats['n']}",
            f"Voltage: min {stats['v_min']:.2f}  max {stats['v_max']:.2f}  avg {stats['v_avg']:.2f} V",
            f"SoC:     min {stats['soc_min']}%  max {stats['soc_max']}%",
        ]
        if stats["t_min"] is not None:
            lines.append(f"Temp:    min {stats['t_min']:.1f}  max {stats['t_max']:.1f} °C")
        delta = (stats["cyc_end"] or 0) - (stats["cyc_start"] or 0)
        lines.append(f"Cycles:  {stats['cyc_start']} → {stats['cyc_end']}  (+{delta})")
    else:
        lines.append("No BMS samples in period.")

    lines += ["", "=== Charger time-in-state (sample counts) ==="]
    if chg_time:
        for state, count in sorted(chg_time.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {state or '(unknown)'}: {count}")
    else:
        lines.append("No charger samples in period.")

    lines += ["", "=== Events ==="]
    if events:
        for e in events:
            lines.append(f"  {_fmt_local(e['ts'])} [{e['severity']}] {e['kind']}: {e['detail']}")
    else:
        lines.append("  (none)")

    lines += ["", f"— {config.SITE_NAME} Battery Monitor"]
    return "\n".join(lines)
