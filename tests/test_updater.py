import io
import json
import zipfile
from pathlib import Path

import pytest

from w4boc import updater as U


def test_version_parsing():
    assert U.parse_version("v2.1.0") == (2, 1, 0)
    assert U.parse_version("2.10.3") == (2, 10, 3)
    assert U.parse_version("garbage") == (0, 0, 0)
    assert U.is_newer("v2.1.0", "2.0.0")
    assert not U.is_newer("2.0.0", "2.0.0")
    assert not U.is_newer("1.9.9", "2.0.0")
    assert U.is_newer("2.0.10", "2.0.9")


def test_parse_sha256sums():
    txt = "abc123  w4boc-battery-monitor-v2.1.0.zip\n# comment\ndef456 *other.zip\n\n"
    d = U.parse_sha256sums(txt)
    assert d == {"w4boc-battery-monitor-v2.1.0.zip": "abc123", "other.zip": "def456"}


def _make_zip(path: Path, files: dict[str, str], top: str = ""):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(f"{top}{name}", content)


STAGED_FILES = {
    "VERSION": "2.1.0\n",
    "main.py": "print('hi')\n",
    "w4boc/__init__.py": "",
    "w4boc/app.py": "# app\n",
    "requirements.txt": "flask\n",
}


def test_extract_zip_strips_single_top_dir(tmp_path):
    zp = tmp_path / "a.zip"
    _make_zip(zp, STAGED_FILES, top="w4boc-battery-monitor/")
    dest = tmp_path / "out"
    U.extract_zip(zp, dest)
    assert (dest / "VERSION").read_text() == "2.1.0\n"
    assert (dest / "w4boc" / "app.py").exists()


def test_extract_zip_flat(tmp_path):
    zp = tmp_path / "b.zip"
    _make_zip(zp, STAGED_FILES)
    dest = tmp_path / "out"
    U.extract_zip(zp, dest)
    assert (dest / "main.py").exists()


def test_extract_zip_rejects_traversal(tmp_path):
    zp = tmp_path / "c.zip"
    _make_zip(zp, {"../evil.py": "x", "VERSION": "1"})
    with pytest.raises(RuntimeError):
        U.extract_zip(zp, tmp_path / "out")


def _release_payload(version="2.1.0", with_sha=True):
    name = f"w4boc-battery-monitor-v{version}.zip"
    assets = [{"name": name, "browser_download_url": f"https://x/{name}", "url": f"https://api/{name}"}]
    if with_sha:
        assets.append({"name": "SHA256SUMS.txt", "browser_download_url": "https://x/SHA256SUMS.txt",
                       "url": "https://api/sha"})
    return {"tag_name": f"v{version}", "name": f"v{version}", "body": "notes", "html_url": "https://x/rel",
            "published_at": "2026-09-13T00:00:00Z", "assets": assets, "prerelease": False}


def test_parse_release():
    rel = U.Updater._parse_release(_release_payload())
    assert rel.version == "2.1.0" and rel.zip_name.endswith("v2.1.0.zip") and rel.sha_url
    assert U.Updater._parse_release({"tag_name": "v9.9.9", "assets": []}) is None


def _fake_downloads(monkeypatch, upd, zip_bytes: bytes, sha_text: str | None):
    def fake_download(rel, name, browser_url, api_url, dest: Path):
        if name.endswith(".zip"):
            dest.write_bytes(zip_bytes)
        else:
            dest.write_text(sha_text or "", encoding="utf-8")
        return dest
    monkeypatch.setattr(upd, "_download", fake_download)


