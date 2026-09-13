"""Test bootstrap: run in simulation mode so config loads from
config.example.toml without secrets, and no Bluetooth is touched."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("W4BOC_SIMULATE", "1")
# Always test against the shipped example config, never a developer's local one.
os.environ["W4BOC_CONFIG_FILE"] = str(ROOT / "config.example.toml")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from w4boc.storage import Storage  # noqa: E402


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    """Never send real email from tests; record calls instead."""
    from w4boc import mailer
    sent = []

    def fake_send(subject, body, recipients=None, **kw):
        sent.append((subject, body))
        return True
    monkeypatch.setattr(mailer, "send", fake_send)
    return sent
