import os
import sys
import tomllib
from pathlib import Path

import pytest

from w4boc import settings as S

ROOT = Path(__file__).resolve().parent.parent


def defaults():
    return {f.id: f.default for f in S.SCHEMA}


def form_from(values):
    """Turn typed values into what an HTML form would post."""
    form = {}
    for f in S.SCHEMA:
        v = values.get(f.id, f.default)
        if f.type == "bool":
            if v:
                form[f.id] = "1"
        elif f.type == "list":
            form[f.id] = "\n".join(v)
        elif f.secret:
            form[f.id] = ""          # blank = keep
        else:
            form[f.id] = str(v)
    return form


def test_examples_match_schema():
    sys.path.insert(0, str(ROOT / "tools"))
    from gen_examples import render_examples
    cfg, sec = render_examples()
    assert (ROOT / "config.example.toml").read_text(encoding="utf-8") == cfg, \
        "run python tools/gen_examples.py"
    assert (ROOT / "secrets.toml.example").read_text(encoding="utf-8") == sec


def test_schema_ids_unique_and_defaults_valid():
    ids = [f.id for f in S.SCHEMA]
    assert len(ids) == len(set(ids))
    values, errors = S.validate(form_from(defaults()))
    assert errors == {}, errors
    for f in S.SCHEMA:
        if not f.secret:
            assert values[f.id] == f.default, f.id


def test_render_roundtrip_preserves_values_and_unknown_keys(tmp_path):
    vals = defaults()
    vals["email.recipients"] = ["a@x.org", "b@y.net"]
    vals["aprs.lat"] = 12.5
    vals["site.name"] = 'Say "hi" \\ there'
    extra = {"aprs": {"custom_flag": True}, "mystuff": {"answer": 42}}
    text = S.render_config_toml(vals, extra)
    data = tomllib.loads(text)
    assert data["email"]["recipients"] == ["a@x.org", "b@y.net"]
    assert data["aprs"]["lat"] == 12.5
    assert data["site"]["name"] == 'Say "hi" \\ there'
    assert data["aprs"]["custom_flag"] is True and data["mystuff"]["answer"] == 42
    values2, extra2 = S.current_values(data, {})
    for f in S.SCHEMA:
        if not f.secret:
            assert values2[f.id] == vals[f.id], f.id
    assert extra2 == extra


def test_legacy_mains_lost_minutes_maps_to_slow_minutes():
    values, extra = S.current_values({"alerts": {"mains_lost_minutes": 33}}, {})
    assert values["mains.slow_minutes"] == 33
    assert extra == {}
    # explicit new key wins
    values, _ = S.current_values({"alerts": {"mains_lost_minutes": 33}, "mains": {"slow_minutes": 40}}, {})
    assert values["mains.slow_minutes"] == 40


def test_validate_reports_errors():
    form = form_from(defaults())
    form["bms.mac"] = "not-a-mac"
    form["aprs.lat"] = "123"
    form["site.timezone"] = "Mars/Olympus"
    form["email.recipients"] = "good@x.org\nbad"
    form["dashboard.port"] = "abc"
    form["aprs.symbol_code"] = "##"
    values, errors = S.validate(form)
    assert set(errors) == {"bms.mac", "aprs.lat", "site.timezone", "email.recipients",
                           "dashboard.port", "aprs.symbol_code"}
    assert "bad" in errors["email.recipients"]


def test_validate_cross_field_rules():
    form = form_from(defaults())
    form["alerts.soc_urgent_pct"] = "60"       # > degraded 50
    form["site.instance_lock_port"] = "8080"   # == dashboard port
    _, errors = S.validate(form)
    assert "alerts.soc_urgent_pct" in errors and "site.instance_lock_port" in errors


def test_secrets_keep_and_clear():
    form = form_from(defaults())
    keep = {"victron.encryption_key": "ab" * 16, "email.app_password": "pw", "updater.github_token": ""}
    values, errors = S.validate(form, secrets_keep=keep)
    assert errors == {}
    assert values["victron.encryption_key"] == "ab" * 16 and values["email.app_password"] == "pw"
    form["email.app_password__clear"] = "1"
    form["updater.github_token"] = "  ghp_new  "
    values, _ = S.validate(form, secrets_keep=keep)
    assert values["email.app_password"] == "" and values["updater.github_token"] == "ghp_new"
    form["victron.encryption_key"] = "zz"
    _, errors = S.validate(form, secrets_keep=keep)
    assert "victron.encryption_key" in errors


def test_bools_missing_from_form_are_false():
    form = form_from(defaults())
    form.pop("aprs.enabled", None)
    values, _ = S.validate(form)
    assert values["aprs.enabled"] is False


