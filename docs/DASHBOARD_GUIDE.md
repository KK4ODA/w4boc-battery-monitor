# W4BOC Battery Monitor — Dashboard Viewer's Guide (v2)

For **looking at** the dashboard. Running the monitor is covered in
`OPERATION_MANUAL.md`.

## 1. Getting there

On the site PC: `http://localhost:8080/`. From anywhere else: join the W4BOC
tailnet (a site operator sends the Tailscale invite; install Tailscale on your
phone/laptop and sign in) and open the `https://…ts.net/` address the operator
gives you — the `https://` prefix is required.

The dashboard is read-only except for the operator buttons in the *Monitor*
and *Tailscale* cards (restart, update, tailnet exposure). Viewers can ignore
those.

## 2. Pages

| Page | What it is for |
| --- | --- |
| **Live** | Right-now state, 24 h chart, recent events, monitor health |
| **Last 7 days** | Hourly charts and stats |
| **Power** | Mains outage history and totals |
| **Logs** | Tail of the application log |

Live refreshes itself every few seconds. If the monitor restarts (it does so
a few times a day for Bluetooth hygiene, and after updates) a red
"Monitor not responding — reconnecting" banner shows for ~20 s and then
disappears by itself.

## 3. The Live page

### 3.1 Mains power card (first card)

| Shows | Meaning |
| --- | --- |
| **ON** for 3d 4h · last outage: 2026-09-01 … · 1h 23m | Normal. The small grey line is the detector's current reasoning ("charger advertising (12s ago)"). |
| **LOST** for 14m · since 2026-09-13 04:22 | Outage in progress, dated from when the charger went silent. The card turns red and a MAINS LOST badge appears under *Last seen*. The email has already gone out. |
| **unknown** | Just started; waiting for the first charger sample. |

Note the ~11 minute delay between a real outage and the LOST verdict — the
monitor deliberately restarts its Bluetooth stack once before declaring an
outage, so a stuck scanner never produces a false alarm.

### 3.2 Battery / charger cards

| Field | Healthy |
| --- | --- |
| State of charge | 95–100 %; amber < 50 %, red < 30 % |
| Pack | ~13.7–13.8 V, ~0 A (negative for a few minutes at a time is normal; sustained negative = battery is carrying the load) |
| Charger | STORAGE, ~13.8 V @ 4–5 A, no error badge |
| Last seen | BMS < 2 min, charger < 1 min, UPS: "no UPS/battery reported" unless the PC has one |
| Cells | four values within ~10 mV; spread shown below |
| State | both FETs ON, no protections |

Why "pack 0 A" with "charger 4.5 A out" is correct: the charger feeds the
repeater directly and the full battery takes nothing.

### 3.3 Last 24 hours chart

SoC (green dashed), battery V (blue), battery A (amber), optionally the
charger's V/A, and a **red shaded band** wherever mains was lost. Toggle
series with the checkboxes.

### 3.4 Recent events

Last 30 events. Common kinds: `startup`, `aprs_sent`, `mains_lost`,
`mains_restored`, `pc_down`, `alert_fired`, `alert_resolved`, `mode_change`,
`watchdog_restart`, `update_available`, `update_installed`, `housekeeping`,
`bms_error` / `bms_offline` (transient is normal), `charger_state_change`,
`manage`.

### 3.5 Monitor card

Version, uptime, BLE restarts in the last 24 h (≤ 8 is normal), email/APRS
status, and the updater: latest release, status, last check. Operators use
*Check for updates*, *Install*, *Restart monitor* here.

## 4. Power page

Three numbers for the last 90 days — outages, total time without mains,
longest outage — and the full list of transitions: **mains lost** (with the
detector's reason and confidence), **restored** (with the duration) and
**PC down** (the monitor PC itself was off; the reason says whether Windows
recorded a power loss).

## 5. Quick triage

1. Mains ON, SoC ≥ 95 %, both FETs ON, no protections, charger STORAGE
   NO_ERROR, ages small → **all good**.
2. Mains **LOST** → outage in progress. Watch SoC on the 24 h chart; at the
   repeater's ~5 A the 280 Ah pack lasts well over a day.
3. Mains ON but SoC low or pack current stays negative → charger problem
   (state/error) rather than mains — check the charger card.
4. Ages > 5 min / "no data" → Bluetooth trouble; check the events for
   `watchdog_restart` and `bms_error`.
5. Nothing loads at all → the PC or Tailscale is down; the APRS page
   <https://aprs.fi/info/a/W4BOC-1> still shows the last telemetry and a
   status line such as `AC MAINS LOST …`.
