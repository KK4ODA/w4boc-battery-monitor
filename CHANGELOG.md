# Changelog

## [2.1.0] — 2026-09-13

### Added
- **Settings page** on the dashboard: every setting (site, BMS, charger,
  email, APRS, mains detection, alert thresholds, dashboard, updater) is
  edited in the browser with validation, written to `config.toml` /
  `secrets.toml` (previous copies kept as `.bak`, unknown keys preserved),
  and applied with the *Restart now* button. Secrets are never echoed back;
  a *Send test email* button uses the saved credentials.
- **Start with Windows** switch (Settings page): creates/removes the Startup
  shortcut immediately and records the intent in `[site] start_with_windows`,
  which recreates a deleted shortcut at the next start. Also
  `python -m w4boc.autostart enable|disable|status`; the
  `tools\*_autostart.bat` scripts now call it.
- Previously hard-wired values are now settings: SMTP server/port,
  instance-lock port, database retention (raw / per-minute / hourly days),
  APRS tocall, telemetry project name and position-comment prefix (the last
  three default to the exact strings used since v1, so nothing changes on
  the air unless you change them).
- `w4boc/settings.py` is the single schema: `config.py` defaults, the
  Settings page and `config.example.toml` / `secrets.toml.example`
  (regenerated with `tools/gen_examples.py`) all derive from it.

## [2.0.1] — 2026-09-13

### Fixed
- Configuration errors exit with code 4 so the launcher waits 30 s between
  retries instead of restarting every 10 s.
- A clear message (and exit code 4) when the dashboard port is already in
  use, e.g. because the v1 dashboard is still running.
- `SHA256SUMS.txt` in releases is written with LF line endings so
  `sha256sum -c` works on any platform.

## [2.0.0] — 2026-09-13

First release from this repository. Rewrite of the April 2026 monitor into a
single supervised application with auto-update.

### Added
- **One application**: monitor and dashboard run in the same process
  (`run.bat` → `launcher.py` → `main.py`). One log, one startup shortcut.
- **Supervisor (`launcher.py`)** with a heartbeat kill: the v1 process could
  freeze inside a Windows Bluetooth call for hours (no samples, no APRS, no
  watchdog) until the nightly reboot. The launcher now restarts it within
  ~3 minutes; an in-process thread watchdog catches the softer stalls sooner.
- **Mains power detection** (`w4boc/mains.py`): charger silent 5 min while the
  BMS still answers → one Bluetooth-restart to rule out a stuck scanner →
  still silent → **MAINS LOST** (≈11 min after the outage began, high
  confidence). Restored as soon as the charger advertises again. Instant
  detection if the PC is on a USB-connected UPS. A reboot after a gap with an
  unexpected-shutdown record in the Windows event log is recorded as
  *PC down* (retroactive outage detection). Every transition is stored in a
  new `mains_events` table with the outage duration.
- Mains transitions email URGENT/RESOLVED (existing `mains_lost` trigger) and,
  on APRS, send a status packet (`>AC MAINS LOST …`), an immediate position +
  telemetry frame, and add ` MAINS LOST` to the position comment while the
  outage lasts. All optional in `[mains]`.
- **Auto-updater** (`w4boc/updater.py`): checks GitHub Releases, verifies
  SHA-256, stages, installs via the launcher, verifies the new version stays
  up, rolls back otherwise, and emails a NOTICE either way. Dashboard buttons
  for check / install / restart.
- Dashboard: *Mains power* card, *Power* page (outage history and totals),
  *Logs* page, *Monitor* card (version, uptime, restarts, update status),
  mains-lost shading on the 24 h and 7 d charts, reconnect banner during
  restarts, `/api/status` JSON.
- **Duplicate-tab guard**: the browser is opened at startup only if no tab is
  already polling the dashboard; an auto-opened tab closes itself when it
  finds an older live tab (session-restored tabs after the nightly reboot).
- `--simulate` mode with fake data and outage/scanner/UPS toggles; 58 tests
  including golden tests that pin the exact APRS frames seen in production.
- `manage.py mains`, `manage.py check-update`, `tools/install_autostart.bat`.

### Changed
- APRS telemetry formats are byte-identical to v1 (headers, `T#` frames,
  position, APRS-IS login, AGWPE frames). The only behavioural change: the
  *Mains* bit uses the 5-minute window of the new detector instead of 2 min,
  so brief BLE hiccups no longer flip it.
- The sample watchdog no longer restarts on charger silence every 5 minutes
  (which in v1 kept the process too young to ever raise the mains-lost alert
  during a real outage); it restarts once per silence period, and on BMS
  silence as before.
- `config.toml` gained `[mains]`, `[dashboard]`, `[updater]` sections; every
  key has a default so the v1 file keeps working. `[alerts] mains_lost_minutes`
  still sets the slow fallback window.

### Removed
- `run_monitor.bat`, `run_dashboard.bat`, `dashboard.py` as a separate
  process, the `charger_left_storage` trigger (already disabled in v1).