def _zip_bytes(files=STAGED_FILES, top="w4boc-battery-monitor/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in files.items():
            z.writestr(f"{top}{n}", c)
    return buf.getvalue()


def test_download_and_stage_verifies_sha(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"; app_dir.mkdir()
    upd = U.Updater("o/r", "2.0.0", tmp_path / "updates", app_dir)
    rel = U.Updater._parse_release(_release_payload())
    data = _zip_bytes()
    import hashlib
    good = hashlib.sha256(data).hexdigest()
    _fake_downloads(monkeypatch, upd, data, f"{good}  {rel.zip_name}\n")
    staged = upd.download_and_stage(rel)
    assert staged is not None and (staged / "VERSION").read_text().strip() == "2.1.0"
    assert upd.snapshot()["status"] == "staged" and upd.snapshot()["staged"] == "2.1.0"
    # second call is a no-op (already staged)
    assert upd.download_and_stage(rel) == staged


def test_download_and_stage_rejects_bad_sha(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"; app_dir.mkdir()
    upd = U.Updater("o/r", "2.0.0", tmp_path / "updates", app_dir)
    rel = U.Updater._parse_release(_release_payload())
    _fake_downloads(monkeypatch, upd, _zip_bytes(), f"{'0' * 64}  {rel.zip_name}\n")
    assert upd.download_and_stage(rel) is None
    snap = upd.snapshot()
    assert snap["status"] == "error" and "SHA-256" in snap["last_error"]
    assert not (tmp_path / "updates" / "staged" / "2.1.0").exists()
    assert not list((tmp_path / "updates").glob("*.zip"))    # discarded


def test_download_and_stage_requires_sha_asset(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"; app_dir.mkdir()
    upd = U.Updater("o/r", "2.0.0", tmp_path / "updates", app_dir)
    rel = U.Updater._parse_release(_release_payload(with_sha=False))
    _fake_downloads(monkeypatch, upd, _zip_bytes(), None)
    assert upd.download_and_stage(rel) is None
    assert "SHA256SUMS" in upd.snapshot()["last_error"]


def test_download_and_stage_rejects_wrong_version_layout(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"; app_dir.mkdir()
    upd = U.Updater("o/r", "2.0.0", tmp_path / "updates", app_dir)
    rel = U.Updater._parse_release(_release_payload("2.1.0"))
    files = dict(STAGED_FILES, VERSION="2.2.0\n")     # mismatch
    data = _zip_bytes(files)
    import hashlib
    _fake_downloads(monkeypatch, upd, data, f"{hashlib.sha256(data).hexdigest()}  {rel.zip_name}\n")
    assert upd.download_and_stage(rel) is None


def test_prepare_install_writes_manifest_and_runs_pip_when_needed(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"; app_dir.mkdir()
    (app_dir / "requirements.txt").write_text("flask\n")
    upd = U.Updater("o/r", "2.0.0", tmp_path / "updates", app_dir)
    staged = tmp_path / "updates" / "staged" / "2.1.0"
    for n, c in STAGED_FILES.items():
        p = staged / n; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(c)
    calls = []
    monkeypatch.setattr(U, "pip_install", lambda req, timeout=900: (calls.append(req), (True, "ok"))[1])
    assert upd.prepare_install("2.1.0")
    assert calls == []                                   # same requirements -> no pip
    m = json.loads((tmp_path / "updates" / "install.json").read_text())
    assert m["version"] == "2.1.0" and m["from_version"] == "2.0.0" and Path(m["staged_dir"]) == staged

    (staged / "requirements.txt").write_text("flask\nnewdep\n")
    assert upd.prepare_install("2.1.0")
    assert len(calls) == 1                               # changed -> pip ran

    monkeypatch.setattr(U, "pip_install", lambda req, timeout=900: (False, "boom"))
    (staged / "requirements.txt").write_text("flask\nother\n")
    assert not upd.prepare_install("2.1.0")
    assert "pip install failed" in upd.snapshot()["last_error"]


def test_check_handles_http_errors(monkeypatch, tmp_path):
    import urllib.error
    upd = U.Updater("o/r", "2.0.0", tmp_path / "u", tmp_path)

    def boom(url):
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    monkeypatch.setattr(upd, "_get_json", boom)
    assert upd.check() is None
    assert upd.snapshot()["last_error"] == "no releases published yet"

    monkeypatch.setattr(upd, "_get_json", lambda url: _release_payload("2.0.0"))
    rel = upd.check()
    assert rel.version == "2.0.0" and upd.snapshot()["status"] == "up_to_date"
    monkeypatch.setattr(upd, "_get_json", lambda url: _release_payload("2.3.0"))
    upd.check()
    assert upd.snapshot()["status"] == "available" and upd.snapshot()["update_available"]
