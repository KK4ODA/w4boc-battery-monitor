"""Regenerate config.example.toml and secrets.toml.example from the settings
schema (tests/test_settings.py fails if they drift). Run after editing
w4boc/settings.py:  python tools/gen_examples.py"""
import _shim  # noqa: F401
from pathlib import Path

from w4boc import APP_DIR
from w4boc import settings as S


def render_examples() -> tuple[str, str]:
    defaults = {f.id: f.default for f in S.SCHEMA}
    return S.render_config_toml(defaults), S.render_secrets_toml(defaults, placeholders=True)


if __name__ == "__main__":
    cfg, sec = render_examples()
    (APP_DIR / "config.example.toml").write_text(cfg, encoding="utf-8", newline="\n")
    (APP_DIR / "secrets.toml.example").write_text(sec, encoding="utf-8", newline="\n")
    print("wrote config.example.toml and secrets.toml.example")
