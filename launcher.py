"""launcher.py — supervisor for the W4BOC Battery Monitor.

Runs `python main.py` as a child process and keeps it alive:

  * restarts it when it exits (crash, watchdog restart, update restart);
  * kills and restarts it when the heartbeat file goes stale — a wedged
    Windows BLE call can block the whole Python process, and no in-process
    watchdog can recover from that (observed on the W4BOC PC as 5-16 h of
    total silence until the next reboot);
  * applies a staged update between runs (copy files, keep site-specific
    ones), verifies that the new version stays up, and rolls back to the
    backed-up files if it does not.

Child exit codes:
    0      clean stop (Ctrl-C / quit)            -> launcher exits 0
    2      restart requested (watchdog / button)  -> restart after 10 s
    3      install the staged update              -> install, restart now
    other  crash                                  -> restart after 30 s

This file is deliberately self-contained (standard library only) and is never
overwritten by the auto-updater. Any argument given to the launcher (e.g.
--simulate) is passed through to main.py.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "logs"
UPDATES_DIR = APP_DIR / "updates"
HEARTBEAT = LOG_DIR / "heartbeat.json"
INSTALL_JSON = UPDATES_DIR / "install.json"
PENDING_JSON = UPDATES_DIR / "pending.json"
LAST_UPDATE_JSON = UPDATES_DIR / "last_update.json"
ROLLBACK_JSON = UPDATES_DIR / "last_rollback.json"

HEARTBEAT_STALE_S = 180      # heartbeat older than this -> child is wedged
STARTUP_GRACE_S = 240        # don't judge the heartbeat before this uptime
VERIFY_HEALTHY_S = 180       # post-update: healthy after this much fresh uptime
MAX_UPDATE_ATTEMPTS = 3
RESTART_DELAY = {2: 10, 3: 0}
CRASH_DELAY_S = 30
POLL_S = 5

# Never overwritten by an update, never backed up.
PROTECTED = {"config.toml", "secrets.toml", "run.bat", "launcher.py"}
PROTECTED_DIRS = {"logs", "updates", "__pycache__", ".git"}
PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".log")

log = logging.getLogger("launcher")


def _setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s launcher: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(LOG_DIR / "launcher.log", maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ---------- child process ----------

def _spawn(extra_args: list[str]) -> subprocess.Popen:
    cmd = [sys.executable, str(APP_DIR / "main.py"), *extra_args]
    log.info(f"starting: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(APP_DIR))


def _kill_tree(proc: subprocess.Popen):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:  # pragma: no cover
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        log.error("child did not die after taskkill")


def _heartbeat_age() -> float | None:
    try:
        return time.time() - HEARTBEAT.stat().st_mtime
    except OSError:
        return None


def _run_child(extra_args: list[str]) -> tuple[int | str, float]:
    """Run one child lifetime. Returns (exit_code | 'stale', healthy_seconds)
    where healthy_seconds is how long the heartbeat stayed fresh (used for
    post-update verification). Ctrl-C reaches the child too (same console);
    we give it a moment to shut down cleanly and report exit code 0."""
    try:
        HEARTBEAT.unlink()
    except OSError:
        pass
    proc = _spawn(extra_args)
    started = time.time()
    healthy_since: float | None = None
    healthy_s = 0.0
    while True:
        try:
            rc = proc.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            rc = None
        except KeyboardInterrupt:
            log.info("Ctrl-C — waiting for child to stop")
            try:
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                _kill_tree(proc)
            return 0, healthy_s
        if rc is not None:
            return rc, healthy_s
        uptime = time.time() - started
        age = _heartbeat_age()
        fresh = age is not None and age <= HEARTBEAT_STALE_S
        if fresh:
            healthy_since = healthy_since or time.time()
            healthy_s = time.time() - healthy_since
            if healthy_s >= VERIFY_HEALTHY_S and PENDING_JSON.exists():
                _mark_update_verified(healthy_s)
        else:
            healthy_since = None
        if uptime > STARTUP_GRACE_S and not fresh:
            log.error(f"heartbeat stale ({'missing' if age is None else f'{int(age)}s old'}) "
                      f"after {int(uptime)}s uptime — killing child pid {proc.pid}")
            _kill_tree(proc)
            return "stale", healthy_s


# ---------- update install / rollback ----------

def _is_protected(rel: Path) -> bool:
    if rel.parts and rel.parts[0] in PROTECTED_DIRS:
        return True
    if any(p == "__pycache__" for p in rel.parts):
        return True
    if rel.name in PROTECTED:
        return True
    return rel.name.endswith(PROTECTED_SUFFIXES)


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p.relative_to(root)


def _clean_pycache(root: Path):
    for d in root.rglob("__pycache__"):
        if d.is_dir() and "updates" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def install_update(app_dir: Path = APP_DIR, updates_dir: Path = UPDATES_DIR) -> bool:
    """Copy the staged tree named in install.json over the app directory,
    backing up every file it replaces. Returns True on success."""
    manifest = _read_json(updates_dir / "install.json")
    if not manifest:
        log.error("install requested but install.json is missing/invalid")
        return False
    staged = Path(manifest.get("staged_dir", ""))
    version = str(manifest.get("version", "?"))
    from_version = str(manifest.get("from_version", "?"))
    if not staged.is_dir() or not (staged / "VERSION").exists():
        log.error(f"staged dir invalid: {staged}")
        (updates_dir / "install.json").unlink(missing_ok=True)
        return False

    backup_dir = updates_dir / "backup" / from_version
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    added: list[str] = []
    copied = 0
    for rel in _iter_files(staged):
        if _is_protected(rel):
            continue
        src = staged / rel
        dst = app_dir / rel
        if dst.exists():
            bk = backup_dir / rel
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bk)
        else:
            added.append(str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    _clean_pycache(app_dir)
    _write_json(updates_dir / "pending.json", {
        "version": version,
        "from_version": from_version,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "attempts": 0,
        "backup_dir": str(backup_dir),
        "added": added,
    })
    (updates_dir / "install.json").unlink(missing_ok=True)
    log.info(f"installed v{version} over v{from_version}: {copied} files copied, "
             f"{len(added)} new, backup in {backup_dir}")
    return True


def rollback(app_dir: Path = APP_DIR, updates_dir: Path = UPDATES_DIR) -> bool:
    pending = _read_json(updates_dir / "pending.json")
    if not pending:
        return False
    backup_dir = Path(pending.get("backup_dir", ""))
    restored = 0
    if backup_dir.is_dir():
        for rel in _iter_files(backup_dir):
            dst = app_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_dir / rel, dst)
            restored += 1
    for rel in pending.get("added", []):
        try:
            (app_dir / rel).unlink()
        except OSError:
            pass
    _clean_pycache(app_dir)
    _write_json(updates_dir / "last_rollback.json", {
        "failed_version": pending.get("version"),
        "restored_version": pending.get("from_version"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "attempts": pending.get("attempts"),
        "restored_files": restored,
        "reported": False,
    })
    (updates_dir / "pending.json").unlink(missing_ok=True)
    log.error(f"rolled back v{pending.get('version')} -> v{pending.get('from_version')} "
              f"({restored} files restored)")
    return True


def _mark_update_verified(healthy_s: float):
    pending = _read_json(PENDING_JSON)
    if not pending:
        return
    _write_json(LAST_UPDATE_JSON, {
        "version": pending.get("version"),
        "from_version": pending.get("from_version"),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reported": False,
    })
    PENDING_JSON.unlink(missing_ok=True)
    log.info(f"update to v{pending.get('version')} verified (healthy {int(healthy_s)}s)")


def _post_run_update_bookkeeping(rc, healthy_s: float):
    """After a child run that ended before the update was verified: count the
    attempt and roll back once MAX_UPDATE_ATTEMPTS is reached. (A child that
    stays healthy is verified while it runs, see _run_child.)"""
    pending = _read_json(PENDING_JSON)
    if not pending:
        return
    if healthy_s >= VERIFY_HEALTHY_S or rc == 0:
        _mark_update_verified(healthy_s)
        return
    pending["attempts"] = int(pending.get("attempts", 0)) + 1
    _write_json(PENDING_JSON, pending)
    log.warning(f"post-update run ended early (rc={rc}, healthy {int(healthy_s)}s) — "
                f"attempt {pending['attempts']}/{MAX_UPDATE_ATTEMPTS}")
    if pending["attempts"] >= MAX_UPDATE_ATTEMPTS:
        rollback()


# ---------- main loop ----------

def main(argv: list[str]) -> int:
    _setup_logging()
    os.chdir(APP_DIR)
    log.info(f"launcher started (python {sys.version.split()[0]}, app dir {APP_DIR})")
    extra = list(argv)
    if INSTALL_JSON.exists():
        install_update()   # e.g. left over from a crash mid-install

    while True:
        rc, healthy_s = _run_child(extra)
        _post_run_update_bookkeeping(rc, healthy_s)

        if rc == 0:
            log.info("child exited cleanly — launcher stopping")
            return 0
        if rc == 3:
            if install_update():
                log.info("restarting into the new version")
            else:
                log.error("install failed — restarting current version")
            delay = 0
        elif rc == "stale":
            delay = RESTART_DELAY[2]
        else:
            delay = RESTART_DELAY.get(rc, CRASH_DELAY_S)
            log.warning(f"child exited with code {rc} — restart in {delay}s")
        if delay:
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
