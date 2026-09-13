"""Alert evaluator. Called once a minute by the evaluator task.

Responsibilities:
  1. Fire urgent emails for critical conditions (rate-limited per trigger type).
  2. Track NORMAL → DEGRADED → recovery mode transitions in KV storage.

Modes:
  NORMAL   — no scheduled digest
  DEGRADED — daily digest; entered if SoC < SOC_DEGRADED OR mains lost
  Recovery — SoC >= SOC_RECOVERY and mains OK for MODE_RECOVERY_MINUTES → NORMAL

Mains-loss detection itself lives in mains.py; this module only reacts to
the tracker's verdict (`MainsView`).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config
from . import mailer
from .battery import RuntimeEstimate, estimate_runtime
from .mains import fmt_duration
from .storage import Storage

log = logging.getLogger(__name__)

MODE_NORMAL = "normal"
MODE_DEGRADED = "degraded"


@dataclass
class MainsView:
    lost: bool = False
    reason: str = ""
    since: datetime | None = None
    confidence: str = "n/a"
    outage_s: int | None = None     # set on the tick that restores mains


def _utcnow():
    return datetime.now(timezone.utc)


def _rate_limited(s: Storage, trigger: str) -> bool:
    last = s.get_state(f"urgent_last_{trigger}")
    if not last:
        return False
    return _utcnow() - datetime.fromisoformat(last) < timedelta(minutes=config.URGENT_RATE_LIMIT_MINUTES)


def _mark(s: Storage, trigger: str):
    s.set_state(f"urgent_last_{trigger}", _utcnow().isoformat(timespec="seconds"))


def _fire(s: Storage, trigger: str, subject: str, body: str,
          severity: str = "urgent"):
    """One-shot alert. Used for transient triggers that don't have a
    meaningful 'resolved' state (e.g. rapid SoC drop). Rate-limited per
    `URGENT_RATE_LIMIT_MINUTES` so they can't spam."""
    if _rate_limited(s, trigger):
        log.debug(f"alert '{trigger}' suppressed (rate-limited)")
        return
    sent = mailer.send(subject, body)
    _mark(s, trigger)
    s.log_event("alert_fired", severity, f"{trigger}: sent={sent}; {subject}")


def _track(s: Storage, trigger: str, active: bool,
           fire_subject: str, resolve_subject: str, body: str,
           severity: str = "urgent"):
    """Edge-triggered alert. Fire on rising edge (condition becoming true),
    send a 'RESOLVED' email on falling edge (condition becoming false).
    Idempotent — once active, won't refire until it has resolved.

    The `active_<trigger>` flag in the state KV table is the source of truth
    for whether we've already sent the fire email. Operators can clear it
    manually via `manage.py reset-alerts`.
    """
    flag_key = f"active_{trigger}"
    was_active = bool(s.get_state(flag_key))
    if active and not was_active:
        sent = mailer.send(fire_subject, body)
        _mark(s, trigger)  # also set urgent_last_<trigger> for the digest scheduler
        s.set_state(flag_key, _utcnow().isoformat(timespec="seconds"))
        s.log_event("alert_fired", severity,
                    f"{trigger}: sent={sent}; {fire_subject}")
    elif not active and was_active:
        sent = mailer.send(resolve_subject, body)
        s.set_state(flag_key, "")
        s.log_event("alert_resolved", "info",
                    f"{trigger}: sent={sent}; {resolve_subject}")


def _track_hyst(s: Storage, trigger: str, on: bool, off: bool,
                fire_subject: str, resolve_subject: str, body: str,
                severity: str = "urgent") -> str | None:
    """Like _track() but with hysteresis: `on` raises, `off` clears, neither
    keeps the current state. Returns 'fired' | 'resolved' | None."""
    flag_key = f"active_{trigger}"
    was_active = bool(s.get_state(flag_key))
    if on and not was_active:
        _track(s, trigger, True, fire_subject, resolve_subject, body, severity)
        return "fired"
    if off and was_active:
        _track(s, trigger, False, fire_subject, resolve_subject, body, severity)
        return "resolved"
    return None


