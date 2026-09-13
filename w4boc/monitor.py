"""Monitoring tasks (single asyncio loop):

  bms_task          — GATT-poll the BMS every BMS_SAMPLE_PERIOD_S seconds
  charger_task      — passive listener for Victron Instant Readout advertisements
  evaluator_task    — mains detector + alert logic + digest scheduler, every 60 s
  sample_watchdog   — restart when BLE tasks silently stop producing samples
  housekeeping_task — DB retention once a day
  heartbeat_task    — proves the event loop is alive (file for launcher.py,
                      timestamp for the in-process thread watchdog, DB key
                      for the mains detector's continuity clock)

The BLE code paths are unchanged from v1 — they are the parts that have been
running against the real hardware since April 2026.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from . import alerts, battery, config, digest, mailer, power
from .context import AppContext, EXIT_RESTART
from .mains import MainsInputs, fmt_duration

log = logging.getLogger("monitor")

VICTRON_MFG_ID = 0x02E1
HEARTBEAT_PERIOD_S = 15
LOOP_STALL_EXIT_S = 120


def _err_label(e: BaseException) -> str:
    """Format an exception for log/event display. Some exceptions stringify
    empty (notably asyncio.TimeoutError from Bleak's WinRT GATT timeout) —
    fall back to the class name only."""
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


# ---------- BMS ----------

async def _bms_poll_attempt():
    """Single connect+read. Returns (info, cells), or None if device not
    discoverable, or raises the underlying BLE exception."""
    from bleak import BleakClient, BleakScanner
    from .bms import JbdReader

    device = await BleakScanner.find_device_by_address(config.BMS_MAC, timeout=10.0)
    if device is None:
        return None
    async with BleakClient(device) as client:
        r = JbdReader(client)
        await r.start()
        info = await r.basic_info()
        cells = await r.cell_voltages()
    return (info, cells)


async def bms_task(ctx: AppContext):
    """Poll the BMS once per BMS_SAMPLE_PERIOD_S.

    Retries once on failure: the Windows WinRT BLE backend periodically
    hangs GATT service discovery (observed ~2/hour). A fresh scan +
    reconnect after a 2 s pause clears most of these transient hangs."""
    storage = ctx.storage
    while True:
        result = None
        first_err: BaseException | None = None
        for attempt in (1, 2):
            try:
                result = await _bms_poll_attempt()
                break
            except Exception as e:
                if attempt == 1:
                    first_err = e
                    log.warning(f"BMS poll attempt 1 failed: {_err_label(e)} — retrying in 2 s")
                    await asyncio.sleep(2)
                    continue
                log.exception(f"BMS poll failed (both attempts): {_err_label(e)}")
                storage.log_event("bms_error", "warn", _err_label(e))
                result = "FAILED"
                break

        if result == "FAILED":
            pass
        elif result is None:
            log.warning("BMS not discoverable (paired elsewhere?)")
            storage.log_event("bms_offline", "warn", "not discoverable")
        else:
            info, cells = result
            storage.write_bms(info, cells)
            tag = " (recovered on retry)" if first_err else ""
            log.info(
                f"BMS: {info.pack_voltage:.2f} V {info.pack_current:+.2f} A "
                f"SoC {info.soc_percent}% cells={[round(v, 3) for v in cells]}"
                f"{tag}"
            )

        await asyncio.sleep(config.BMS_SAMPLE_PERIOD_S)


# ---------- Charger ----------

def _safe(obj, name):
    try:
        return getattr(obj, name)()
    except Exception:
        return None


async def charger_task(ctx: AppContext):
    from bleak import BleakScanner
    from victron_ble.devices import detect_device_type

    storage = ctx.storage
    mac = config.VICTRON_MAC.upper()
    key = config.VICTRON_KEY
    state = {"last_write_ts": 0.0, "last_payload": None}

    def on_adv(device, adv):
        if device.address.upper() != mac:
            return
        mfg = adv.manufacturer_data.get(VICTRON_MFG_ID)
        if not mfg:
            return
        try:
            cls = detect_device_type(mfg)
            if cls is None:
                return
            parsed = cls(key).parse(mfg)
        except Exception as e:
            log.debug(f"Victron parse failed: {e}")
            return

        v = _safe(parsed, "get_output_voltage1") or 0.0
        i = _safe(parsed, "get_output_current1") or 0.0
        mode = _safe(parsed, "get_charge_state")
        err = _safe(parsed, "get_charger_error")
        mode_name = mode.name if mode is not None else ""
        err_name = err.name if err is not None else ""
        payload = (round(v, 2), round(i, 2), mode_name, err_name)

        now_mono = time.monotonic()
        last = state["last_payload"]
        significant = last is None or (
            payload[2] != last[2]
            or payload[3] != last[3]
            or abs(payload[0] - last[0]) >= 0.1
            or abs(payload[1] - last[1]) >= 0.5
        )
        elapsed = now_mono - state["last_write_ts"]
        if significant or elapsed >= config.CHARGER_SAMPLE_PERIOD_S:
            storage.write_charger(v, i, mode_name, err_name)
            state["last_write_ts"] = now_mono
            state["last_payload"] = payload
            log.info(f"Charger: {mode_name} {v:.1f} V @ {i:.1f} A err={err_name}")

    async with BleakScanner(on_adv):
        while True:
            await asyncio.sleep(3600)


# ---------- Evaluator (mains + alerts + digest) ----------

def _mains_inputs(ctx: AppContext, now: datetime) -> MainsInputs:
    s = ctx.storage
    bms = s.latest_bms()
    ac = "unknown"
    if config.MAINS_USE_WINDOWS_POWER and not ctx.simulate:
        ps = power.system_power_status()
        ctx.power_status = ps
        ac = ps.ac_line
    elif ctx.simulate and ctx.sim is not None:
        ac = ctx.sim.ac_line
    return MainsInputs(
        now=now,
        monitoring_since=ctx.monitoring_since,
        charger_last_seen=s.last_charger_seen(),
        bms_last_seen=s.last_bms_seen(),
        bms_current_a=bms["pack_current"] if bms else None,
        ac_line=ac,
        confirm_restart_ts=s.get_state_ts("charger_silence_restart_ts"),
    )


def _status_text(ctx: AppContext, transition: str) -> str:
    bms = ctx.storage.latest_bms()
    bits = []
    if bms:
        bits.append(f"{bms['pack_voltage']:.2f}V {bms['soc_pct']}%")
        if bms.get("pack_current") is not None:
            bits.append(f"{bms['pack_current']:+.1f}A")
    tail = " ".join(bits)
    if transition == "lost":
        rt = battery.estimate_runtime(ctx.storage)
        if rt:
            tail = f"{tail} {rt.short}".strip()
        return f"AC MAINS LOST {tail}".strip()
    last = ctx.storage.latest_mains_event()
    dur = fmt_duration(last["outage_s"]) if last and last.get("outage_s") is not None else "?"
    return f"AC MAINS RESTORED after {dur} {tail}".strip()


def evaluate_once(ctx: AppContext):
    """One evaluator tick: mains detector -> alerts -> digest -> APRS nudges."""
    s = ctx.storage
    now = datetime.now(timezone.utc)
    assessment, transition = ctx.mains.tick(_mains_inputs(ctx, now))
    ctx.aprs_bus.mains_lost = ctx.mains.lost
    view = alerts.MainsView(
        lost=ctx.mains.lost,
        reason=ctx.mains.reason,
        since=ctx.mains.since,
        confidence=assessment.confidence,
        outage_s=ctx.mains.last_outage_s if transition == "restored" else None,
    )
    for text in alerts.evaluate(s, view):
        ctx.aprs_bus.queue_status(text)
    digest.maybe_send_scheduled(s)
    if transition:
        log.warning(f"mains transition: {transition} — {assessment.reason}")
        if config.MAINS_APRS_STATUS:
            ctx.aprs_bus.queue_status(_status_text(ctx, transition))
        if config.MAINS_APRS_KICK:
            ctx.aprs_bus.kick()


async def evaluator_task(ctx: AppContext):
    while True:
        try:
            evaluate_once(ctx)
        except Exception as e:
            log.exception(f"evaluator failed: {e}")
        await asyncio.sleep(60)


# ---------- Heartbeat ----------

async def heartbeat_task(ctx: AppContext):
    """Every 15 s: touch logs/heartbeat.json (launcher), stamp the in-process
    liveness clock (thread watchdog) and persist last_heartbeat (mains
    continuity across restarts)."""
    path = config.LOG_DIR / "heartbeat.json"
    while True:
        try:
            ctx.loop_alive()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            ctx.storage.set_state("last_heartbeat", now)
            payload = json.dumps({"pid": os.getpid(), "ts": now, "version": ctx.version,
                                  "uptime_s": int(ctx.uptime_s())})
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            log.warning(f"heartbeat write failed: {_err_label(e)}")
        await asyncio.sleep(HEARTBEAT_PERIOD_S)


def start_thread_watchdog(ctx: AppContext) -> threading.Thread:
    """Plain thread: if the asyncio loop has not ticked for LOOP_STALL_EXIT_S
    the process is wedged inside a native call. Exit hard so the launcher
    restarts us; if even this thread cannot run, the launcher's heartbeat
    check kills the process from outside."""
    def run():
        while not ctx.stop_event.is_set():
            time.sleep(10)
            stalled = ctx.loop_stalled_s()
            if stalled > LOOP_STALL_EXIT_S:
                msg = f"event loop stalled for {int(stalled)}s — hard exit for restart"
                log.error(msg)
                try:
                    ctx.storage.log_event("loop_stall", "warn", msg)
                except Exception:
                    pass
                for h in logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
                os._exit(EXIT_RESTART)
    t = threading.Thread(target=run, name="loop-watchdog", daemon=True)
    t.start()
    return t


# ---------- Sample watchdog (v1 logic) ----------

async def sample_watchdog_task(ctx: AppContext):
    """Detect when BMS or charger BLE tasks have silently stopped writing.

    Bleak on Windows occasionally hangs a BLE await in a way that never
    surfaces as an exception. When no fresh samples arrive for 5x the
    configured sample period, request a restart (exit code 2) and only
    email the operators if the restart count in the last 24 h reaches
    WATCHDOG_EMAIL_THRESHOLD — single restarts are normal BLE noise.

    Charger silence is handled differently from v1: with mains down the
    charger is legitimately silent, and restarting every 5 minutes for the
    whole outage would (as in v1) keep the process too young to ever raise
    the mains-lost alert. Instead we restart exactly ONCE per silence
    period — a "confirmation restart" that rules out a dead advertisement
    scanner. The mains detector waits for that restart before declaring
    the outage (see mains.py).
    """
    storage = ctx.storage
    grace_s = config.BMS_SAMPLE_PERIOD_S * 5
    log.info(f"watchdog: arming after {grace_s}s grace period")
    await asyncio.sleep(grace_s)

    bms_threshold_s = config.BMS_SAMPLE_PERIOD_S * 5
    chg_threshold_s = config.MAINS_FAST_MINUTES * 60
    while True:
        try:
            now = datetime.now(timezone.utc)
            last_bms = storage.last_bms_seen()
            bms_age = (now - last_bms).total_seconds() if last_bms else None
            if bms_age is None or bms_age > bms_threshold_s:
                symptom = f"BMS: {int(bms_age) if bms_age else 'no sample'}s"
                _watchdog_trip(ctx, symptom, bms_threshold_s)
                await asyncio.sleep(2)  # let any pending SMTP send flush
                ctx.request_exit(EXIT_RESTART, f"sample watchdog: {symptom}")
                return

            last_chg = storage.last_charger_seen()
            chg_age = (now - last_chg).total_seconds() if last_chg else None
            marker = storage.get_state_ts("charger_silence_restart_ts")
            new_silence = last_chg is not None and (marker is None or marker < last_chg)
            if chg_age is not None and chg_age > chg_threshold_s and new_silence:
                storage.set_state("charger_silence_restart_ts", now.isoformat(timespec="seconds"))
                msg = (f"charger silent {int(chg_age)}s while BMS answers — one "
                       f"confirmation restart to rule out a dead BLE scanner")
                log.warning(f"watchdog: {msg}")
                storage.log_event("watchdog_restart", "info", msg)
                await asyncio.sleep(1)
                ctx.request_exit(EXIT_RESTART, "charger-silence confirmation restart")
                return
        except Exception as e:
            log.exception(f"watchdog tick failed: {_err_label(e)}")
        await asyncio.sleep(60)


def _watchdog_trip(ctx: AppContext, symptom: str, bms_threshold_s: int) -> None:
    storage = ctx.storage
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    raw = storage.get_state("watchdog_restarts_24h") or "[]"
    try:
        timestamps = json.loads(raw)
        if not isinstance(timestamps, list):
            timestamps = []
    except (json.JSONDecodeError, TypeError):
        timestamps = []
    timestamps = [t for t in timestamps if datetime.fromisoformat(t) > cutoff]
    timestamps.append(now.isoformat(timespec="seconds"))
    storage.set_state("watchdog_restarts_24h", json.dumps(timestamps))

    n = len(timestamps)
    msg = f"BLE task(s) appear stuck: {symptom}"
    log.error(f"watchdog: {msg} — exiting for launcher restart (#{n} in 24h)")
    storage.log_event("watchdog_restart", "warn",
                      f"{msg} (#{n} of last 24h, email threshold {config.WATCHDOG_EMAIL_THRESHOLD})")

    if n < config.WATCHDOG_EMAIL_THRESHOLD:
        return

    last_email = storage.get_state("urgent_last_watchdog_excessive")
    if last_email:
        try:
            if now - datetime.fromisoformat(last_email) < timedelta(hours=6):
                return
        except ValueError:
            pass

    body = (
        f"Site: {config.SITE_NAME}\n\n"
        f"The BLE watchdog has restarted the monitor {n} times in the last 24 h\n"
        f"(threshold: {config.WATCHDOG_EMAIL_THRESHOLD}). The system self-heals\n"
        f"each time via the launcher respawn — telemetry is still being captured\n"
        f"between restarts.\n\n"
        f"ACTION REQUIRED: please have a site operator investigate the\n"
        f"Bluetooth adapter or BLE devices at the site.\n\n"
        f"Most recent symptom: {msg}\n\n"
        f"Restart timestamps (UTC) in the last 24 h:\n"
        + "\n".join(f"  {t}" for t in timestamps) + "\n\n"
        f"BMS sample threshold: {bms_threshold_s} s\n"
    )
    sent = mailer.send(
        f"[{config.SITE_NAME}] NOTICE: BLE watchdog elevated — {n} restarts in 24 h",
        body,
    )
    storage.set_state("urgent_last_watchdog_excessive", now.isoformat(timespec="seconds"))
    storage.log_event("alert_fired", "warn",
                      f"watchdog_excessive: sent={sent}; {n} restarts/24h")


# ---------- Housekeeping ----------

async def housekeeping_task(ctx: AppContext):
    """Apply DB retention/downsampling once a day. Runs at startup so any
    backlog gets compacted right away, then once every 24 h thereafter."""
    storage = ctx.storage
    while True:
        try:
            counts = storage.prune(config.RETENTION_RAW_DAYS, config.RETENTION_MINUTE_DAYS,
                                   config.RETENTION_MAX_DAYS)
            summary = ", ".join(f"{k}: -{v}" for k, v in counts.items())
            log.info(f"housekeeping: prune complete — {summary}")
            storage.log_event("housekeeping", "info", f"prune {summary}")
        except Exception as e:
            log.exception(f"housekeeping failed: {e}")
        await asyncio.sleep(24 * 3600)
