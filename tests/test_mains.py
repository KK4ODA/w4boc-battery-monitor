from datetime import datetime, timedelta, timezone

from w4boc import mains
from w4boc.mains import MainsInputs, MainsTracker, assess

T0 = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
RULES = dict(fast_min=5, ble_alive_min=3, slow_min=45, restore_min=2, discharge_a=-1.0)


def inputs(now=T0, running_min=120, chg_age_min=0.5, bms_age_min=0.5, i=-0.0,
           ac="unknown", restart_min_ago=None, chg_never=False):
    return MainsInputs(
        now=now,
        monitoring_since=now - timedelta(minutes=running_min),
        charger_last_seen=None if chg_never else now - timedelta(minutes=chg_age_min),
        bms_last_seen=now - timedelta(minutes=bms_age_min),
        bms_current_a=i,
        ac_line=ac,
        confirm_restart_ts=None if restart_min_ago is None else now - timedelta(minutes=restart_min_ago),
    )


def test_charger_fresh_means_on():
    a = assess(inputs(), **RULES)
    assert a.decided and not a.lost and a.confidence == "high"


def test_ups_offline_is_authoritative():
    a = assess(inputs(ac="offline"), **RULES)
    assert a.decided and a.lost and "UPS" in a.reason


def test_short_silence_is_undecided():
    a = assess(inputs(chg_age_min=4), **RULES)
    assert not a.decided


def test_fast_rule_needs_confirmation_restart():
    # silent 6 min, BMS alive, but no restart yet -> waiting
    a = assess(inputs(chg_age_min=6), **RULES)
    assert not a.decided and "awaiting" in a.reason
    # restart happened 2 min ago (after charger last seen 6 min ago) -> still waiting
    a = assess(inputs(chg_age_min=6, restart_min_ago=2), **RULES)
    assert not a.decided
    # restart 5 min ago, charger silent 11 min, BMS alive -> LOST (high)
    a = assess(inputs(chg_age_min=11, restart_min_ago=5, i=-4.6), **RULES)
    assert a.decided and a.lost and a.confidence == "high"
    assert "discharging -4.6 A" in a.reason


def test_restart_before_last_charger_sample_does_not_count():
    # scanner died, restart fixed it, charger seen since; then a NEW silence
    a = assess(inputs(chg_age_min=7, restart_min_ago=30), **RULES)
    assert not a.decided


def test_fast_rule_needs_bms_alive():
    a = assess(inputs(chg_age_min=11, restart_min_ago=5, bms_age_min=10), **RULES)
    assert not a.decided and "BMS not answering" in a.reason


def test_slow_rule_when_bms_dark():
    a = assess(inputs(chg_age_min=46, bms_age_min=46), **RULES)
    assert a.decided and a.lost and a.confidence == "low"
    assert "Bluetooth failure" in a.reason


def test_slow_rule_when_bms_alive_but_no_launcher():
    a = assess(inputs(chg_age_min=46), **RULES)
    assert a.decided and a.lost and a.confidence == "low"
    assert "no Bluetooth restart confirmation" in a.reason


def test_monitoring_continuity_guard():
    # just started monitoring: even long silence is undecided
    a = assess(inputs(running_min=2, chg_age_min=60, bms_age_min=60), **RULES)
    assert not a.decided and "monitoring only" in a.reason


def test_charger_never_seen_counts_from_monitoring_start():
    a = assess(inputs(chg_never=True, running_min=50, bms_age_min=50), **RULES)
    assert a.decided and a.lost and a.confidence == "low"


def test_battery_current_alone_never_triggers():
    a = assess(inputs(i=-25.0), **RULES)      # heavy discharge but charger advertising
    assert a.decided and not a.lost


# ---- tracker ----

def test_tracker_transitions_and_outage_duration(storage):
    tr = MainsTracker(storage)
    assert tr.state == "unknown"
    a, t = tr.tick(inputs())
    assert t is None and tr.state == "on"            # first observation, silent
    # outage: restart confirmed, silent 11 min
    a, t = tr.tick(inputs(now=T0 + timedelta(minutes=11), chg_age_min=11, restart_min_ago=5, i=-4.6))
    assert t == "lost" and tr.lost
    ev = storage.latest_mains_event()
    assert ev["state"] == "lost" and ev["confidence"] == "high"
    # undecided ticks keep the state
    a, t = tr.tick(inputs(now=T0 + timedelta(minutes=12), chg_age_min=4))
    assert t is None and tr.lost
    # the 'lost' row is dated when the charger was last heard (T0)
    assert storage.mains_events_since(T0 - timedelta(days=1))[-1]["ts"].startswith("2026-09-13T12:00:00")
    assert tr.since == T0
    # charger back -> restored with the true duration (T0 .. T0+71m)
    a, t = tr.tick(inputs(now=T0 + timedelta(minutes=71)))
    assert t == "restored" and not tr.lost
    ev = storage.latest_mains_event()
    assert ev["state"] == "restored" and ev["outage_s"] == 71 * 60
    assert tr.last_outage_s == 71 * 60
    kinds = [e["kind"] for e in storage.recent_events(10)]
    assert "mains_lost" in kinds and "mains_restored" in kinds


def test_tracker_persists_across_instances(storage):
    tr = MainsTracker(storage)
    tr.tick(inputs())
    tr.tick(inputs(now=T0 + timedelta(minutes=11), chg_age_min=11, restart_min_ago=5))
    tr2 = MainsTracker(storage)
    assert tr2.lost and tr2.since is not None


# ---- continuity + PC downtime ----

def test_continuity_start_survives_short_gap(storage):
    since = mains.continuity_start(storage, T0)
    assert since == T0
    storage.set_state("last_heartbeat", (T0 + timedelta(minutes=30)).isoformat(timespec="seconds"))
    # restart 40 s after the last heartbeat -> same clock
    assert mains.continuity_start(storage, T0 + timedelta(minutes=30, seconds=40)) == T0
    # restart 10 min after -> clock resets
    later = T0 + timedelta(minutes=40)
    assert mains.continuity_start(storage, later) == later


def test_pc_downtime_report(storage):
    assert mains.check_pc_downtime(storage, T0) is None            # no heartbeat yet
    storage.set_state("last_heartbeat", (T0 - timedelta(minutes=5)).isoformat(timespec="seconds"))
    assert mains.check_pc_downtime(storage, T0) is None            # < 10 min gap
    storage.set_state("last_heartbeat", (T0 - timedelta(hours=2)).isoformat(timespec="seconds"))
    rep = mains.check_pc_downtime(storage, T0, shutdown_probe=lambda: (True, "event 6008"))
    assert rep is not None and rep.likely_power_loss and rep.gap_s == 7200
    ev = storage.latest_mains_event()
    assert ev["state"] == "pc_down" and ev["outage_s"] == 7200 and ev["confidence"] == "high"
    rep = mains.check_pc_downtime(storage, T0, shutdown_probe=lambda: (False, "event 1074"))
    assert rep.unexpected is False and not rep.likely_power_loss


def test_fmt_duration():
    assert mains.fmt_duration(45) == "45s"
    assert mains.fmt_duration(600) == "10m"
    assert mains.fmt_duration(3660) == "1h 01m"
    assert mains.fmt_duration(90000) == "1d 1h 00m"
    assert mains.fmt_duration(None) == "?"
