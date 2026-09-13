# W4BOC Battery Monitor — Operation Manual (v2)

A Windows application that monitors the W4BOC repeater's LiFePO4 battery and
Victron charger via Bluetooth, detects mains power loss, exposes a local web
dashboard, sends email alerts, broadcasts telemetry on APRS (RF + APRS-IS)
and keeps itself updated from GitHub.

---

## 1. What it does

- Polls the **JBD/Xiaoxiang BMS** inside the LiFePO4 pack once a minute
  (pack voltage, current, SoC, cell voltages, temperature, protections, FETs).
- Listens passively for the **Victron Blue Smart IP22** charger's BLE
  "Instant Readout" advertisement (state, output voltage/current, error).
- Decides whether **mains power** is present (§4.4) and records every outage.
- Stores everything in a local SQLite database (`monitor.db`).
- Serves a **web dashboard** on `http://localhost:8080` (§4.1).
- Sends **email alerts** (§4.3) and, while degraded, a daily digest.
- Broadcasts **APRS telemetry** every few minutes via soundmodem (RF) and
  APRS-IS (§4.5).
- Runs under a **supervisor** that restarts it after crashes or freezes and
  installs **updates** from GitHub Releases (§6.3).

---

## 2. Hardware on the site PC

| Device | Connection | Notes |
| --- | --- | --- |
| LiFePO4 pack with JBD BMS | Bluetooth LE (MAC in `config.toml`) | One GATT client at a time — the phone app blocks us and vice versa |
| Victron Blue Smart IP22 charger | Bluetooth LE advertisement (MAC + key in config/secrets) | Passive listen — VictronConnect on a phone can run simultaneously |
| Repeater radio | Audio + PTT to soundmodem | soundmodem owns PTT, levels, timing |
| (optional) UPS with USB to the PC | Windows power status | Gives instant mains-loss detection |

---

## 3. Software components

| Process | Role | Started by | Port |
| --- | --- | --- | --- |
| `run.bat` → `launcher.py` | Supervisor: restart, heartbeat kill, update install/rollback | Startup folder shortcut | — |
| `main.py` (child of the launcher) | BLE polling, DB, mains detector, alerts, APRS, dashboard | launcher | 8080 (localhost), 50001 (instance lock) |
| **soundmodem** | AFSK modem to/from radio | autostart | 8000 (AGW) |
| **YAAC** | RF digipeater + iGate for *other* stations | autostart | — |
| **Tailscale** | Remote access | service | — |

The monitor talks to soundmodem **directly** on port 8000 (AGWPE), not
through YAAC. APRS-IS uplink is also direct.

---

## 4. Day-to-day

### 4.1 The dashboard

- On the PC: `http://localhost:8080`. Remote: via Tailscale (§7).
- **Live** — mains power, SoC, pack, charger, last-seen ages, cells, FETs,
  24 h chart (with red shading while mains was lost), recent events, the
  *Monitor* card (version, uptime, BLE restarts in 24 h, update status,
  buttons) and the Tailscale card.
- **Last 7 days** — hourly charts and summary stats.
- **Power** — outage history: count, total time without mains, longest
  outage, and every transition with its reason and confidence.
- **Logs** — tail of `monitor.log` / `launcher.log` without RDP.
- **Settings** — every setting, editable in the browser (§6.7), plus the
  *Start with Windows* switch.

The app opens the dashboard in the default browser at startup **only if no
browser tab already has it open** (an open tab polls the server every few
seconds). A tab that the app opened closes itself if it later finds an
older live tab, so session restore after the nightly reboot no longer
accumulates tabs.

### 4.2 What healthy looks like

| Reading | Healthy value |
| --- | --- |
| Mains power | **ON**, "charger advertising (Ns ago)" |
| Pack voltage | ~13.78 V resting (up to ~14.4 V in ABSORPTION) |
| Pack current | ~0 A (charger feeds the load); short negative blips of a few minutes are normal in STORAGE |
| SoC | 100 % |
| Cell spread | < 10 mV |
| Charger | STORAGE, ~13.8 V @ 4–5 A, NO_ERROR |
| Last seen | BMS < 2 min, charger < 1 min |
| Monitor card | BLE restarts (24 h) ≤ 8, updater *up to date* |

