import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load_launcher()


def test_protected_paths():
    P = Path
    assert L._is_protected(P("config.toml"))
    assert L._is_protected(P("secrets.toml"))
    assert L._is_protected(P("run.bat"))
    assert L._is_protected(P("launcher.py"))
    assert L._is_protected(P("monitor.db"))
    assert L._is_protected(P("monitor.db-wal"))
    assert L._is_protected(P("logs/monitor.log"))
    assert L._is_protected(P("updates/x.zip"))
    assert L._is_protected(P("w4boc/__pycache__/app.cpython-311.pyc"))
    assert not L._is_protected(P("w4boc/app.py"))
    assert not L._is_protected(P("VERSION"))
    assert not L._is_protected(P("config.example.toml"))
    assert not L._is_protected(P("w4boc/templates/live.html"))


def _app_tree(tmp_path):
    app = tmp_path / "app"
    for rel, content in {
        "VERSION": "2.0.0\n",
        "main.py": "old main\n",
        "config.toml": "site config\n",
        "secrets.toml": "secret\n",
        "monitor.db": "db\n",
        "launcher.py": "launcher v1\n",
        "w4boc/app.py": "old app\n",
        "w4boc/old_only.py": "keep me\n",
        "logs/monitor.log": "log\n",
    }.items():
        p = app / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
    staged = app / "updates" / "staged" / "2.1.0"
    for rel, content in {
        "VERSION": "2.1.0\n",
        "main.py": "new main\n",
        "config.toml": "SHOULD NOT BE COPIED\n",
        "launcher.py": "launcher v2\n",
        "w4boc/app.py": "new app\n",
        "w4boc/new_module.py": "new\n",
        "w4boc/__pycache__/app.pyc": "junk",
    }.items():
        p = staged / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
    (app / "updates" / "install.json").write_text(json.dumps({
        "version": "2.1.0", "from_version": "2.0.0", "staged_dir": str(staged)}))
    return app


def test_install_then_rollback(tmp_path):
    app = _app_tree(tmp_path)
    upd = app / "updates"
    assert L.install_update(app, upd)

    assert (app / "VERSION").read_text() == "2.1.0\n"
    assert (app / "main.py").read_text() == "new main\n"
    assert (app / "w4boc/app.py").read_text() == "new app\n"
    assert (app / "w4boc/new_module.py").exists()
    assert (app / "w4boc/old_only.py").read_text() == "keep me\n"
    # protected files untouched
    assert (app / "config.toml").read_text() == "site config\n"
    assert (app / "secrets.toml").read_text() == "secret\n"
    assert (app / "launcher.py").read_text() == "launcher v1\n"
    assert (app / "monitor.db").read_text() == "db\n"
    assert not (app / "w4boc/__pycache__").exists()
    assert not (upd / "install.json").exists()

    pending = json.loads((upd / "pending.json").read_text())
    assert pending["version"] == "2.1.0" and pending["attempts"] == 0
    assert "w4boc/new_module.py" in [p.replace("\\", "/") for p in pending["added"]]
    backup = Path(pending["backup_dir"])
    assert (backup / "main.py").read_text() == "old main\n"
    assert (backup / "VERSION").read_text() == "2.0.0\n"

    assert L.rollback(app, upd)
    assert (app / "VERSION").read_text() == "2.0.0\n"
    assert (app / "main.py").read_text() == "old main\n"
    assert (app / "w4boc/app.py").read_text() == "old app\n"
    assert not (app / "w4boc/new_module.py").exists()
    assert not (upd / "pending.json").exists()
    rb = json.loads((upd / "last_rollback.json").read_text())
    assert rb["failed_version"] == "2.1.0" and rb["restored_version"] == "2.0.0" and rb["reported"] is False


def test_install_with_missing_manifest(tmp_path):
    app = tmp_path / "app"; (app / "updates").mkdir(parents=True)
    assert not L.install_update(app, app / "updates")


def test_post_run_bookkeeping(tmp_path, monkeypatch):
    app = _app_tree(tmp_path)
    upd = app / "updates"
    monkeypatch.setattr(L, "PENDING_JSON", upd / "pending.json")
    monkeypatch.setattr(L, "LAST_UPDATE_JSON", upd / "last_update.json")
    orig_rollback = L.rollback
    monkeypatch.setattr(L, "rollback", lambda: orig_rollback(app, upd))
    assert L.install_update(app, upd)

    # two early exits -> attempts 2, still pending
    L._post_run_update_bookkeeping(1, 20.0)
    L._post_run_update_bookkeeping("stale", 0.0)
    assert json.loads((upd / "pending.json").read_text())["attempts"] == 2
    # third -> rollback
    L._post_run_update_bookkeeping(1, 5.0)
    assert not (upd / "pending.json").exists()
    assert (app / "VERSION").read_text() == "2.0.0\n"

    # fresh install verified by a healthy run
    (upd / "install.json").write_text(json.dumps({
        "version": "2.1.0", "from_version": "2.0.0", "staged_dir": str(upd / "staged" / "2.1.0")}))
    assert L.install_update(app, upd)
    L._post_run_update_bookkeeping(2, L.VERIFY_HEALTHY_S + 1)
    assert not (upd / "pending.json").exists()
    last = json.loads((upd / "last_update.json").read_text())
    assert last["version"] == "2.1.0" and last["reported"] is False
