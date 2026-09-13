# W4BOC Battery Monitor

Monitors the LiFePO4 battery and Victron charger that power the **W4BOC**
repeater on Stone Mountain, GA, from a small Windows PC at the site:

* polls the JBD BMS and listens to the Victron IP22 charger over Bluetooth LE;
* detects **mains (AC) power loss** within minutes and records every outage;
* serves a local web **dashboard** (live readings, 24 h / 7 d charts, power
  history, logs, update & restart controls);
* emails **alerts** (SoC, temperature, BMS protection, charger error, mains
  lost/restored, PC power loss) and daily digests while degraded;
* broadcasts **APRS telemetry** on RF via soundmodem (AGWPE) and to APRS-IS
  as `W4BOC-1` — [aprs.fi/info/a/W4BOC-1](https://aprs.fi/info/a/W4BOC-1);
* keeps itself alive with an external supervisor (`launcher.py`) and
  **updates itself** from this repository's GitHub Releases.

Version 2 merges the former separate *monitor* and *dashboard* programs into
one process. See [CHANGELOG.md](CHANGELOG.md) for what changed and
[docs/UPGRADING.md](docs/UPGRADING.md) to move a v1 site over.

## Layout

```
run.bat                 start here (double-click / Startup shortcut)
launcher.py             supervisor: restarts, heartbeat kill, update install/rollback
main.py                 the application entry point (used by launcher.py)
w4boc/                  the package
  app.py                wiring: tasks + dashboard thread + exit codes
  monitor.py            BMS / charger BLE tasks, evaluator, watchdogs, heartbeat
  mains.py              mains-power detector (see docstring for the rules)
  aprs.py agwpe.py aprsis.py   APRS telemetry (formats locked by golden tests)
  alerts.py digest.py mailer.py   email
  updater.py            GitHub Releases auto-updater
  dashboard.py + templates/ static/   Flask dashboard
  storage.py            SQLite (WAL) — samples, events, mains history, state
  simulate.py           --simulate mode: fake site data for development
tools/                  diagnostics (BLE scan, one-shot reads, mailer test),
                        autostart shortcut installer, release builder
docs/                   OPERATION_MANUAL, DASHBOARD_GUIDE, UPGRADING
tests/                  pytest suite (runs without hardware)
```

## Install (site PC)

Requirements: Windows 10/11, Python 3.11+ on `PATH`, soundmodem with its
AGWPE server on port 8000 (for RF), Bluetooth adapter.

1. Download `w4boc-battery-monitor-vX.Y.Z.zip` from the
   [latest release](https://github.com/KK4ODA/w4boc-battery-monitor/releases/latest)
   and extract it (the zip contains a single `w4boc-battery-monitor` folder —
   put it wherever you like, e.g. `C:\W4BOC\w4boc-battery-monitor`).
2. `pip install -r requirements.txt`
3. Copy `config.example.toml` → `config.toml` and `secrets.toml.example` →
   `secrets.toml`; fill in the MACs, Victron encryption key, Gmail app
   password, APRS-IS passcode and recipients.
4. Double-click `run.bat`. The dashboard opens at <http://localhost:8080/>.
5. `tools\install_autostart.bat` adds a Startup-folder shortcut so it comes
   back after a reboot (auto-login is assumed).

Upgrading from v1: [docs/UPGRADING.md](docs/UPGRADING.md).

## Updating

The monitor checks this repository's releases every 6 hours (and 3 minutes
after each start). A newer release is downloaded, its SHA-256 verified against
the published `SHA256SUMS.txt`, staged, and — with `auto_install = true` —
installed with a ~20 s restart. `config.toml`, `secrets.toml`, the database
and logs are never touched. If the new version fails to stay up, the launcher
restores the previous files automatically and emails a NOTICE. The dashboard's
*Monitor* card shows the current/latest versions and has *Check for updates*,
*Install* and *Restart* buttons.

To publish a release: bump `VERSION`, add a `## [X.Y.Z]` section to
`CHANGELOG.md`, commit, then `git tag vX.Y.Z && git push --tags`. The
*Release* workflow runs the tests, builds the archive and publishes it.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q                      # 58 tests, no hardware needed
python main.py --simulate --no-browser   # fake BMS/charger, dashboard on :8080
python launcher.py --simulate            # same, under the supervisor
```

In simulation the dashboard's *Monitor* card gains buttons to fake an outage,
kill the charger scanner or report a UPS AC-loss, which exercises the whole
detection → alert → restore chain. APRS is never transmitted in simulation
unless `W4BOC_SIM_APRS=1` is set.

## License

MIT — see [LICENSE](LICENSE).
