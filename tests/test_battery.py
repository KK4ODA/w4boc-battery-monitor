from datetime import datetime, timedelta, timezone

from w4boc import alerts, battery, config
from w4boc.alerts import MainsView
from w4boc.jbd import BasicInfo


def _info(soc=96, v=13.2, i=-4.6, residual=None):
    residual = 280 * soc / 100 if residual is None else residual
    return BasicInfo(
        pack_voltage=v, pack_current=i, residual_capacity=residual, nominal_capacity=280,
        cycle_count=3, production_date=None, balance_bitmap=0, protection_bitmap=0,
        protections=[], sw_version=0x20, soc_percent=soc,
        charge_fet_on=True, discharge_fet_on=True, cell_count=4, temperatures_c=[23.0],
    )


def _fill(storage, n, **kw):
    for _ in range(n):
        storage.write_bms(_info(**kw), [3.3] * 4)


# ---------- runtime estimate ----------

def test_runtime_estimate_when_discharging(storage):
    _fill(storage, 5, soc=96, i=-4.6)              # 268.8 Ah / 4.6 A = 58.4 h
    rt = battery.estimate_runtime(storage)
    assert rt is not None
    assert 58 < rt.hours < 59 and rt.avg_current_a == -4.6 and rt.samples == 5
    assert rt.text.startswith("≈ 2d 10h") and "4.6 A" in rt.text
    assert rt.short == "~58h"


def test_runtime_estimate_short_formats(storage):
    _fill(storage, 4, soc=2, i=-4.6, residual=15.0)   # 15/4.6 = 3.26 h
    rt = battery.estimate_runtime(storage)
    assert rt.short == "~3h15m"          # 3.26 h, minutes floored
    _fill(storage, 4, soc=1, i=-4.6, residual=2.0)    # avg over window still ~-4.6
    rt = battery.estimate_runtime(storage)
    assert rt.short.startswith("~") and rt.short.endswith("m")


def test_runtime_estimate_none_when_not_discharging(storage):
    _fill(storage, 5, soc=100, i=0.0)
    assert battery.estimate_runtime(storage) is None
    _fill(storage, 5, soc=100, i=+3.0)
    assert battery.estimate_runtime(storage) is None


def test_runtime_estimate_needs_samples_and_residual(storage):
    assert battery.estimate_runtime(storage) is None
    _fill(storage, 2, i=-4.6)
    assert battery.estimate_runtime(storage) is None          # < MIN_SAMPLES
    _fill(storage, 3, i=-4.6)
    assert battery.estimate_runtime(storage) is not None


# ---------- final warning ----------

def test_final_warning_fires_once_with_hysteresis(storage, no_email):
    # healthy: nothing
    _fill(storage, 4, soc=60, v=13.2, i=-4.6)
    st = alerts.evaluate(storage, MainsView(lost=True, reason="r", since=datetime.now(timezone.utc)))
    assert not storage.get_state("active_battery_final") and st == []
    # SoC hits the threshold -> URGENT with runtime + APRS status text
    _fill(storage, 4, soc=10, v=12.6, i=-4.6)
    st = alerts.evaluate(storage, MainsView(lost=True, reason="r", since=datetime.now(timezone.utc)))
    subj = [s for s, _ in no_email if "nearly exhausted" in s]
    assert len(subj) == 1 and "SoC 10%" in subj[0] and "12.60 V" in subj[0] and "left" in subj[0]
    assert "Estimated runtime" in no_email[-1][1]
    assert st == ["BATTERY LOW 12.60V 10% ~6h05m"]
    assert storage.get_state("active_battery_final")
    # in the hysteresis band (12 %): no resolve, no refire
    _fill(storage, 4, soc=12, v=12.7, i=-4.6)
    n = len(no_email)
    st = alerts.evaluate(storage, MainsView(lost=True, reason="r", since=datetime.now(timezone.utc)))
    assert len(no_email) == n and st == [] and storage.get_state("active_battery_final")
    # recovered: charging, SoC >= 15 and V > 12.3
    _fill(storage, 4, soc=16, v=13.3, i=+10.0)
    alerts.evaluate(storage, MainsView())
    assert any("battery recovered" in s for s, _ in no_email)
    assert not storage.get_state("active_battery_final")


def test_final_warning_voltage_path_only_while_discharging(storage, no_email):
    _fill(storage, 4, soc=40, v=11.9, i=+2.0)     # low voltage but charging -> no alarm
    alerts.evaluate(storage, MainsView())
    assert not storage.get_state("active_battery_final")
    _fill(storage, 4, soc=40, v=11.9, i=-4.6)
    alerts.evaluate(storage, MainsView())
    assert storage.get_state("active_battery_final")


def test_final_warning_aprs_status_can_be_disabled(storage, no_email, monkeypatch):
    monkeypatch.setattr(config, "BATTERY_FINAL_APRS", False)
    _fill(storage, 4, soc=5, v=12.4, i=-4.6)
    st = alerts.evaluate(storage, MainsView())
    assert st == [] and storage.get_state("active_battery_final")


def test_mains_lost_status_includes_runtime(storage):
    import asyncio
    from w4boc.context import AppContext
    from w4boc.mains import MainsTracker
    from w4boc.monitor import _status_text
    _fill(storage, 5, soc=96, v=13.2, i=-4.6)
    loop = asyncio.new_event_loop()
    ctx = AppContext(storage, loop, simulate=True)
    ctx.mains = MainsTracker(storage)
    assert _status_text(ctx, "lost") == "AC MAINS LOST 13.20V 96% -4.6A ~58h"
    loop.close()


def test_snapshot_shows_runtime_and_final_banner(storage):
    import asyncio
    from w4boc.context import AppContext
    from w4boc.dashboard import create_app
    from w4boc.mains import MainsTracker
    _fill(storage, 5, soc=9, v=12.5, i=-4.6)
    storage.set_state("active_battery_final", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    loop = asyncio.new_event_loop()
    ctx = AppContext(storage, loop, simulate=True)
    ctx.mains = MainsTracker(storage)
    app = create_app(ctx); app.testing = True
    html = app.test_client().get("/partials/snapshot").data.decode()
    assert "BATTERY NEARLY EXHAUSTED" in html and "left" in html
    j = app.test_client().get("/api/status").get_json()
    assert j["battery_final"] is True and j["runtime"]["hours"] > 5
    loop.close()
