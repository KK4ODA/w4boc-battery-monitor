"""Application wiring: one process, one asyncio loop, one dashboard thread.

    python main.py [--simulate] [--no-browser]

Exit codes (consumed by launcher.py):
    0 clean stop, 2 restart requested, 3 install staged update and restart,
    1 unexpected task failure (launcher restarts after a delay).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from . import APP_DIR, __version__, config, mailer, mains, power, singleton
from .aprs import aprs_task
from .context import AppContext, EXIT_UPDATE
from .monitor import (bms_task, charger_task, evaluator_task, heartbeat_task,
                      housekeeping_task, sample_watchdog_task, start_thread_watchdog)
from .storage import Storage
from .updater import Updater

log = logging.getLogger("app")


def _setup_logging():
    """Console (live window) + rotating file in logs/monitor.log."""
    config.LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(
        config.LOG_DIR / "monitor.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("waitress").setLevel(logging.WARNING)
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    logging.getLogger("bleak").setLevel(logging.WARNING)


# ---------- startup reports ----------

def _report_pc_downtime(ctx: AppContext):
    now = datetime.now(timezone.utc)
    probe = None if ctx.simulate else power.last_shutdown_was_unexpected
    rep = mains.check_pc_downtime(ctx.storage, now, shutdown_probe=probe)
    if rep is None:
        return
    log.warning(f"downtime detected: {rep.detail}")
    if rep.unexpected is False:
        return  # clean shutdown/restart — recorded as an event, no email
    subject = (f"[{config.SITE_NAME}] NOTICE: PC lost power for {mains.fmt_duration(rep.gap_s)}"
               if rep.likely_power_loss else
               f"[{config.SITE_NAME}] NOTICE: monitor was down for {mains.fmt_duration(rep.gap_s)}")
    body = (
        f"Site: {config.SITE_NAME}\n\n"
        f"The monitor is back after a gap of {mains.fmt_duration(rep.gap_s)}.\n"
        f"Last heartbeat before the gap: {rep.last_heartbeat.astimezone(config.TZ):%Y-%m-%d %H:%M %Z}\n"
        f"Restarted: {now.astimezone(config.TZ):%Y-%m-%d %H:%M %Z}\n\n"
        f"Assessment: {rep.detail}\n\n"
        + ("An unexpected shutdown recorded by Windows plus a gap this long is the\n"
           "signature of a mains outage that took the PC down with it. Check the\n"
           "dashboard's Power page and the battery SoC.\n"
           if rep.likely_power_loss else
           "Windows did not record an unexpected shutdown, so this may simply be\n"
           "the monitor having been stopped. Recorded on the dashboard's Power page.\n")
    )
    sent = mailer.send(subject, body)
    ctx.storage.log_event("alert_fired", "warn", f"pc_down: sent={sent}; {subject}")


def report_launcher_results(ctx: AppContext):
    """The launcher writes updates/last_update.json when a new version has
    proven itself and updates/last_rollback.json when it had to roll back.
    Turn each unreported file into an event + NOTICE email, once."""
    for name, kind in (("last_update.json", "update"), ("last_rollback.json", "rollback")):
        path = config.UPDATES_DIR / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("reported"):
            continue
        if kind == "update":
            subject = f"[{config.SITE_NAME}] NOTICE: monitor updated to v{data.get('version')}"
            body = (f"Site: {config.SITE_NAME}\n\nThe battery monitor updated itself from "
                    f"v{data.get('from_version')} to v{data.get('version')} and has been running "
                    f"normally since {data.get('verified_at')}.\n\nRelease notes: "
                    f"https://github.com/{config.UPDATER_REPO}/releases\n")
            ctx.storage.log_event("update_installed", "info",
                                  f"v{data.get('from_version')} -> v{data.get('version')} verified")
        else:
            subject = (f"[{config.SITE_NAME}] NOTICE: update to v{data.get('failed_version')} "
                       f"failed — rolled back to v{data.get('restored_version')}")
            body = (f"Site: {config.SITE_NAME}\n\nThe new version did not stay up after "
                    f"{data.get('attempts')} attempts, so the launcher restored "
                    f"v{data.get('restored_version')} ({data.get('restored_files')} files).\n"
                    f"The monitor is running on the previous version. Check logs/launcher.log "
                    f"and logs/monitor.log before publishing a fixed release.\n")
            ctx.storage.log_event("update_rolled_back", "warn",
                                  f"v{data.get('failed_version')} failed; restored "
                                  f"v{data.get('restored_version')}")
        data["reported"] = True
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass
        threading.Thread(target=mailer.send, args=(subject, body), daemon=True).start()


# ---------- updater ----------

async def install_update(ctx: AppContext, version: str | None = None) -> bool:
    upd: Updater = ctx.updater
    version = version or upd.state.staged
    if not version:
        return False
    ok = await ctx.loop.run_in_executor(None, upd.prepare_install, version)
    if ok:
        ctx.storage.log_event("update_install", "info",
                              f"installing v{version} (from v{ctx.version}); restarting")
        ctx.request_exit(EXIT_UPDATE, f"install update v{version}")
    else:
        ctx.storage.log_event("update_error", "warn",
                              f"install of v{version} aborted: {upd.state.last_error}")
    return ok


async def updater_task(ctx: AppContext):
    upd: Updater = ctx.updater
    if not config.UPDATER_ENABLED or upd is None:
        log.info("updater: disabled in config")
        return
    await asyncio.sleep(config.UPDATER_STARTUP_DELAY_S)
    while True:
        try:
            result = await ctx.loop.run_in_executor(None, upd.check_and_stage)
            if result == "staged":
                ctx.storage.log_event("update_available", "info", f"v{upd.state.staged} downloaded and verified")
                if config.UPDATER_AUTO_INSTALL:
                    await install_update(ctx)
        except Exception as e:
            log.exception(f"updater cycle failed: {e}")
        await asyncio.sleep(config.UPDATER_CHECK_INTERVAL_S)


async def launcher_results_task(ctx: AppContext):
    """Poll for the launcher's verification/rollback notes (cheap file stats)."""
    while True:
        try:
            report_launcher_results(ctx)
        except Exception as e:
            log.warning(f"launcher result report failed: {e}")
        await asyncio.sleep(30)


