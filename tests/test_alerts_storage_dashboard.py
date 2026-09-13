import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from w4boc import alerts, config
from w4boc.alerts import MainsView
from w4boc.jbd import BasicInfo
from w4boc.storage import Storage


def _info(soc=100, v=13.78, i=0.0, temp=23.0, protections=None):
    return BasicInfo(
        pack_voltage=v, pack_current=i, residual_capacity=280 * soc / 100, nominal_capacity=280,
        cycle_count=3, production_date=None, balance_bitmap=0, protection_bitmap=0,
        protections=protections or [], sw_version=0x20, soc_percent=soc,
        charge_fet_on=True, discharge_fet_on=True, cell_count=4, temperatures_c=[temp],
    )


# ---------- storage ----------

def test_schema_upgrade_from_v1(tmp_path):
    """A v1 database (no mains_events table) opens cleanly and gains the table."""
    p = tmp_path / "v1.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE bms_samples (ts TEXT NOT NULL, pack_voltage REAL, pack_current REAL, soc_pct INTEGER,
            residual_ah REAL, cycle_count INTEGER, temp_c REAL, charge_fet INTEGER, discharge_fet INTEGER,
            cells_json TEXT, protections_json TEXT);
        CREATE TABLE charger_samples (ts TEXT NOT NULL, voltage REAL, current REAL, state TEXT, error TEXT);
        CREATE TABLE events (ts TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT);
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO state VALUES ('aprs_seq', '34');
    """)
    c.commit(); c.close()
    s = Storage(p)
    assert s.get_state("aprs_seq") == "34"
    s.write_mains_event("lost", "test", "high")
    assert s.latest_mains_event()["state"] == "lost"
    s.close()


def test_storage_roundtrip_and_prune(storage):
    storage.write_bms(_info(), [3.45, 3.449, 3.446, 3.446])
    storage.write_charger(13.8, 4.4, "STORAGE", "NO_ERROR")
    b = storage.latest_bms()
    assert b["soc_pct"] == 100 and b["cells"][0] == 3.45 and b["protections"] == []
    assert storage.latest_charger()["state"] == "STORAGE"
    assert storage.last_bms_seen() is not None and storage.last_charger_seen() is not None
    assert storage.recent_bms(5)[0]["ts"] == b["ts"]
    counts = storage.prune()
    assert counts == {"bms_samples": 0, "charger_samples": 0}
    assert storage.latest_bms() is not None


# ---------- alerts ----------

def test_mains_alert_fire_and_resolve(storage, no_email):
    storage.write_bms(_info(soc=96, i=-4.6), [3.4] * 4)
    since = datetime.now(timezone.utc) - timedelta(minutes=70)
    alerts.evaluate(storage, MainsView(lost=True, reason="charger silent 11m", since=since, confidence="high"))
    assert any("MAINS POWER LOST" in s for s, _ in no_email)
    assert storage.get_state("active_mains_lost")
    assert storage.get_state("mode") == "degraded"
    # same state again: no second email
    n = len(no_email)
    alerts.evaluate(storage, MainsView(lost=True, reason="charger silent 12m", since=since, confidence="high"))
    assert len(no_email) == n
    # resolved
    alerts.evaluate(storage, MainsView(lost=False, reason="charger advertising", since=datetime.now(timezone.utc)))
    assert any("mains power restored" in s for s, _ in no_email)
    assert not storage.get_state("active_mains_lost")


def test_soc_and_temp_alerts(storage, no_email):
    storage.write_bms(_info(soc=25, temp=40.0), [3.2] * 4)
    alerts.evaluate(storage, MainsView())
    subjects = [s for s, _ in no_email]
    assert any("SoC 25%" in s for s in subjects)
    assert any("battery 40.0 C" in s for s in subjects)
    assert storage.get_state("mode") == "degraded"


def test_body_mentions_mains(storage):
    storage.write_bms(_info(), [3.4] * 4)
    body = alerts._urgent_body(storage.latest_bms(), None,
                               MainsView(lost=True, reason="r", since=datetime.now(timezone.utc), confidence="high"))
    assert "Mains power: LOST" in body


# ---------- dashboard ----------

def _ctx(storage):
    from w4boc.context import AppContext
    from w4boc.mains import MainsTracker
    from w4boc.updater import Updater
    loop = asyncio.new_event_loop()
    ctx = AppContext(storage, loop, simulate=True)
    ctx.mains = MainsTracker(storage)
    ctx.monitoring_since = datetime.now(timezone.utc)
    ctx.updater = Updater("o/r", "2.0.0", storage.path.parent / "updates", storage.path.parent)
    from w4boc.simulate import SimState
    ctx.sim = SimState()
    return ctx


def test_dashboard_routes(storage):
    from w4boc.dashboard import create_app
    storage.write_bms(_info(), [3.45, 3.449, 3.446, 3.446])
    storage.write_charger(13.8, 4.4, "STORAGE", "NO_ERROR")
    storage.write_mains_event("lost", "test outage", "high")
    storage.write_mains_event("restored", "charger back", "high", 1800)
    storage.log_event("startup", "info", "test")
    ctx = _ctx(storage)
    app = create_app(ctx)
    app.testing = True
    c = app.test_client()

    for path in ("/", "/week", "/power", "/logs", "/partials/snapshot", "/partials/events",
                 "/partials/system", "/partials/mains_history", "/partials/tailscale"):
        r = c.get(path)
        assert r.status_code == 200, path

    snap = c.get("/partials/snapshot").data.decode()
    assert "Mains power" in snap and "13.78 V" in snap
    hist = c.get("/partials/mains_history").data.decode()
    assert "restored" in hist and "30m" in hist

    j = c.get("/api/chart/24h").get_json()
    assert j["bms"] and j["charger"] and j["mains"]
    j = c.get("/api/chart/7d").get_json()
    assert "bms_hourly" in j and "mains" in j
    j = c.get("/api/status").get_json()
    assert j["version"] and j["mains"]["state"] == "unknown"

    # client presence is noted by partial polls
    assert ctx.client_seen_ago() is not None and ctx.client_seen_ago() < 5

    # simulation toggles exist in simulate mode
    r = c.post("/api/sim/mains?on=0")
    assert r.status_code == 200 and ctx.sim.mains_on is False

    # restart button asks the loop to exit with code 2
    async def _run():
        r = c.post("/api/app/restart")
        assert r.status_code == 200
        await asyncio.sleep(0)
    ctx.loop.run_until_complete(_run())
    assert ctx.exit_code == 2 and ctx.stop_event.is_set()
    ctx.loop.close()


def test_base_template_has_duplicate_tab_guard(storage):
    from w4boc.dashboard import create_app
    ctx = _ctx(storage)
    app = create_app(ctx); app.testing = True
    html = app.test_client().get("/?autoopen=1").data.decode()
    assert "BroadcastChannel('w4boc-dashboard')" in html
    assert "window.close()" in html
    ctx.loop.close()


# ---------- browser open guard ----------

def test_browser_opens_only_without_live_tab(storage, monkeypatch):
    import webbrowser
    from w4boc import app as appmod, config
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(config, "DASH_BROWSER_CHECK_DELAY_S", 0)

    ctx = _ctx(storage)
    ctx.dashboard_url = "http://127.0.0.1:8080/"
    # a tab polled the server just now -> do not open another
    ctx.note_client()
    ctx.loop.run_until_complete(appmod.browser_task(ctx, True))
    assert opened == [] and ctx.browser_opened == "skipped"

    # nobody has called in -> open, tagged so the page can dedupe itself
    ctx2 = _ctx(storage)
    ctx2.dashboard_url = "http://127.0.0.1:8080/"
    ctx2.loop.run_until_complete(appmod.browser_task(ctx2, True))
    assert opened == ["http://127.0.0.1:8080/?autoopen=1"] and ctx2.browser_opened == "opened"

    ctx3 = _ctx(storage)
    ctx3.loop.run_until_complete(appmod.browser_task(ctx3, False))
    assert ctx3.browser_opened == "disabled" and len(opened) == 1
    for c in (ctx, ctx2, ctx3):
        c.loop.close()
