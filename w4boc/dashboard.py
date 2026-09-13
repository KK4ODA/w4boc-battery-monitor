"""Web dashboard (Flask, served by waitress in a thread of the main process).

Reads the shared SQLite DB through its own read-only connections; the few
actions (restart, update, Tailscale serve, simulation toggles) go through the
AppContext so the asyncio side stays in charge.

Bound to 127.0.0.1:8080 by default. Expose to the tailnet with
`tailscale serve --bg 8080` (there is a button in the UI that does this).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import subprocess
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

from . import __version__, autostart, config, mailer
from . import settings as S
from .context import AppContext, EXIT_RESTART
from .mains import fmt_duration

log = logging.getLogger("dashboard")


# ---------- tiny helpers ----------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(dt_iso: str) -> datetime:
    dt = datetime.fromisoformat(dt_iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    return _parse(dt_iso).astimezone(config.TZ).strftime("%Y-%m-%d %H:%M:%S")


def _time_ago(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    s = int((_utcnow() - _parse(dt_iso)).total_seconds())
    if s < 60: return f"{s}s ago"
    if s < 3600: return f"{s // 60}m ago"
    if s < 86400: return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _db_at(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- Tailscale control ----------

def _ts_run(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["tailscale", *args], capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", "tailscale CLI not found in PATH"
    except subprocess.TimeoutExpired:
        return -2, "", "tailscale CLI timed out"


def _ts_status() -> dict:
    rc, out, err = _ts_run(["status", "--json"])
    if rc != 0:
        return {"ok": False, "error": (err or f"rc={rc}").strip()}
    try:
        d = json.loads(out)
        self_info = d.get("Self", {}) or {}
        return {
            "ok": True,
            "backend": d.get("BackendState", "Unknown"),
            "dns_name": (self_info.get("DNSName") or "").rstrip("."),
            "ips": self_info.get("TailscaleIPs", []) or [],
        }
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse: {e}"}


def _ts_serve_status(port: int) -> dict:
    rc, out, err = _ts_run(["serve", "status", "--json"])
    if rc != 0:
        return {"ok": False, "error": (err or f"rc={rc}").strip(), "enabled": False}
    try:
        d = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        d = {}
    enabled = False
    proxy_target = None
    web = d.get("Web") or {}
    for host_entry in web.values():
        for handler in (host_entry.get("Handlers") or {}).values():
            p = handler.get("Proxy", "")
            if p.endswith(f":{port}"):
                enabled = True
                proxy_target = p
    return {"ok": True, "enabled": enabled, "proxy": proxy_target}


# ---------- log tail ----------

def _tail(path, n: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except OSError:
        return []


# ---------- app factory ----------

def create_app(ctx: AppContext) -> Flask:
    app = Flask(__name__)
    port = config.DASH_PORT

    def _db():
        return _db_at(ctx.storage.path)

    @app.before_request
    def _note_client():
        # Any live page polls these; that is how we know a tab is open.
        if request.path.startswith(("/partials/", "/api/chart/")):
            ctx.note_client()

    @app.context_processor
    def _inject():
        return {"site": config.SITE_NAME, "version": __version__, "simulate": ctx.simulate}

    # ---------- pages ----------

    @app.route("/")
    def page_live():
        return render_template("live.html")

    @app.route("/week")
    def page_week():
        return render_template("week.html")

    @app.route("/power")
    def page_power():
        return render_template("power.html", cfg_fast=config.MAINS_FAST_MINUTES)

    # ---------- settings ----------

    def _settings_files():
        cfg = S.load_toml(config.CONFIG_PATH)
        sec = S.load_toml(config.SECRETS_PATH)
        return cfg, sec

    def _render_settings(values, extra, errors=None, saved=False, changed=None, notice=None):
        return render_template(
            "settings.html",
            sections=S.SECTIONS, order=S.SECTION_ORDER, fields_in=S.fields_in,
            values=values, errors=errors or {}, saved=saved, changed=changed or [],
            notice=notice, autostart=autostart.status(),
            config_path=str(config.CONFIG_PATH), secrets_path=str(config.SECRETS_PATH),
            needs_restart=any(S.field(*c.split(".", 1)).restart for c in (changed or [])),
        )

    @app.route("/settings")
    def page_settings():
        cfg, sec = _settings_files()
        values, extra = S.current_values(cfg, sec)
        return _render_settings(values, extra)

    @app.route("/api/settings/save", methods=["POST"])
    def api_settings_save():
        cfg, sec = _settings_files()
        current, extra = S.current_values(cfg, sec)
        form = request.form.to_dict()
        # The start-with-Windows switch is applied immediately elsewhere; keep it.
        form["site.start_with_windows"] = "1" if current.get("site.start_with_windows") else ""
        keep = {f.id: current.get(f.id, "") for f in S.SCHEMA if f.secret}
        values, errors = S.validate(form, secrets_keep=keep)
        if errors:
            log.warning(f"settings not saved: {len(errors)} invalid field(s)")
            return _render_settings(values, extra, errors=errors), 400
        changed = [f.id for f in S.SCHEMA if values.get(f.id) != current.get(f.id)]
        S.save(values, config.CONFIG_PATH, config.SECRETS_PATH, extra)
        shown = [c for c in changed if not S.field(*c.split(".", 1)).secret]
        hidden = len(changed) - len(shown)
        detail = ", ".join(shown) + (f" (+{hidden} secret)" if hidden else "")
        ctx.storage.log_event("settings_saved", "info", detail or "no changes")
        log.info(f"settings saved: {detail or 'no changes'}")
        return _render_settings(values, extra, saved=True, changed=changed)

    @app.route("/api/settings/test-email", methods=["POST"])
    def api_settings_test_email():
        cfg, sec = _settings_files()
        v, _ = S.current_values(cfg, sec)
        recipients = v.get("email.recipients") or []
        if not (v.get("email.sender") and v.get("email.app_password") and recipients):
            return render_template("partials/action.html",
                                   msg="Email is not configured (sender, app password and recipients "
                                       "must all be saved first).")
        ok = mailer.send(
            f"[{v.get('site.name')}] Test email from the dashboard",
            "If you are reading this, the saved SMTP settings work.\n",
            recipients=list(recipients),
            sender=str(v.get("email.sender")), password=str(v.get("email.app_password")),
            host=str(v.get("email.smtp_host")), port=int(v.get("email.smtp_port") or 587),
        )
        ctx.storage.log_event("manage", "info", f"test email sent={ok} to {', '.join(recipients)}")
        return render_template("partials/action.html",
                               msg=("Test email sent to " + ", ".join(recipients)) if ok else
                                   "Sending failed — see the log (Logs page) for the SMTP error.")

    @app.route("/api/autostart", methods=["POST"])
    def api_autostart():
        on = request.args.get("on", "1") not in ("0", "false")
        ok, msg = autostart.enable() if on else autostart.disable()
        if ok:
            cfg, sec = _settings_files()
            values, extra = S.current_values(cfg, sec)
            values["site.start_with_windows"] = on
            S.save(values, config.CONFIG_PATH, config.SECRETS_PATH, extra)
            ctx.storage.log_event("settings_saved", "info",
                                  f"start with Windows {'enabled' if on else 'disabled'}")
        else:
            log.warning(f"autostart change failed: {msg}")
        return render_template("partials/autostart.html", autostart=autostart.status(), msg=msg, ok=ok)

    @app.route("/partials/autostart")
    def partial_autostart():
        return render_template("partials/autostart.html", autostart=autostart.status())

    @app.route("/logs")
    def page_logs():
        n = max(20, min(2000, request.args.get("n", 300, type=int)))
        which = request.args.get("f", "monitor")
        fname = {"monitor": "monitor.log", "launcher": "launcher.log", "wrapper": "wrapper.log"}.get(which, "monitor.log")
        lines = _tail(config.LOG_DIR / fname, n)
        return render_template("logs.html", lines=lines, n=n, which=which)

    # ---------- HTMX partials ----------

    @app.route("/partials/snapshot")
    def partial_snapshot():
        with _db() as c:
            bms = c.execute("SELECT * FROM bms_samples ORDER BY ts DESC LIMIT 1").fetchone()
            chg = c.execute("SELECT * FROM charger_samples ORDER BY ts DESC LIMIT 1").fetchone()

        bms_d = dict(bms) if bms else None
        chg_d = dict(chg) if chg else None
        if bms_d:
            bms_d["age"] = _time_ago(bms_d["ts"])
            bms_d["ts_local"] = _local(bms_d["ts"])
            bms_d["cells"] = json.loads(bms_d.get("cells_json") or "[]")
            bms_d["protections"] = json.loads(bms_d.get("protections_json") or "[]")
            bms_d["power_w"] = (bms_d["pack_voltage"] or 0) * (bms_d["pack_current"] or 0)
            bms_d["spread_mv"] = int(round((max(bms_d["cells"]) - min(bms_d["cells"])) * 1000)) \
                if bms_d["cells"] else 0
            bbmap = bms_d.get("balance_bitmap") or 0
            bms_d["balancing"] = [bool(bbmap & (1 << i)) for i in range(len(bms_d["cells"]))]
            soc = bms_d["soc_pct"] or 0
            bms_d["soc_cls"] = "ok" if soc >= config.SOC_DEGRADED else \
                              "warn" if soc >= config.SOC_URGENT else "bad"
        if chg_d:
            chg_d["age"] = _time_ago(chg_d["ts"])
            chg_d["ts_local"] = _local(chg_d["ts"])

        m = ctx.mains.summary() if ctx.mains else {"state": "unknown"}
        m["since_local"] = _local(m.get("since"))
        m["since_ago"] = fmt_duration((_utcnow() - _parse(m["since"])).total_seconds()) if m.get("since") else "—"
        last = ctx.storage.latest_mains_event()
        if last and last["state"] in ("restored", "pc_down"):
            m["last_outage"] = f"{_local(last['ts'])} · {fmt_duration(last['outage_s'])}"
        elif last and last["state"] == "lost":
            m["last_outage"] = f"{_local(last['ts'])} · ongoing"
        else:
            m["last_outage"] = "none recorded"
        ps = ctx.power_status
        m["ups"] = (ps.source if ps else "n/a")

        return render_template(
            "partials/snapshot.html",
            bms=bms_d, chg=chg_d, mains=m, cfg=config,
        )

    @app.route("/partials/events")
    def partial_events():
        with _db() as c:
            rows = c.execute(
                "SELECT ts, kind, severity, detail FROM events ORDER BY ts DESC LIMIT 30"
            ).fetchall()
        events = [
            {"ts_local": _local(r["ts"]), "kind": r["kind"],
             "severity": r["severity"], "detail": r["detail"] or ""}
            for r in rows
        ]
        return render_template("partials/events.html", events=events)

    @app.route("/partials/tailscale")
    def partial_tailscale():
        return render_template(
            "partials/tailscale.html",
            ts=_ts_status(), serve=_ts_serve_status(port), port=port,
        )

    @app.route("/partials/system")
    def partial_system():
        upd = ctx.updater.snapshot() if ctx.updater else None
        restarts = []
        try:
            restarts = json.loads(ctx.storage.get_state("watchdog_restarts_24h") or "[]")
        except ValueError:
            pass
        sysinfo = {
            "version": __version__,
            "uptime": fmt_duration(ctx.uptime_s()),
            "started_local": ctx.started.astimezone(config.TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "restarts_24h": len(restarts),
            "email": config.EMAIL_ENABLED,
            "aprs": config.APRS_ENABLED,
            "aprs_call": config.APRS_CALLSIGN,
            "browser": ctx.browser_opened,
            "repo": config.UPDATER_REPO,
            "sim": ctx.sim.snapshot() if ctx.sim else None,
        }
        return render_template("partials/system.html", sysinfo=sysinfo, upd=upd,
                               auto_install=config.UPDATER_AUTO_INSTALL)

    @app.route("/partials/mains_history")
    def partial_mains_history():
        days = request.args.get("days", 90, type=int)
        since = _utcnow() - timedelta(days=days)
        rows = ctx.storage.mains_events_since(since, limit=200)
        events = []
        for r in rows:
            events.append({
                "ts_local": _local(r["ts"]),
                "state": r["state"],
                "reason": r["reason"] or "",
                "confidence": r["confidence"] or "",
                "outage": fmt_duration(r["outage_s"]) if r["outage_s"] is not None else "",
            })
        outages = [r for r in rows if r["state"] in ("restored", "pc_down") and r["outage_s"]]
        total_s = sum(r["outage_s"] for r in outages)
        longest = max((r["outage_s"] for r in outages), default=0)
        stats = {"days": days, "count": len(outages), "total": fmt_duration(total_s),
                 "longest": fmt_duration(longest)}
        return render_template("partials/mains_history.html", events=events, stats=stats)

    # ---------- actions ----------

    @app.route("/api/app/restart", methods=["POST"])
    def api_restart():
        ctx.storage.log_event("manage", "info", "restart requested from dashboard")
        ctx.request_exit(EXIT_RESTART, "dashboard restart button")
        return render_template("partials/action.html",
                               msg="Restarting… the launcher brings the monitor back in ~15 s. "
                                   "This page will reconnect by itself.")

    @app.route("/api/update/check", methods=["POST"])
    def api_update_check():
        if not ctx.updater:
            return render_template("partials/action.html", msg="Updater not available.")

        def _work():
            result = ctx.updater.check_and_stage()
            if result == "staged" and config.UPDATER_AUTO_INSTALL:
                from .app import install_update
                asyncio.run_coroutine_threadsafe(install_update(ctx), ctx.loop)
        threading.Thread(target=_work, daemon=True).start()
        return render_template("partials/action.html",
                               msg="Checking GitHub for a newer release… (status updates below)")

    @app.route("/api/update/install", methods=["POST"])
    def api_update_install():
        if not ctx.updater or not ctx.updater.state.staged:
            return render_template("partials/action.html", msg="Nothing staged to install.")
        from .app import install_update
        asyncio.run_coroutine_threadsafe(install_update(ctx), ctx.loop)
        return render_template("partials/action.html",
                               msg=f"Installing v{ctx.updater.state.staged} and restarting…")

    @app.route("/api/tailscale/serve/enable", methods=["POST"])
    def api_ts_enable():
        rc, out, err = _ts_run(["serve", "--bg", str(port)], timeout=60.0)
        log.info(f"tailscale serve enable: rc={rc} out={out!r} err={err!r}")
        return render_template(
            "partials/tailscale.html",
            ts=_ts_status(), serve=_ts_serve_status(port), port=port,
            last_action={"kind": "enable", "rc": rc, "out": out, "err": err},
        )

    @app.route("/api/tailscale/serve/disable", methods=["POST"])
    def api_ts_disable():
        rc, out, err = _ts_run(["serve", "reset"])
        log.info(f"tailscale serve disable: rc={rc} out={out!r} err={err!r}")
        return render_template(
            "partials/tailscale.html",
            ts=_ts_status(), serve=_ts_serve_status(port), port=port,
            last_action={"kind": "disable", "rc": rc, "out": out, "err": err},
        )

    if ctx.simulate:
        @app.route("/api/sim/mains", methods=["POST"])
        def api_sim_mains():
            on = request.args.get("on", "1") not in ("0", "false")
            ctx.sim.mains_on = on
            ctx.storage.log_event("manage", "info", f"SIMULATION: mains {'ON' if on else 'OFF'}")
            return render_template("partials/action.html",
                                   msg=f"Simulated mains is now {'ON' if on else 'OFF'}.")

        @app.route("/api/sim/ups", methods=["POST"])
        def api_sim_ups():
            ctx.sim.ac_line = request.args.get("ac", "unknown")
            return render_template("partials/action.html",
                                   msg=f"Simulated UPS AC line: {ctx.sim.ac_line}.")

        @app.route("/api/sim/scanner", methods=["POST"])
        def api_sim_scanner():
            ctx.sim.charger_ble = request.args.get("on", "1") not in ("0", "false")
            return render_template("partials/action.html",
                                   msg=f"Simulated charger BLE scanner {'alive' if ctx.sim.charger_ble else 'DEAD'}.")

    # ---------- chart JSON ----------

    @app.route("/api/chart/24h")
    def api_chart_24h():
        since_dt = _utcnow() - timedelta(hours=24)
        since = since_dt.isoformat(timespec="seconds")
        with _db() as c:
            bms = [
                {"t": r["ts"], "v": r["pack_voltage"], "i": r["pack_current"], "soc": r["soc_pct"]}
                for r in c.execute(
                    "SELECT ts, pack_voltage, pack_current, soc_pct FROM bms_samples "
                    "WHERE ts >= ? ORDER BY ts", (since,)
                )
            ]
            chg = [
                {"t": r["ts"], "v": r["voltage"], "i": r["current"], "state": r["state"]}
                for r in c.execute(
                    "SELECT ts, voltage, current, state FROM charger_samples "
                    "WHERE ts >= ? ORDER BY ts", (since,)
                )
            ]
        return jsonify({"bms": bms, "charger": chg, "mains": _mains_steps(ctx, since_dt)})

    @app.route("/api/chart/7d")
    def api_chart_7d():
        since_dt = _utcnow() - timedelta(days=7)
        since = since_dt.isoformat(timespec="seconds")
        with _db() as c:
            bms = [
                {
                    "t": r["hour"],
                    "v": r["v"], "i": r["i"],
                    "soc": r["soc_avg"],
                    "soc_min": r["soc_min"], "soc_max": r["soc_max"],
                    "temp": r["temp"],
                }
                for r in c.execute(
                    "SELECT strftime('%Y-%m-%dT%H:00:00+00:00', ts) AS hour,"
                    "       AVG(pack_voltage) AS v, AVG(pack_current) AS i,"
                    "       AVG(soc_pct) AS soc_avg, MIN(soc_pct) AS soc_min,"
                    "       MAX(soc_pct) AS soc_max, AVG(temp_c) AS temp"
                    " FROM bms_samples WHERE ts >= ? GROUP BY hour ORDER BY hour",
                    (since,),
                )
            ]
            row = c.execute(
                "SELECT MIN(pack_voltage) v_min, MAX(pack_voltage) v_max, AVG(pack_voltage) v_avg,"
                " MIN(soc_pct) s_min, MAX(soc_pct) s_max,"
                " MIN(temp_c) t_min, MAX(temp_c) t_max,"
                " MIN(cycle_count) c_start, MAX(cycle_count) c_end,"
                " COUNT(*) n"
                " FROM bms_samples WHERE ts >= ?",
                (since,),
            ).fetchone()
            stats = dict(row) if row else {}
            chg_states = dict(c.execute(
                "SELECT state, COUNT(*) n FROM charger_samples WHERE ts >= ? GROUP BY state",
                (since,),
            ).fetchall())
        return jsonify({"bms_hourly": bms, "stats": stats, "charger_states": chg_states,
                        "mains": _mains_steps(ctx, since_dt)})

    @app.route("/api/status")
    def api_status():
        """Machine-readable summary (handy for scripts / uptime checks)."""
        bms = ctx.storage.latest_bms()
        chg = ctx.storage.latest_charger()
        return jsonify({
            "version": __version__,
            "uptime_s": int(ctx.uptime_s()),
            "mains": ctx.mains.summary() if ctx.mains else None,
            "bms": bms, "charger": chg,
            "updater": ctx.updater.snapshot() if ctx.updater else None,
        })

    return app


def _mains_steps(ctx: AppContext, since: datetime) -> list[dict]:
    """Mains state as a step series (1 = on, 0 = off) for chart overlays."""
    events = list(reversed(ctx.storage.mains_events_since(since, limit=500)))
    # initial state at `since`: opposite of the first transition, else current
    if events:
        first = events[0]["state"]
        cur = 0 if first == "restored" else 1
    else:
        cur = 0 if (ctx.mains and ctx.mains.lost) else 1
    pts = [{"t": since.isoformat(timespec="seconds"), "on": cur}]
    for e in events:
        if e["state"] == "lost":
            cur = 0
        elif e["state"] == "restored":
            cur = 1
        elif e["state"] == "pc_down" and e["outage_s"]:
            start = (_parse(e["ts"]) - timedelta(seconds=e["outage_s"])).isoformat(timespec="seconds")
            pts.append({"t": start, "on": 0})
            cur = 1
        pts.append({"t": e["ts"], "on": cur})
    pts.append({"t": _utcnow().isoformat(timespec="seconds"), "on": cur})
    return pts
