"""``LOOPER_*`` variables fall back to their ``LOOPX_*`` names from before the rename, for one release."""

from __future__ import annotations

import logging

import pytest

from looper import env


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LOOPER_RUNTIME_ROOT", "LOOPX_RUNTIME_ROOT", "LOOPER_TELEGRAM_BOT_TOKEN", "LOOPX_TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(env, "_warned", set())


@pytest.mark.parametrize(
    ("name", "environ", "value", "warned"),
    [
        ("LOOPER_RUNTIME_ROOT", {"LOOPER_RUNTIME_ROOT": "new"}, "new", False),
        ("LOOPER_RUNTIME_ROOT", {"LOOPX_RUNTIME_ROOT": "old"}, "old", True),
        ("LOOPER_RUNTIME_ROOT", {"LOOPER_RUNTIME_ROOT": "new", "LOOPX_RUNTIME_ROOT": "old"}, "new", False),
        ("LOOPER_RUNTIME_ROOT", {"LOOPER_RUNTIME_ROOT": "", "LOOPX_RUNTIME_ROOT": "old"}, "", False),
        ("LOOPER_RUNTIME_ROOT", {}, "default", False),
        # A stored setting that still names the old variable (a channel's bot_token_env) finds the renamed one.
        ("LOOPX_TELEGRAM_BOT_TOKEN", {"LOOPER_TELEGRAM_BOT_TOKEN": "new"}, "new", False),
        ("LOOPX_TELEGRAM_BOT_TOKEN", {"LOOPX_TELEGRAM_BOT_TOKEN": "old"}, "old", False),
        ("ANTHROPIC_API_KEY_FOR_NOTHING", {}, "default", False),
    ],
    ids=["new", "old-only", "both", "new-empty", "neither", "stored-old-name", "stored-old-name-set", "other"],
)
def test_a_looper_variable_falls_back_to_its_loopx_name_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    name: str, environ: dict[str, str], value: str, warned: bool,
) -> None:
    for key, item in environ.items():
        monkeypatch.setenv(key, item)

    with caplog.at_level(logging.WARNING, logger=env.__name__):
        assert env.get(name, "default") == value
        assert env.get(name, "default") == value

    deprecations = [r.getMessage() for r in caplog.records if "is deprecated" in r.getMessage()]
    assert deprecations == (["LOOPX_RUNTIME_ROOT is deprecated: rename it to LOOPER_RUNTIME_ROOT "
                             "(the old name is read until the next release)"] if warned else [])