def evaluate(s: Storage, mains: MainsView | None = None) -> list[str]:
    """One evaluator pass. Returns APRS status texts to broadcast (usually none)."""
    mains = mains or MainsView()
    bms = s.latest_bms()
    chg = s.latest_charger()
    now = _utcnow()
    runtime = estimate_runtime(s, now=now)
    body = _urgent_body(bms, chg, mains, runtime)
    site = config.SITE_NAME
    statuses: list[str] = []

    # ---- edge-triggered (fire + resolve) ----

    soc = bms["soc_pct"] if bms and bms.get("soc_pct") is not None else None
    if soc is not None:
        _track(s, "soc_critical", soc < config.SOC_URGENT,
               f"[{site}] URGENT: SoC {soc}%, below {config.SOC_URGENT}%",
               f"[{site}] RESOLVED: SoC recovered to {soc}% (threshold {config.SOC_URGENT}%)",
               body)

    if bms:
        protections = bms.get("protections") or []
        _track(s, "protection_tripped", bool(protections),
               f"[{site}] URGENT: BMS protection: {', '.join(protections) or '(unknown)'}",
               f"[{site}] RESOLVED: BMS protection cleared",
               body)

        # Cell spread — observed in the events log and digests only, no email.
        cells = bms.get("cells") or []
        spread_mv = int(round((max(cells) - min(cells)) * 1000)) if len(cells) > 1 else 0
        was_high = bool(s.get_state("active_cell_imbalance"))
        is_high = spread_mv > config.CELL_SPREAD_MV
        if is_high and not was_high:
            s.set_state("active_cell_imbalance", _utcnow().isoformat(timespec="seconds"))
            s.log_event("cell_imbalance", "warn",
                        f"cell spread {spread_mv} mV (> {config.CELL_SPREAD_MV} mV)")
        elif not is_high and was_high:
            s.set_state("active_cell_imbalance", "")
            s.log_event("cell_imbalance_cleared", "info",
                        f"cell spread back to {spread_mv} mV")

        t = bms.get("temp_c")
        if t is not None:
            _track(s, "temp_high", t > config.TEMP_HIGH_C,
                   f"[{site}] URGENT: battery {t:.1f} C (> {config.TEMP_HIGH_C} C)",
                   f"[{site}] RESOLVED: battery temperature back to {t:.1f} C",
                   body)
            _track(s, "temp_low", t < config.TEMP_LOW_C,
                   f"[{site}] URGENT: battery {t:.1f} C (< {config.TEMP_LOW_C} C)",
                   f"[{site}] RESOLVED: battery temperature back to {t:.1f} C",
                   body)

    # Final warning: the monitor PC runs from this battery, so this is the
    # last email before the BMS cuts everything (including us) off.
    if bms and soc is not None and bms.get("pack_voltage") is not None:
        v = bms["pack_voltage"]
        discharging = (bms.get("pack_current") or 0) < 0
        on = soc <= config.BATTERY_FINAL_SOC or (discharging and v <= config.BATTERY_FINAL_VOLTAGE)
        off = soc >= config.BATTERY_FINAL_SOC + 5 and v > config.BATTERY_FINAL_VOLTAGE + 0.3
        left = f", {runtime.text} left" if runtime else ""
        result = _track_hyst(
            s, "battery_final", on, off,
            f"[{site}] URGENT: battery nearly exhausted — SoC {soc}%, {v:.2f} V{left}; "
            f"monitoring stops when the BMS cuts off",
            f"[{site}] RESOLVED: battery recovered — SoC {soc}%, {v:.2f} V",
            body)
        if result == "fired" and config.BATTERY_FINAL_APRS:
            statuses.append(f"BATTERY LOW {v:.2f}V {soc}%" + (f" {runtime.short}" if runtime else ""))

    chg_err = chg.get("error") if chg else None
    chg_error_active = bool(chg_err and chg_err not in ("", "NO_ERROR"))
    _track(s, "charger_error", chg_error_active,
           f"[{site}] URGENT: charger error: {chg_err}",
           f"[{site}] RESOLVED: charger error cleared (was {chg_err})",
           body)

    if mains.outage_s is not None:
        outage = fmt_duration(mains.outage_s)
    elif mains.since:
        outage = fmt_duration((now - mains.since).total_seconds())
    else:
        outage = "?"
    _track(s, "mains_lost", mains.lost,
           f"[{site}] URGENT: MAINS POWER LOST — {mains.reason or 'charger silent'}",
           f"[{site}] RESOLVED: mains power restored after {outage}",
           body)

    # ---- one-shot (fire only, transient by nature) ----

    if soc is not None:
        prior = s.soc_at_or_before(now - timedelta(hours=1))
        if prior is not None and (prior - soc) >= config.SOC_DROP_PER_HOUR:
            _fire(s, "soc_dropping_fast",
                  f"[{site}] URGENT: SoC dropped {prior - soc}% "
                  f"in last hour ({prior}% -> {soc}%)",
                  body)

    # Charger state transitions: log every change to events (no email — the
    # Victron does refresh cycles routinely; STORAGE -> BULK is normal).
    if chg:
        new_state = chg.get("state")
        if new_state:
            prev_state = s.get_state("charger_state")
            if new_state != prev_state:
                s.set_state("charger_state", new_state)
                if prev_state:
                    s.log_event("charger_state_change", "info",
                                f"{prev_state} -> {new_state}")

    _update_mode(s, bms, mains, now)
    return statuses