def test_save_writes_both_files_with_backup(tmp_path):
    cfg = tmp_path / "config.toml"
    sec = tmp_path / "secrets.toml"
    vals = defaults()
    vals["victron.encryption_key"] = "cd" * 16
    S.save(vals, cfg, sec)
    assert not (tmp_path / "config.toml.bak").exists()
    vals["site.name"] = "TEST"
    S.save(vals, cfg, sec, extra={"site": {"old_key": "x"}})
    assert (tmp_path / "config.toml.bak").exists()
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["site"]["name"] == "TEST" and data["site"]["old_key"] == "x"
    assert "encryption_key" not in data["victron"]
    sdata = tomllib.loads(sec.read_text(encoding="utf-8"))
    assert sdata["victron"]["encryption_key"] == "cd" * 16
    assert sdata["email"]["app_password"] == ""


# ---------- autostart ----------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows Startup folder")
def test_autostart_enable_disable(tmp_path, monkeypatch):
    from w4boc import autostart
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup").mkdir(parents=True)
    st = autostart.status()
    assert st["supported"] and not st["enabled"]
    ok, msg = autostart.enable()
    assert ok, msg
    st = autostart.status()
    assert st["enabled"] and st["target_ok"], st
    ok, _ = autostart.ensure_enabled()
    assert ok
    ok, msg = autostart.disable()
    assert ok and not autostart.status()["enabled"]


def test_autostart_unsupported(monkeypatch):
    from w4boc import autostart
    monkeypatch.delenv("APPDATA", raising=False)
    assert autostart.status()["supported"] is False
    assert autostart.enable()[0] is False


# ---------- dashboard routes ----------

def _ctx(storage):
    import asyncio
    from datetime import datetime, timezone
    from w4boc.context import AppContext
    from w4boc.mains import MainsTracker
    loop = asyncio.new_event_loop()
    ctx = AppContext(storage, loop, simulate=True)
    ctx.mains = MainsTracker(storage)
    ctx.monitoring_since = datetime.now(timezone.utc)
    return ctx


def test_settings_page_and_save(storage, tmp_path, monkeypatch, no_email):
    from w4boc import config
    from w4boc.dashboard import create_app
    cfg = tmp_path / "config.toml"
    sec = tmp_path / "secrets.toml"
    cfg.write_text('[site]\nname = "OLD"\n[aprs]\nweird = 1\n[alerts]\nmains_lost_minutes = 50\n', encoding="utf-8")
    sec.write_text('[victron]\nencryption_key = "ab' + 'ab' * 15 + '"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config, "SECRETS_PATH", sec)
    monkeypatch.delenv("APPDATA", raising=False)

    ctx = _ctx(storage)
    app = create_app(ctx); app.testing = True
    c = app.test_client()

    html = c.get("/settings").data.decode()
    assert 'value="OLD"' in html and "Start with Windows" in html
    assert 'name="mains.slow_minutes" value="50"' in html          # legacy key surfaced
    assert "abab" not in html                                       # secrets never echoed

    # a browser posts what the page shows: the on-disk values
    form = form_from(S.current_values(S.load_toml(cfg), {})[0])
    form["site.name"] = "NEW"
    form["bms.mac"] = "bad"
    r = c.post("/api/settings/save", data=form)
    assert r.status_code == 400 and "need attention" in r.data.decode()
    assert tomllib.loads(cfg.read_text())["site"]["name"] == "OLD"   # nothing written

    form["bms.mac"] = "11:22:33:44:55:66"
    form["email.app_password"] = "newpw"
    r = c.post("/api/settings/save", data=form)
    assert r.status_code == 200 and "Saved to" in r.data.decode()
    data = tomllib.loads(cfg.read_text())
    assert data["site"]["name"] == "NEW" and data["bms"]["mac"] == "11:22:33:44:55:66"
    assert data["aprs"]["weird"] == 1                                # unknown key preserved
    assert data["mains"]["slow_minutes"] == 50 and "mains_lost_minutes" not in data.get("alerts", {})
    sdata = tomllib.loads(sec.read_text())
    assert sdata["victron"]["encryption_key"] == "ab" * 16          # kept
    assert sdata["email"]["app_password"] == "newpw"
    ev = storage.recent_events(3)[0]
    assert ev["kind"] == "settings_saved" and "site.name" in ev["detail"] and "newpw" not in ev["detail"]

    # test-email uses the saved values
    r = c.post("/api/settings/test-email")
    assert r.status_code == 200 and no_email and "Test email" in no_email[-1][0]

    r = c.get("/partials/autostart")
    assert r.status_code == 200 and "Only available on Windows" in r.data.decode()
    ctx.loop.close()
