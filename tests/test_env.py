"""Configured credential names are literal, including operator-chosen LOOPX names."""

import pytest

from looper import env


@pytest.mark.parametrize(("name", "values", "expected"), [
    ("LOOPER_TELEGRAM_BOT_TOKEN", {"LOOPX_TELEGRAM_BOT_TOKEN": "old"}, "default"),
    ("LOOPX_TELEGRAM_BOT_TOKEN", {"LOOPER_TELEGRAM_BOT_TOKEN": "new"}, "default"),
    ("LOOPER_TELEGRAM_BOT_TOKEN", {"LOOPER_TELEGRAM_BOT_TOKEN": "", "LOOPX_TELEGRAM_BOT_TOKEN": "old"}, ""),
    ("LOOPX_TELEGRAM_BOT_TOKEN", {"LOOPX_TELEGRAM_BOT_TOKEN": "literal"}, "literal"),
    ("MY_BOT_TOKEN", {"MY_BOT_TOKEN": "custom"}, "custom"),
])
def test_environment_lookup_uses_only_the_literal_name(monkeypatch, caplog, name, values, expected):
    for key in ("LOOPER_TELEGRAM_BOT_TOKEN", "LOOPX_TELEGRAM_BOT_TOKEN", "MY_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert env.get(name, "default") == expected
    assert not caplog.records