# ---------- browser ----------

async def browser_task(ctx: AppContext, enabled: bool):
    """Open the dashboard once at startup — unless a browser tab already has
    it open. An open tab polls /partials/* every few seconds, so if nobody
    has called within the check delay there is no live tab."""
    if not enabled:
        ctx.browser_opened = "disabled"
        return
    await asyncio.sleep(config.DASH_BROWSER_CHECK_DELAY_S)
    ago = ctx.client_seen_ago()
    if ago is not None and ago < 30:
        ctx.browser_opened = "skipped"
        log.info(f"dashboard already open in a browser (client seen {ago:.0f}s ago) — not opening another tab")
        return
    url = ctx.dashboard_url + "?autoopen=1"
    try:
        ok = await ctx.loop.run_in_executor(None, webbrowser.open, url)
    except Exception as e:
        ok = False
        log.warning(f"browser open failed: {e}")
    ctx.browser_opened = "opened" if ok else "failed"
    log.info(f"opened dashboard in browser: {url}" if ok else "could not open a browser")


# ---------- main ----------

async def _run_tasks(ctx: AppContext, args) -> int:
    if ctx.simulate:
        from .simulate import sim_bms_task, sim_charger_task
        producers = [sim_bms_task(ctx), sim_charger_task(ctx)]
    else:
        producers = [bms_task(ctx), charger_task(ctx)]

    coros = {
        "bms": producers[0],
        "charger": producers[1],
        "evaluator": evaluator_task(ctx),
        "housekeeping": housekeeping_task(ctx),
        "heartbeat": heartbeat_task(ctx),
        "watchdog": sample_watchdog_task(ctx),
        "updater": updater_task(ctx),
        "launcher_results": launcher_results_task(ctx),
        "browser": browser_task(ctx, config.DASH_OPEN_BROWSER and not getattr(args, "no_browser", False)),
    }
    if config.APRS_ENABLED:
        coros["aprs"] = aprs_task(ctx.storage, ctx.aprs_bus)

    tasks = {name: ctx.loop.create_task(c, name=name) for name, c in coros.items()}
    # Tasks that are allowed to finish on their own.
    may_finish = {"browser", "updater", "watchdog"}
    stop = ctx.loop.create_task(ctx.stop_event.wait(), name="stop")

    pending = set(tasks.values()) | {stop}
    while True:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if stop in done:
            break
        for t in done:
            name = t.get_name()
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                log.error(f"task '{name}' crashed: {type(exc).__name__}: {exc}",
                          exc_info=exc)
                ctx.storage.log_event("task_crash", "warn", f"{name}: {type(exc).__name__}: {exc}")
                ctx.request_exit(1, f"task '{name}' crashed")
            elif name not in may_finish:
                log.error(f"task '{name}' ended unexpectedly")
                ctx.request_exit(1, f"task '{name}' ended")
        if ctx.stop_event.is_set():
            break

    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return ctx.exit_code