### 4.3 Alerts (email)

Subject prefixes: **URGENT** (act now), **NOTICE** (worth knowing),
**RESOLVED** (an URGENT condition cleared).

| Subject | Trigger |
| --- | --- |
| `URGENT: MAINS POWER LOST — …` | Mains detector declared an outage (§4.4). The reason and confidence are in the subject/body. |
| `RESOLVED: mains power restored after …` | Charger advertising again. |
| `NOTICE: PC lost power for …` | The monitor came back after a gap ≥ 10 min and Windows logged an unexpected shutdown — the PC itself lost power. |
| `NOTICE: monitor was down for …` | Same gap, but no unexpected-shutdown record (cause unknown). |
| `URGENT: SoC X%, below 30%` | SoC below `soc_urgent_pct` |
| `URGENT: SoC dropped X% in last hour` | Rapid discharge |
| `URGENT: BMS protection: …` | BMS tripped a protection |
| `URGENT: charger error: …` | Victron reports an error |
| `URGENT: battery X C` | Temp > 35 °C or < 0 °C |
| `URGENT: battery nearly exhausted — SoC 9%, 12.31 V, ≈ 4h 10m left; monitoring stops when the BMS cuts off` | SoC ≤ `battery_final_soc_pct` (10) or, while discharging, pack ≤ `battery_final_voltage_v` (12.0 V). Sent once; clears 5 points / 0.3 V higher. Also an APRS status `>BATTERY LOW 12.31V 9% ~4h10m`. |
| `RESOLVED: battery recovered — SoC 16%, 13.30 V` | The above cleared (charging again). |
| `NOTICE: BLE watchdog elevated — N restarts in 24 h` | ≥ `watchdog_email_threshold` BMS-silence restarts in 24 h |
| `NOTICE: monitor updated to vX.Y.Z` | Auto-update installed and verified |
| `NOTICE: update to vX failed — rolled back` | New version did not stay up; previous restored |

Mode tracking: SoC < 50 % or mains lost → **DEGRADED** (daily digest at
08:00 local). Back to NORMAL after the cause has been clear for
`mode_recovery_minutes` (60) and SoC ≥ 80 %.

Rate limiting and the active/resolved flags are per trigger; `manage.py
reset-alerts` re-arms everything (§6.5).

### 4.4 Mains power detection — how it works

