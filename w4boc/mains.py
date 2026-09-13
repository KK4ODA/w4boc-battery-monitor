"""Mains (AC) power detector.

Physical facts the detector relies on (measured on the W4BOC site data):

* The Victron charger is mains-powered. When AC drops it goes dark
  instantly — no BLE advertisements at all. While the monitor process is
  alive and AC is up, the charger has never been silent for more than
  3 minutes (it advertises ~1 Hz and we persist a sample at least once a
  minute).
* The BMS is powered by the battery, so it keeps answering during an
  outage. A fresh BMS sample therefore proves the Bluetooth stack is
  healthy, which is what separates "mains lost" from "BLE hiccup".
* Battery current is NOT a usable signal on its own: in STORAGE mode the
  charger routinely hands the load to the battery for 3-9 minutes at a
  time. It is only reported as supporting detail.
* If the PC sits on a USB-connected UPS, Windows reports AC loss instantly
  (see power.py). That input is authoritative when available.

Rules (all windows configurable in config.toml [mains]):

  UPS says offline                                         -> LOST (high)
  charger seen within `restore_minutes`                    -> ON   (high)
  charger silent >= `fast_minutes` AND BMS seen within
      `ble_alive_minutes` AND monitoring continuous for
      >= `fast_minutes` AND one "confirmation restart" of
      the process happened >= `fast_minutes` ago without
      the charger reappearing                              -> LOST (high)
  charger silent >= `slow_minutes` AND monitoring
      continuous for >= `slow_minutes`                     -> LOST (low: may
                                                              be a BLE failure)
  anything else                                            -> undecided, keep
                                                              previous state

"Monitoring continuous" is measured from the last heartbeat gap larger than
`HEARTBEAT_GAP_MAX`, so the quick watchdog restarts (~35 s) do not reset the
clock the way they did in v1.

The confirmation restart (done by monitor.sample_watchdog_task, recorded in
state key `charger_silence_restart_ts`) is what rules out the one failure
mode that looks exactly like an outage from inside the process: the BLE
advertisement scanner dying while the rest of the stack keeps working. A
restart brings a dead scanner back within seconds; a real outage survives it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config
from .storage import Storage, parse_ts

log = logging.getLogger(__name__)

STATE_ON = "on"
STATE_OFF = "off"
STATE_UNKNOWN = "unknown"

HEARTBEAT_GAP_MAX = timedelta(minutes=3)
PC_DOWN_MIN_GAP = timedelta(minutes=10)


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m:02d}m" if (h or d) else f"{m}m")
    return " ".join(parts)


@dataclass
class MainsInputs:
    now: datetime
    monitoring_since: datetime
    charger_last_seen: datetime | None
    bms_last_seen: datetime | None
    bms_current_a: float | None
    ac_line: str = "unknown"    # power.PowerStatus.ac_line
    confirm_restart_ts: datetime | None = None   # state 'charger_silence_restart_ts'


@dataclass
class Assessment:
    decided: bool
    lost: bool
    confidence: str     # "high" | "low" | "n/a"
    reason: str
    charger_silent_s: int | None = None

    @property
    def state(self) -> str:
        if not self.decided:
            return STATE_UNKNOWN
        return STATE_OFF if self.lost else STATE_ON


def _minutes(td: timedelta | None) -> float | None:
    return None if td is None else td.total_seconds() / 60.0


def assess(inp: MainsInputs, *,
           fast_min: int | None = None,
           ble_alive_min: int | None = None,
           slow_min: int | None = None,
           restore_min: int | None = None,
           discharge_a: float | None = None) -> Assessment:
    fast_min = config.MAINS_FAST_MINUTES if fast_min is None else fast_min
    ble_alive_min = config.MAINS_BLE_ALIVE_MINUTES if ble_alive_min is None else ble_alive_min
    slow_min = config.MAINS_SLOW_MINUTES if slow_min is None else slow_min
    restore_min = config.MAINS_RESTORE_MINUTES if restore_min is None else restore_min
    discharge_a = config.MAINS_DISCHARGE_CURRENT_A if discharge_a is None else discharge_a

    now = inp.now
    running = now - inp.monitoring_since
    charger_age = (now - inp.charger_last_seen) if inp.charger_last_seen else None
    bms_age = (now - inp.bms_last_seen) if inp.bms_last_seen else None

    # 1. UPS is authoritative when it has an opinion.
    if inp.ac_line == "offline":
        return Assessment(True, True, "high", "PC UPS reports AC power offline",
                          int(charger_age.total_seconds()) if charger_age else None)

    # 2. Charger advertising -> mains present.
    if charger_age is not None and charger_age <= timedelta(minutes=restore_min):
        return Assessment(True, False, "high",
                          f"charger advertising ({int(charger_age.total_seconds())}s ago)", 0)

    # 3. Charger silent. How long? If never seen, count from monitoring start.
    silent = charger_age if charger_age is not None else running
    silent_min = _minutes(silent)
    silent_s = int(silent.total_seconds())
    bms_alive = bms_age is not None and bms_age <= timedelta(minutes=ble_alive_min)

    if inp.bms_current_a is None:
        batt = "battery current unknown"
    elif inp.bms_current_a <= discharge_a:
        batt = f"battery discharging {inp.bms_current_a:+.1f} A"
    else:
        batt = f"battery current {inp.bms_current_a:+.1f} A"

    # The confirmation restart counts only if it happened after the charger
    # was last heard (i.e. it belongs to this silence period).
    restart_confirmed = (
        inp.confirm_restart_ts is not None
        and (inp.charger_last_seen is None or inp.confirm_restart_ts > inp.charger_last_seen)
        and (now - inp.confirm_restart_ts) >= timedelta(minutes=fast_min)
    )

    if (silent_min >= fast_min and bms_alive and restart_confirmed
            and running >= timedelta(minutes=fast_min)):
        return Assessment(True, True, "high",
                          f"charger silent {fmt_duration(silent_s)} while BMS still answering "
                          f"(survived a Bluetooth restart); {batt}",
                          silent_s)

    if silent_min >= slow_min and running >= timedelta(minutes=slow_min):
        if bms_alive:
            detail = "BMS answering but no Bluetooth restart confirmation"
        else:
            detail = "BMS silent too — mains lost or Bluetooth failure"
        return Assessment(True, True, "low",
                          f"charger silent {fmt_duration(silent_s)}; {detail}; {batt}",
                          silent_s)

    why = f"charger silent {fmt_duration(silent_s)}"
    if not bms_alive:
        why += ", BMS not answering"
    elif not restart_confirmed:
        why += ", awaiting Bluetooth restart confirmation"
    if running < timedelta(minutes=fast_min):
        why += f", monitoring only {fmt_duration(running.total_seconds())}"
    return Assessment(False, False, "n/a", why + " (waiting)", silent_s)


# ---------- persistent tracker ----------

class MainsTracker:
    """Owns the persisted mains state and turns assessments into transitions.

    State keys: mains_state ('on'|'off'|'unknown'), mains_since (ISO ts of the
    last transition), mains_reason.
    """

    def __init__(self, storage: Storage):
        self.s = storage
        self.state = storage.get_state("mains_state") or STATE_UNKNOWN
        self.since = storage.get_state_ts("mains_since")
        self.reason = storage.get_state("mains_reason") or ""
        self.last_assessment: Assessment | None = None
        self.last_outage_s: int | None = None

    @property
    def lost(self) -> bool:
        return self.state == STATE_OFF

    def _persist(self, since: datetime):
        self.s.set_state("mains_state", self.state)
        self.s.set_state("mains_since", since.isoformat(timespec="seconds"))
        self.s.set_state("mains_reason", self.reason)

    def tick(self, inp: MainsInputs) -> tuple[Assessment, str | None]:
        """Evaluate once. Returns (assessment, transition) where transition is
        'lost', 'restored' or None."""
        a = assess(inp)
        self.last_assessment = a
        if not a.decided:
            return a, None
        new = a.state
        if new == self.state:
            return a, None

        now = inp.now
        prev = self.state
        outage_s = None
        if new == STATE_OFF:
            # The outage began when the charger was last heard, not when we
            # finished confirming it; date the transition accordingly.
            start = now - timedelta(seconds=a.charger_silent_s) if a.charger_silent_s else now
            self.state, self.reason, self.since = STATE_OFF, a.reason, start
            self._persist(start)
            self.s.write_mains_event("lost", a.reason, a.confidence, ts=start)
            self.s.log_event("mains_lost", "urgent",
                             f"{a.reason} [{a.confidence} confidence]; outage began "
                             f"{fmt_duration((now - start).total_seconds())} ago")
            log.warning(f"MAINS LOST: {a.reason} [{a.confidence}]")
            return a, "lost"

        # new == STATE_ON
        if prev == STATE_OFF and self.since is not None:
            outage_s = int((now - self.since).total_seconds())
        self.state, self.reason = STATE_ON, a.reason
        self._persist(now)
        if prev == STATE_OFF:
            self.last_outage_s = outage_s
            self.s.write_mains_event("restored", a.reason, a.confidence, outage_s)
            self.s.log_event("mains_restored", "info",
                             f"{a.reason}; outage lasted {fmt_duration(outage_s)}")
            log.info(f"MAINS RESTORED after {fmt_duration(outage_s)}: {a.reason}")
            self.since = now
            return a, "restored"
        # unknown -> on: first observation, nothing to announce
        self.since = now
        self.s.log_event("mains_state", "info", f"mains ON ({a.reason})")
        return a, None

    def since_ts(self) -> datetime | None:
        return self.since

    def summary(self) -> dict:
        a = self.last_assessment
        return {
            "state": self.state,
            "since": self.since.isoformat(timespec="seconds") if self.since else None,
            "reason": self.reason,
            "assessment": a.reason if a else "",
            "confidence": a.confidence if a else "n/a",
        }


# ---------- monitoring continuity ----------

def continuity_start(storage: Storage, now: datetime) -> datetime:
    """Return the instant from which the monitor has been running without a
    heartbeat gap larger than HEARTBEAT_GAP_MAX. Persists `monitoring_since`.
    Call once at startup (the heartbeat task keeps `last_heartbeat` fresh)."""
    last_hb = storage.get_state_ts("last_heartbeat")
    since = storage.get_state_ts("monitoring_since")
    if last_hb is None or since is None or (now - last_hb) > HEARTBEAT_GAP_MAX:
        since = now
        storage.set_state("monitoring_since", since.isoformat(timespec="seconds"))
    return since


@dataclass
class DowntimeReport:
    gap_s: int
    last_heartbeat: datetime
    unexpected: bool | None
    detail: str

    @property
    def likely_power_loss(self) -> bool:
        return self.unexpected is True


def check_pc_downtime(storage: Storage, now: datetime,
                      shutdown_probe=None) -> DowntimeReport | None:
    """At startup: if the previous heartbeat is older than PC_DOWN_MIN_GAP the
    monitor (or the whole PC) was down. Records a 'pc_down' mains event and
    returns a report; None when there was no significant gap.

    `shutdown_probe` is power.last_shutdown_was_unexpected (injectable for
    tests). A boot that happened after the last heartbeat plus an
    "unexpected shutdown" event log entry is the signature of a mains
    outage that took the PC down with it."""
    last_hb = storage.get_state_ts("last_heartbeat")
    if last_hb is None:
        return None
    gap = now - last_hb
    if gap < PC_DOWN_MIN_GAP:
        return None
    unexpected, detail = (None, "not probed")
    if shutdown_probe is not None:
        try:
            unexpected, detail = shutdown_probe()
        except Exception as e:  # pragma: no cover - defensive
            unexpected, detail = None, f"probe failed: {e}"
    gap_s = int(gap.total_seconds())
    if unexpected is True:
        conf, label = "high", "PC lost power (unexpected shutdown)"
    elif unexpected is False:
        conf, label = "low", "monitor was stopped (clean shutdown/restart)"
    else:
        conf, label = "low", "monitor was not running (cause unknown)"
    reason = f"{label} for {fmt_duration(gap_s)}; {detail}"
    storage.write_mains_event("pc_down", reason, conf, gap_s)
    storage.log_event("pc_down", "warn" if unexpected is not False else "info", reason)
    return DowntimeReport(gap_s, last_hb, unexpected, reason)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