def run(args) -> int:
    _setup_logging()
    simulate = bool(getattr(args, "simulate", False)) or config.SIMULATE
    # Singleton lock — exits with a clear message if another instance is running.
    lock = singleton.acquire("monitor", 50001)

    storage = Storage()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ctx = AppContext(storage, loop, simulate=simulate)
    ctx.dashboard_url = f"http://{config.DASH_HOST}:{config.DASH_PORT}/"
    now = datetime.now(timezone.utc)

    storage.log_event(
        "startup", "info",
        f"monitor v{__version__} started; email={'enabled' if config.EMAIL_ENABLED else 'DISABLED'}"
        f"{'; SIMULATION' if simulate else ''}",
    )
    log.info(
        f"W4BOC Battery Monitor v{__version__} starting"
        f"{' in SIMULATION mode' if simulate else ''}. "
        f"Email {'ENABLED' if config.EMAIL_ENABLED else 'DISABLED (app password blank)'}. "
        f"DB: {config.DB_PATH}"
    )

    # Mains detector state + continuity clock, then the "was the PC down?" check
    # (it must run before the heartbeat task overwrites last_heartbeat).
    ctx.monitoring_since = mains.continuity_start(storage, now)
    _report_pc_downtime(ctx)
    ctx.mains = mains.MainsTracker(storage)
    ctx.updater = Updater(config.UPDATER_REPO, __version__, config.UPDATES_DIR, APP_DIR,
                          token=config.UPDATER_TOKEN)
    if simulate:
        from .simulate import SimState
        ctx.sim = SimState(storage)

    # Dashboard: bind in this thread (fail fast on a busy port), serve in a thread.
    from .dashboard import create_app
    from waitress import create_server
    flask_app = create_app(ctx)
    server = create_server(flask_app, host=config.DASH_HOST, port=config.DASH_PORT, threads=8)
    threading.Thread(target=server.run, name="dashboard", daemon=True).start()
    log.info(f"dashboard on {ctx.dashboard_url}")

    start_thread_watchdog(ctx)

    try:
        code = loop.run_until_complete(_run_tasks(ctx, args))
    except KeyboardInterrupt:
        log.info("stopped by Ctrl-C")
        code = 0
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        storage.log_event("shutdown", "info", f"exit code {ctx.exit_code} ({ctx.exit_reason or 'stop'})")
        storage.close()
        lock.close()
    log.info(f"exiting with code {code} ({ctx.exit_reason or 'stop'})")
    return code