Facts it relies on (measured on this site's data):

- The Victron charger is mains-powered: when AC drops it stops advertising
  **immediately**. While the monitor is alive and AC is up, the charger has
  never been silent for more than 3 minutes.
- The BMS is battery-powered and keeps answering during an outage. A fresh
  BMS sample proves Bluetooth itself is healthy.
- Battery current is *not* used as a trigger: in STORAGE mode the charger
  hands the load to the battery for 3–9 minutes several times a day.

Sequence during a real outage (default settings):

1. T+0 — charger goes silent.
2. T+5 min — the watchdog performs **one** "confirmation restart" of the
   monitor. If the Bluetooth advertisement scanner had merely died, the
   restart revives it and the charger reappears within seconds (no alert).
3. T+10–11 min — still silent, BMS still answering → **MAINS LOST** (high
   confidence): URGENT email, DEGRADED mode, APRS status packet + immediate
   position/telemetry frame, ` MAINS LOST` in the position comment, red
   *Mains power* card. The outage is dated from T+0.
4. While it lasts: the email, the daily digest, the Mains card and
   `/api/status` carry an **estimated runtime** — residual Ah from the BMS
   divided by the average draw over the last 10 minutes (e.g. "≈ 2d 8h at
   4.6 A"). The monitor PC is powered from the same battery, so this is
   also how long monitoring continues. `URGENT: SoC …, below 30%` follows
   in due course, then the **final warning** (SoC ≤ 10 % or ≤ 12.0 V) —
   the last email you will get before the BMS opens its discharge FET and
   the PC goes dark with the repeater.
5. Charger advertises again → **RESTORED**: RESOLVED email with the outage
   duration, APRS status packet, history row on the Power page. If the PC
   had died, it boots when mains returns and additionally sends
   `NOTICE: PC lost power for …`.

Other paths:

- **UPS**: if Windows reports a system battery (USB-connected UPS) and AC
  offline, the outage is declared instantly.
- **BMS also silent** (Bluetooth adapter dead?) → after 45 min
  (`slow_minutes`) a *low-confidence* MAINS LOST is raised: "mains lost or
  Bluetooth failure".
- **PC down**: at startup, if the previous heartbeat is more than 10 minutes
  old, a `pc_down` row is recorded with the gap; the Windows event log decides
  whether it was a power loss (event 6008/41) or a clean shutdown (1074).

All windows are in `config.toml [mains]`.

### 4.5 APRS telemetry on aprs.fi

Station page: <https://aprs.fi/info/a/W4BOC-1> (Telemetry tab). Formats are
unchanged from v1 and pinned by tests.

| Ch | Name | Unit | Meaning |
| --- | --- | --- | --- |
| 1 | Vbat | Vdc | Pack voltage (BMS) |
| 2 | Ibat | Adc | Pack current (BMS), signed |
| 3 | SoC | Pct | State of charge |
| 4 | Tbat | degC | Battery temperature |
| 5 | IchgO | Adc | Charger output current (Victron) |

Bits (1 = healthy): ChgFET, DisFET, **Mains** (charger heard in the last
5 min and no outage declared), BMSok, ProtOK, ChgOK, Ok50, Ok30.

On a mains transition the station also sends a **status** packet
(`>AC MAINS LOST 13.42V 96% -4.6A` / `>AC MAINS RESTORED after 1h 23m …`),
shown as "Status" on the aprs.fi station page.

---

## 5. Files & data

| Path | Purpose |
| --- | --- |
| `run.bat` | Launcher wrapper (never auto-updated) |
| `launcher.py` | Supervisor (never auto-updated) |
| `main.py`, `w4boc/` | The application |
| `config.toml` | Site settings. Safe to share. Never overwritten by updates. |
| `secrets.toml` | Victron key, Gmail app password, optional GitHub token. Stays on the PC. |
| `monitor.db` | SQLite (WAL). Samples, events, mains history, state. |
| `logs/monitor.log` | App log (5 MB × 5). `logs/launcher.log`: supervisor. `logs/heartbeat.json`: liveness. |
| `updates/` | Downloaded/staged releases, backup of the previous version, install/verify markers |
| `manage.py`, `tools/` | Operator utilities (§6) |

---

## 6. Common operations

### 6.1 Restart the monitor

Dashboard → *Monitor* card → **Restart monitor** (works remotely). Or close
the console window and double-click `run.bat`. Config changes need a restart.

### 6.2 Reading the log

```
INFO monitor: BMS: 13.78 V +0.00 A SoC 100% cells=[3.448, 3.446, 3.446, 3.444]
INFO monitor: Charger: STORAGE 13.8 V @ 4.4 A err=NO_ERROR
INFO w4boc.aprs: APRS data: rf=True is=True  T#081,138,128,098,082,024,11111111
WARNING monitor: watchdog: charger silent 312s while BMS answers — one confirmation restart …
WARNING w4boc.mains: MAINS LOST: charger silent 11m while BMS still answering … [high]
```

`rf=False` → soundmodem AGW unreachable. `is=False` → no Internet (RF still
works). `launcher.log` shows restarts, "heartbeat stale … killing child",
update installs and rollbacks.

### 6.3 Updates

Automatic: every 6 h the monitor checks GitHub Releases; a newer release is
downloaded, SHA-256-verified, installed and the monitor restarts (~20 s).
After 3 minutes of healthy running it is "verified" and a NOTICE email is
sent. If it fails to stay up three times, the launcher restores the previous
version and emails a NOTICE.

Manual: dashboard → **Check for updates** / **Install vX.Y.Z**, or
`python manage.py check-update`. Set `[updater] auto_install = false` to
require the button.

### 6.4 Reboot recovery

Auto-login → Startup shortcut → `run.bat` → launcher → monitor. Within ~60 s
the dashboard is reachable and APRS resumes. On the first start after a gap
the monitor also decides whether the PC lost power (§4.4).

### 6.5 `manage.py`

```
python manage.py status           mode, mains state, active alerts, rate-limits
python manage.py mains            mains state + outage history (--days N)
python manage.py reset-alerts     re-arm every alert
python manage.py reset-alert mains_lost
python manage.py reset-mode       force NORMAL
python manage.py force-digest
python manage.py check-update
```

### 6.6 Diagnostics (stop the monitor first for the BMS ones)

```
python tools\scan.py 20           BLE devices nearby
python tools\bms_read.py          one BMS snapshot
python tools\victron_read.py      one charger advertisement, decoded
python tools\enum_gatt.py         BMS GATT table
python tools\mailer_test.py       send a test email
```

### 6.7 Settings page

Dashboard → **Settings**. Sections: Site, Battery BMS, Charger, Email
alerts, APRS telemetry, Mains power detection, Alert thresholds, Dashboard &
startup, Updates. Every field has its meaning and limits next to it.

- **Save settings** validates everything (MAC/email/callsign formats,
  ranges, cross-checks such as urgent < degraded SoC) and only then rewrites
  `config.toml` and `secrets.toml`, keeping the previous copies as
  `config.toml.bak` / `secrets.toml.bak` and preserving any keys it does not
  know about. Nothing is written if a field is invalid.
- Secrets (Victron key, Gmail app password, GitHub token) are shown as
  "(set)" only; leave a secret blank to keep it, tick *clear* to remove it.
- Most changes need a restart — the green banner offers **Restart now**.
- **Send test email** uses the values saved on disk, so save first.
- **Start with Windows** (top of the page) creates or removes the Startup
  shortcut immediately and records the choice in `[site]
  start_with_windows`; when it is on, a missing shortcut is recreated at
  the next start. Command line: `python -m w4boc.autostart enable|disable|status`.

The page is served by the dashboard, so it is reachable from the tailnet
when `tailscale serve` is on — anyone who can open the dashboard can change
settings. Keep the tailnet invitation list short.

---

## 7. Remote access via Tailscale

Unchanged from v1. The dashboard binds to localhost; `tailscale serve --bg
8080` (button on the Live page) exposes it as `https://<machine>.<tailnet>.ts.net/`
— the `https://` prefix is required. RDP to the same hostname for full
access.

---

## 8. Configuration reference

Use the Settings page (§6.7) or edit `config.toml` by hand — the file is
fully commented (`config.example.toml` shows every key with its default).
Sections: `[site]` (name, time zone, sampling, retention, lock port, start
with Windows), `[bms]`, `[victron]`, `[email]` (sender, recipients, SMTP),
`[aprs]` (callsign, AGW/APRS-IS, position, symbol, cadence, tocall, project
name, comment prefix), `[mains]`, `[alerts]`, `[dashboard]`, `[updater]`.
Secrets: `secrets.toml` (`[victron] encryption_key`, `[email] app_password`,
`[updater] github_token`).

---

## 9. Known behaviour

- BMS polls fail transiently several times a day ("BMS not discoverable",
  "OSError … canceled") and the watchdog restarts the monitor a few times a
  day on BMS silence. Both are normal Windows BLE noise; only an elevated
  rate (≥ 12/24 h) emails.
- The first `mains_lost` declaration after an outage takes ~11 min by design
  (one restart to rule out a dead scanner). A UPS on the PC makes it instant.
- `monitor.db` is pruned daily (raw 7 d, per-minute 30 d, hourly 1 y).
