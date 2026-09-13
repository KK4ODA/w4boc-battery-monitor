"""Start-with-Windows: a shortcut to run.bat in the user's Startup folder.

The shortcut's existence is the effective state; `[site] start_with_windows`
in config.toml records the intent so a fresh start can recreate a deleted
shortcut (see app.py). Works per-user (no admin rights), which matches the
auto-login setup on the site PC.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import APP_DIR

SHORTCUT_NAME = "W4BOC Battery Monitor.lnk"
LEGACY_SHORTCUTS = (
    "run_monitor.lnk", "run_dashboard.lnk",
    "run_monitor.bat - Shortcut.lnk", "run_dashboard.bat - Shortcut.lnk",
    "run_monitor - Shortcut.lnk", "run_dashboard - Shortcut.lnk",
)
TARGET = APP_DIR / "run.bat"


def supported() -> bool:
    return sys.platform == "win32" and bool(os.environ.get("APPDATA"))


def startup_dir() -> Path | None:
    if not supported():
        return None
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path | None:
    d = startup_dir()
    return d / SHORTCUT_NAME if d else None


def _ps(script: str, timeout: float = 20) -> tuple[bool, str]:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    return r.returncode == 0 and not err, out or err


def _q(s: str) -> str:
    """Single-quote for PowerShell."""
    return "'" + str(s).replace("'", "''") + "'"


def status() -> dict:
    """{'supported', 'enabled', 'path', 'target', 'target_ok'}"""
    p = shortcut_path()
    if p is None:
        return {"supported": False, "enabled": False, "path": "", "target": "", "target_ok": False,
                "message": "only on Windows"}
    if not p.exists():
        return {"supported": True, "enabled": False, "path": str(p), "target": "", "target_ok": False,
                "message": "not enabled"}
    ok, out = _ps(f"(New-Object -ComObject WScript.Shell).CreateShortcut({_q(p)}).TargetPath")
    target = out if ok else ""
    target_ok = bool(target) and Path(target).resolve() == TARGET.resolve()
    msg = "enabled" if target_ok else f"shortcut points elsewhere: {target or '?'}"
    return {"supported": True, "enabled": True, "path": str(p), "target": target,
            "target_ok": target_ok, "message": msg}


def enable() -> tuple[bool, str]:
    p = shortcut_path()
    if p is None:
        return False, "start-with-Windows is only available on Windows"
    if not TARGET.exists():
        return False, f"{TARGET} not found"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        for name in LEGACY_SHORTCUTS:
            (p.parent / name).unlink(missing_ok=True)
    except OSError as e:
        return False, f"cannot access Startup folder: {e}"
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_q(p)}); "
        f"$s.TargetPath = {_q(TARGET)}; $s.WorkingDirectory = {_q(APP_DIR)}; "
        f"$s.WindowStyle = 7; $s.Description = 'W4BOC Battery Monitor'; $s.Save(); 'ok'"
    )
    ok, out = _ps(script)
    if not ok or not p.exists():
        return False, f"could not create the shortcut: {out}"
    return True, f"Startup shortcut created ({p})"


def disable() -> tuple[bool, str]:
    p = shortcut_path()
    if p is None:
        return False, "start-with-Windows is only available on Windows"
    try:
        removed = p.exists()
        p.unlink(missing_ok=True)
    except OSError as e:
        return False, f"could not remove the shortcut: {e}"
    return True, "Startup shortcut removed" if removed else "Startup shortcut was not present"


def ensure_enabled() -> tuple[bool, str]:
    """Create the shortcut only if it is missing or points elsewhere."""
    st = status()
    if not st["supported"]:
        return False, st["message"]
    if st["enabled"] and st["target_ok"]:
        return True, "Startup shortcut present"
    return enable()


if __name__ == "__main__":   # python -m w4boc.autostart [status|enable|disable]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "enable":
        ok, msg = enable()
    elif cmd == "disable":
        ok, msg = disable()
    else:
        st = status()
        ok, msg = True, f"{st['message']} ({st['path']})"
    print(msg)
    sys.exit(0 if ok else 1)
