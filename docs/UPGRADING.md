# Upgrading a v1 site (April 2026 layout) to v2

v1 was two programs (`run_monitor.bat` + `run_dashboard.bat`) in a flat
folder. v2 is one program in a package. Your data files are reused as-is:
`config.toml`, `secrets.toml`, `monitor.db` (the schema is upgraded in place
— a new `mains_events` table is added, nothing else changes) and `logs/`.

Total downtime is a couple of minutes. Do this over Remote Desktop.

## 1. Stop v1

Close the two console windows titled *W4BOC Battery Monitor* and *W4BOC
Battery Dashboard* (or Ctrl-C in each). Check nothing is left:

```powershell
Get-Process python -ErrorAction SilentlyContinue
```

## 2. Put v2 next to the old folder

Download the latest `w4boc-battery-monitor-vX.Y.Z.zip` from
<https://github.com/KK4ODA/w4boc-battery-monitor/releases/latest> and extract
it, e.g. to `C:\Users\Admin\Documents\WD5EMA\w4boc-battery-monitor`.

Copy from the old folder into the new one:

```
config.toml        (keep yours — every v1 key still works)
secrets.toml
monitor.db  monitor.db-wal  monitor.db-shm   (if the -wal/-shm files exist)
logs\                                        (optional, for history)
```

Then install the (unchanged) dependencies for good measure:

```powershell
cd C:\Users\Admin\Documents\WD5EMA\w4boc-battery-monitor
python -m pip install -r requirements.txt
```

## 3. Start v2

Double-click `run.bat`. Within ~20 s the log shows the BMS and charger
samples and the dashboard opens (or is reused if a tab is still open).

Check the *Monitor* card on the Live page: version, *Email enabled*, *APRS
W4BOC-1*, updater status *up to date*. Check `logs\monitor.log` for the usual
`AGW: connected` / `APRS-IS: connected` lines and the first `APRS data:
rf=True is=True` frame.

## 4. Replace the Startup shortcuts

Run `tools\install_autostart.bat`. It deletes the old `run_monitor` /
`run_dashboard` shortcuts from the Startup folder (if they were named that
way — check `shell:startup` and remove any leftovers by hand) and creates
one shortcut to `run.bat`.

## 5. Nightly reboot

The scheduled 01:00 reboot that was covering for the v1 freezes can stay for
now; v2 recovers from those freezes by itself (see `logs\launcher.log` for
"heartbeat stale … killing child" lines). Once you have seen a week of clean
`launcher.log`, the reboot task can be removed.

## 6. Old folder

Keep it for a week as a fallback, then delete it. The old folder's
`secrets.toml` is a copy of a real secret — shred it when you delete.

## Rollback to v1

Stop v2, copy `monitor.db*` back to the old folder (the v2 schema is a
superset; v1 ignores the extra table), start the two v1 bats again.
