"""Existing units can still supply credentials under the pre-rename prefix."""

import pytest

from looper import env


@pytest.mark.parametrize(("name", "values", "expected"), [
    ("LOOPER_TELEGRAM_BOT_TOKEN", {"LOOPX_TELEGRAM_BOT_TOKEN": "old"}, "old"),
    ("LOOPX_TELEGRAM_BOT_TOKEN", {"LOOPER_TELEGRAM_BOT_TOKEN": "new"}, "default"),
    ("LOOPER_TELEGRAM_BOT_TOKEN", {"LOOPER_TELEGRAM_BOT_TOKEN": "", "LOOPX_TELEGRAM_BOT_TOKEN": "old"}, ""),
    ("LOOPX_TELEGRAM_BOT_TOKEN", {"LOOPX_TELEGRAM_BOT_TOKEN": "literal"}, "literal"),
    ("MY_BOT_TOKEN", {"MY_BOT_TOKEN": "custom"}, "custom"),
    ("LOOPER_TELEGRAM_BOT_TOKEN", {"LOOPER_TELEGRAM_BOT_TOKEN": "new", "LOOPX_TELEGRAM_BOT_TOKEN": "old"}, "new"),
    ("LOOPER_TELEGRAM_BOT_TOKEN", {"LOOPX_TELEGRAM_BOT_TOKEN": ""}, ""),
    ("MY_BOT_TOKEN", {}, "default"),
])
def test_environment_lookup_preserves_explicit_values_and_legacy_fallback(monkeypatch, caplog, name, values, expected):
    for key in ("LOOPER_TELEGRAM_BOT_TOKEN", "LOOPX_TELEGRAM_BOT_TOKEN", "MY_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert env.get(name, "default") == expected
    assert not caplog.records
