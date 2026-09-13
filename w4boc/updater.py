"""Auto-updater: GitHub Releases → verified download → staged copy.

Flow (all steps are idempotent and safe to repeat):

  check()               GET /repos/{repo}/releases/latest, compare versions
  download_and_stage()  fetch the release zip + SHA256SUMS, verify the digest,
                        extract into updates/staged/<version>/ and sanity-check
                        the layout (VERSION, main.py, w4boc/)
  prepare_install()     pip-install the staged requirements.txt if it changed,
                        then write updates/install.json

The running app never overwrites its own files. After install.json exists the
app exits with code 3; `launcher.py` (the supervisor) copies the staged tree
over the app directory — skipping config.toml, secrets.toml, the database,
logs/ and updates/ — restarts the app, and rolls back automatically if the
new version fails to stay up.

Only the standard library is used so the updater keeps working even if a
third-party dependency is broken.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
ASSET_ZIP_RE = re.compile(r"^w4boc-battery-monitor-v?\d+\.\d+\.\d+\.zip$", re.I)
SHA_NAMES = ("SHA256SUMS.txt", "SHA256SUMS", "sha256sums.txt")
REQUIRED_LAYOUT = ("VERSION", "main.py", "w4boc/__init__.py", "w4boc/app.py")
_VER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int]:
    m = _VER_RE.search(text or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    name: str
    notes: str
    html_url: str
    published_at: str
    zip_name: str
    zip_url: str
    zip_api_url: str
    sha_url: str | None
    sha_api_url: str | None
    prerelease: bool = False


@dataclass
class UpdaterState:
    status: str = "idle"          # idle|checking|downloading|staged|ready|installing|error|up_to_date|disabled
    message: str = ""
    current: str = "0.0.0"
    latest: str | None = None
    latest_url: str | None = None
    latest_notes: str = ""
    staged: str | None = None
    last_check: str | None = None
    last_error: str | None = None
    install_requested: bool = False
    history: list[str] = field(default_factory=list)


class Updater:
    def __init__(self, repo: str, current_version: str, updates_dir: Path,
                 app_dir: Path, token: str = "", timeout: float = 30.0):
        self.repo = repo
        self.current = current_version
        self.updates_dir = Path(updates_dir)
        self.app_dir = Path(app_dir)
        self.token = token
        self.timeout = timeout
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self.state = UpdaterState(current=current_version)
        self._release: ReleaseInfo | None = None

    # ---------- state helpers ----------

    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self.state, k, v)

    def _note(self, msg: str):
        log.info(f"updater: {msg}")
        with self._lock:
            self.state.message = msg
            self.state.history.append(f"{_now_str()} {msg}")
            del self.state.history[:-20]

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.state.__dict__)
            d["history"] = list(self.state.history)
        d["update_available"] = bool(d["latest"] and is_newer(d["latest"], d["current"]))
        return d

    # ---------- HTTP ----------

    def _request(self, url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", f"w4boc-battery-monitor/{self.current}")
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return req

    def _get_json(self, url: str) -> dict:
        with urllib.request.urlopen(self._request(url), timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _download(self, rel: ReleaseInfo, name: str, browser_url: str, api_url: str,
                  dest: Path) -> Path:
        """Download an asset to `dest`. Private repos need the API URL with
        the octet-stream accept header; public ones can use either."""
        url = api_url if self.token else browser_url
        req = self._request(url, accept="application/octet-stream")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(req, timeout=self.timeout) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f, length=1 << 16)
        tmp.replace(dest)
        return dest

    # ---------- public steps ----------

    def check(self) -> ReleaseInfo | None:
        """Query the latest release. Returns the ReleaseInfo (even when it is
        not newer) or None on error / no releases."""
        self._set(status="checking", last_error=None)
        url = f"{GITHUB_API}/repos/{self.repo}/releases/latest"
        try:
            data = self._get_json(url)
        except urllib.error.HTTPError as e:
            msg = "no releases published yet" if e.code == 404 else f"GitHub API HTTP {e.code}"
            self._set(status="error", last_error=msg, last_check=_now_str())
            self._note(f"check failed: {msg}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"
            self._set(status="error", last_error=msg, last_check=_now_str())
            self._note(f"check failed: {msg}")
            return None

        rel = self._parse_release(data)
        self._release = rel
        if rel is None:
            self._set(status="error", last_error="latest release has no matching zip asset",
                      last_check=_now_str())
            self._note("latest release has no w4boc-battery-monitor-vX.Y.Z.zip asset")
            return None
        self._set(latest=rel.version, latest_url=rel.html_url, latest_notes=rel.notes,
                  last_check=_now_str())
        if is_newer(rel.version, self.current):
            self._set(status="available")
            self._note(f"update available: v{rel.version} (running v{self.current})")
        else:
            self._set(status="up_to_date")
            self._note(f"up to date (v{self.current}; latest release v{rel.version})")
        return rel

    @staticmethod
    def _parse_release(data: dict) -> ReleaseInfo | None:
        assets = data.get("assets") or []
        zip_asset = next((a for a in assets if ASSET_ZIP_RE.match(a.get("name", ""))), None)
        if zip_asset is None:
            return None
        sha_asset = next((a for a in assets if a.get("name") in SHA_NAMES), None)
        return ReleaseInfo(
            version=".".join(str(x) for x in parse_version(data.get("tag_name", ""))),
            tag=data.get("tag_name", ""),
            name=data.get("name") or data.get("tag_name", ""),
            notes=data.get("body") or "",
            html_url=data.get("html_url", ""),
            published_at=data.get("published_at", ""),
            zip_name=zip_asset["name"],
            zip_url=zip_asset.get("browser_download_url", ""),
            zip_api_url=zip_asset.get("url", ""),
            sha_url=sha_asset.get("browser_download_url") if sha_asset else None,
            sha_api_url=sha_asset.get("url") if sha_asset else None,
            prerelease=bool(data.get("prerelease")),
        )

    def download_and_stage(self, rel: ReleaseInfo) -> Path | None:
        """Download + verify + extract. Returns the staged directory."""
        staged_dir = self.updates_dir / "staged" / rel.version
        if (staged_dir / "VERSION").exists() and self._staged_ok(staged_dir, rel.version):
            self._set(status="staged", staged=rel.version)
            self._note(f"v{rel.version} already staged")
            return staged_dir

        self.updates_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.updates_dir / rel.zip_name
        self._set(status="downloading")
        self._note(f"downloading {rel.zip_name}")
        try:
            self._download(rel, rel.zip_name, rel.zip_url, rel.zip_api_url, zip_path)
            if not rel.sha_url or not rel.sha_api_url:
                raise RuntimeError("release has no SHA256SUMS asset — refusing unverified update")
            sha_path = self.updates_dir / "SHA256SUMS.txt"
            self._download(rel, "SHA256SUMS.txt", rel.sha_url, rel.sha_api_url, sha_path)
            expected = parse_sha256sums(sha_path.read_text(encoding="utf-8")).get(rel.zip_name)
            if not expected:
                raise RuntimeError(f"SHA256SUMS has no entry for {rel.zip_name}")
            actual = sha256_file(zip_path)
            if actual.lower() != expected.lower():
                zip_path.unlink(missing_ok=True)
                raise RuntimeError("SHA-256 mismatch — download discarded")
            self._note("download verified (SHA-256 OK)")
            extract_zip(zip_path, staged_dir)
            if not self._staged_ok(staged_dir, rel.version):
                raise RuntimeError("staged archive has unexpected layout or VERSION")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self._set(status="error", last_error=msg)
            self._note(f"download/stage failed: {msg}")
            shutil.rmtree(staged_dir, ignore_errors=True)
            return None

        self._cleanup(keep_zip=zip_path, keep_staged=staged_dir)
        self._set(status="staged", staged=rel.version)
        self._note(f"v{rel.version} staged in {staged_dir}")
        return staged_dir

    def _staged_ok(self, staged_dir: Path, version: str) -> bool:
        for rel in REQUIRED_LAYOUT:
            if not (staged_dir / rel).exists():
                return False
        try:
            v = (staged_dir / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return parse_version(v) == parse_version(version)

    def prepare_install(self, version: str) -> bool:
        """Install new Python requirements (if changed) and write install.json.
        Returns True when the launcher can proceed after the app exits."""
        staged_dir = self.updates_dir / "staged" / version
        if not self._staged_ok(staged_dir, version):
            self._set(status="error", last_error="nothing staged")
            return False
        self._set(status="installing")
        new_req = staged_dir / "requirements.txt"
        cur_req = self.app_dir / "requirements.txt"
        if new_req.exists() and _file_digest(new_req) != _file_digest(cur_req):
            self._note("requirements changed — running pip install")
            ok, out = pip_install(new_req)
            if not ok:
                self._set(status="error", last_error=f"pip install failed: {out[-400:]}")
                self._note("pip install failed; update aborted (still running current version)")
                return False
            self._note("pip install OK")
        manifest = {
            "version": version,
            "from_version": self.current,
            "staged_dir": str(staged_dir),
            "requested_at": _now_str(),
        }
        (self.updates_dir / "install.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self._set(status="ready", install_requested=True)
        self._note(f"install of v{version} requested — restarting")
        return True

    def check_and_stage(self) -> str:
        """One full cycle. Returns 'up_to_date' | 'staged' | 'error' | 'busy'."""
        if not self._busy.acquire(blocking=False):
            return "busy"
        try:
            rel = self.check()
            if rel is None:
                return "error"
            if not is_newer(rel.version, self.current):
                return "up_to_date"
            return "staged" if self.download_and_stage(rel) else "error"
        finally:
            self._busy.release()

    def _cleanup(self, keep_zip: Path, keep_staged: Path):
        for p in self.updates_dir.glob("*.zip"):
            if p != keep_zip:
                p.unlink(missing_ok=True)
        for p in self.updates_dir.glob("*.part"):
            p.unlink(missing_ok=True)
        staged_root = self.updates_dir / "staged"
        if staged_root.exists():
            for d in staged_root.iterdir():
                if d.is_dir() and d != keep_staged:
                    shutil.rmtree(d, ignore_errors=True)


# ---------- helpers (also used by the launcher and tests) ----------

def parse_sha256sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        out[name.lstrip("*").strip()] = digest
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_digest(path: Path) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def extract_zip(zip_path: Path, dest: Path):
    """Extract safely into `dest`, stripping a single top-level directory if
    every entry lives under one (GitHub-style archives)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            raise RuntimeError("empty archive")
        tops = {n.split("/", 1)[0] for n in names}
        strip = ""
        if len(tops) == 1 and all("/" in n for n in names):
            strip = next(iter(tops)) + "/"
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if strip and name.startswith(strip):
                name = name[len(strip):]
            if not name:
                continue
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"unsafe path in archive: {info.filename}")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def pip_install(requirements: Path, timeout: float = 900) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements),
           "--disable-pip-version-check", "--no-input"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sleep_s(s: float):
    time.sleep(s)