def _degraded_cause(bms, mains: MainsView) -> str | None:
    if bms and bms["soc_pct"] is not None and bms["soc_pct"] < config.SOC_DEGRADED:
        return f"SoC {bms['soc_pct']}% < {config.SOC_DEGRADED}%"
    if mains.lost:
        return "mains lost"
    return None


def _is_clear_for_recovery(bms, mains: MainsView) -> bool:
    """Hysteresis: while in DEGRADED, only consider the situation 'clear' once
    SoC is comfortably above the recovery threshold (not just above the
    degradation threshold) AND mains is healthy. Prevents mode flapping."""
    soc_ok = (bms is None
              or bms["soc_pct"] is None
              or bms["soc_pct"] >= config.SOC_RECOVERY)
    return soc_ok and not mains.lost


def _update_mode(s: Storage, bms, mains: MainsView, now):
    current = s.get_state("mode") or MODE_NORMAL
    cause = _degraded_cause(bms, mains)

    if current == MODE_NORMAL:
        if cause:
            s.set_state("mode", MODE_DEGRADED)
            s.set_state("degraded_since", now.isoformat(timespec="seconds"))
            s.set_state("recovery_start", "")
            s.log_event("mode_change", "warn", f"entered DEGRADED: {cause}")
        return

    # current == MODE_DEGRADED
    if not _is_clear_for_recovery(bms, mains):
        if s.get_state("recovery_start"):
            s.set_state("recovery_start", "")
        return

    rec_start = s.get_state("recovery_start")
    if not rec_start:
        s.set_state("recovery_start", now.isoformat(timespec="seconds"))
        return
    clear_for = now - datetime.fromisoformat(rec_start)
    if clear_for >= timedelta(minutes=config.MODE_RECOVERY_MINUTES):
        s.set_state("mode", MODE_NORMAL)
        s.set_state("recovery_start", "")
        mins = int(clear_for.total_seconds() / 60)
        s.log_event("mode_change", "info",
                    f"recovered to NORMAL (cause clear for {mins} min)")


def _urgent_body(bms, chg, mains: MainsView | None = None,
                 runtime: RuntimeEstimate | None = None) -> str:
    lines = [f"Site: {config.SITE_NAME}", ""]
    if mains is not None:
        state = "LOST" if mains.lost else "on"
        since = mains.since.astimezone(config.TZ).strftime("%Y-%m-%d %H:%M %Z") if mains.since else "?"
        lines += [f"Mains power: {state} (since {since})"]
        if mains.reason:
            lines.append(f"  {mains.reason} [{mains.confidence} confidence]")
        lines.append("")
    if runtime is not None:
        lines += [
            f"Estimated runtime: {runtime.text} ({runtime.residual_ah:.0f} Ah residual, "
            f"average of the last {runtime.window_min} min). The monitor PC runs from this "
            f"battery, so monitoring stops when the BMS cuts off.",
            "",
        ]
    if bms:
        temp = f"{bms['temp_c']:.1f} °C" if bms.get("temp_c") is not None else "—"
        lines += [
            f"BMS (ts {bms['ts']}):",
            f"  Pack voltage: {bms['pack_voltage']:.2f} V",
            f"  Pack current: {bms['pack_current']:+.2f} A",
            f"  SoC: {bms['soc_pct']}%",
            f"  Residual: {bms['residual_ah']:.1f} Ah",
            f"  Temperature: {temp}",
            f"  FETs: charge={'ON' if bms['charge_fet'] else 'OFF'}, "
            f"discharge={'ON' if bms['discharge_fet'] else 'OFF'}",
            f"  Protections: {', '.join(bms['protections']) or 'none'}",
            f"  Cells: {', '.join(f'{v:.3f}' for v in bms['cells'])} V",
        ]
    if chg:
        lines += [
            "",
            f"Charger (ts {chg['ts']}):",
            f"  State: {chg['state']}",
            f"  Output: {chg['voltage']:.1f} V @ {chg['current']:.1f} A",
            f"  Error: {chg['error']}",
        ]
    return "\n".join(lines)
