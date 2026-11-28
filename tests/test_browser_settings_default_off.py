"""The browser/computer-use switches: default-off in-memory settings with no consumer yet.

The module owns the ``browser.*`` system settings only. With no row (or any value other than the string ``"true"``)
every switch is off. A failed read leaves them off. They are not chat settings, and nothing in the prompt, tool,
argv, OpenAPI or task-JSON surfaces changes. The golden hashes freeze the main tool list and the OpenAPI document
(task schemas included; the package version is redacted).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings
from looper.agents.browser import browser_settings
from looper.agents.ops import planner, room, service, vault
from looper.agents.room import MemberKind
from tests.db_support import DSN, run_db

KEYS = (
    browser_settings.ENABLED_KEY,
    browser_settings.USER_CHROME_KEY,
    browser_settings.DESKTOP_KEY,
    browser_settings.RETENTION_DAYS_KEY,
)
DEFAULTS = {
    "enabled": False,
    "user_chrome": False,
    "desktop": False,
    "retention_days": browser_settings.DEFAULT_RETENTION_DAYS,
}


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_settings, "_enabled", False)
    monkeypatch.setattr(browser_settings, "_user_chrome", False)
    monkeypatch.setattr(browser_settings, "_desktop", False)
    monkeypatch.setattr(
        browser_settings, "_retention_days", browser_settings.DEFAULT_RETENTION_DAYS
    )
    monkeypatch.setattr(browser_settings, "_generation", 0)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def test_the_factory_defaults_are_all_off() -> None:
    assert browser_settings.enabled() is False
    assert browser_settings.user_chrome_enabled() is False
    assert browser_settings.desktop_enabled() is False
    assert browser_settings.retention_days() == 7
    assert browser_settings.generation() == 0
    assert browser_settings.view() == DEFAULTS


@pytest.mark.parametrize("stored", [None, "false", "TRUE", "1", "yes", "true "])
def test_only_the_lowercase_string_true_turns_a_switch_on(stored: str | None) -> None:
    browser_settings.apply_stored(
        {
            browser_settings.ENABLED_KEY: stored,
            browser_settings.USER_CHROME_KEY: stored,
            browser_settings.DESKTOP_KEY: stored,
        }
    )
    assert browser_settings.view() == {**DEFAULTS}


def test_each_boolean_switch_reads_its_own_row() -> None:
    browser_settings.apply_stored(
        {
            browser_settings.ENABLED_KEY: "true",
            browser_settings.USER_CHROME_KEY: "true",
            browser_settings.DESKTOP_KEY: "true",
        }
    )
    assert (
        browser_settings.enabled(),
        browser_settings.user_chrome_enabled(),
        browser_settings.desktop_enabled(),
    ) == (True, True, True)
    browser_settings.apply_stored({browser_settings.ENABLED_KEY: "true"})
    assert browser_settings.enabled() is True
    assert (
        browser_settings.user_chrome_enabled() is False
        and browser_settings.desktop_enabled() is False
    )


@pytest.mark.parametrize(
    "raw,expected,warns",
    [
        (None, 7, False),
        ("14", 14, False),
        (" 7 ", 7, False),
        ("0", 7, True),
        ("-3", 7, True),
        ("1.5", 7, True),
        ("garbage", 7, True),
    ],
)
def test_retention_falls_back_to_the_default_but_accepts_a_positive_integer(
    raw: str | None, expected: int, warns: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(
        logging.WARNING, logger="looper.agents.browser.browser_settings"
    ):
        browser_settings.apply_stored(
            {browser_settings.RETENTION_DAYS_KEY: raw} if raw is not None else {}
        )
    assert browser_settings.retention_days() == expected
    assert browser_settings.view()["retention_days"] == expected
    if warns:
        assert [record.levelname for record in caplog.records] == ["WARNING"]
        assert browser_settings.RETENTION_DAYS_KEY in caplog.records[0].getMessage()
    else:
        assert caplog.records == []


def test_generation_counts_only_master_switch_off_transitions() -> None:
    assert browser_settings.generation() == 0
    browser_settings.set_enabled(True)
    browser_settings.set_enabled(True)
    assert browser_settings.generation() == 0
    browser_settings.set_enabled(False)
    assert browser_settings.generation() == 1
    browser_settings.set_enabled(False)
    assert browser_settings.generation() == 1
    browser_settings.set_enabled(True)
    browser_settings.set_enabled(False)
    assert browser_settings.generation() == 2
    browser_settings.apply_stored({browser_settings.ENABLED_KEY: "true"})
    assert browser_settings.generation() == 2
    browser_settings.apply_stored({})
    assert browser_settings.generation() == 3
    assert browser_settings.enabled() is False


@pytest.fixture
def _stored(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], list[dict[str, str | None]]]:
    stored: dict[str, str] = {}
    writes: list[dict[str, str | None]] = []

    async def read(keys: list[str], **_: object) -> dict[str, str]:
        return {key: stored[key] for key in keys if key in stored}

    async def write(values: Mapping[str, str | None], **_: object) -> None:
        writes.append(dict(values))
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(chat_settings.db_settings, "get_settings", read)
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", write)
    return stored, writes


def test_chat_settings_load_applies_the_stored_rows(
    _stored: tuple[dict[str, str], list[Any]],
) -> None:
    stored, _writes = _stored

    async def check() -> None:
        await chat_settings.load()  # no rows: all off
        assert browser_settings.view() == DEFAULTS
        stored.update(
            {
                browser_settings.ENABLED_KEY: "true",
                browser_settings.USER_CHROME_KEY: "false",
                browser_settings.DESKTOP_KEY: "true",
                browser_settings.RETENTION_DAYS_KEY: "30",
            }
        )
        await chat_settings.load()
        assert browser_settings.view() == {
            "enabled": True,
            "user_chrome": False,
            "desktop": True,
            "retention_days": 30,
        }
        stored[browser_settings.RETENTION_DAYS_KEY] = "not-a-number"
        await chat_settings.load()
        assert (
            browser_settings.retention_days() == 7
            and browser_settings.enabled() is True
        )

    import asyncio

    asyncio.run(check())


def test_update_persists_each_field_and_null_deletes_the_row(
    _stored: tuple[dict[str, str], list[dict[str, str | None]]],
) -> None:
    stored, writes = _stored

    async def check() -> None:
        view = await browser_settings.update({"enabled": True, "retention_days": 30})
        assert view == {
            "enabled": True,
            "user_chrome": False,
            "desktop": False,
            "retention_days": 30,
        }
        assert stored == {
            browser_settings.ENABLED_KEY: "true",
            browser_settings.RETENTION_DAYS_KEY: "30",
        }
        assert (
            browser_settings.enabled() is True
            and browser_settings.retention_days() == 30
        )
        await browser_settings.update({"user_chrome": True, "desktop": False})
        assert stored[browser_settings.USER_CHROME_KEY] == "true"
        assert stored[browser_settings.DESKTOP_KEY] == "false"
        # A partial null update resets only the fields it names; the other switches keep their values.
        view = await browser_settings.update({"enabled": None, "retention_days": None})
        assert view == {
            "enabled": False,
            "user_chrome": True,
            "desktop": False,
            "retention_days": 7,
        }
        assert browser_settings.ENABLED_KEY not in stored
        assert browser_settings.RETENTION_DAYS_KEY not in stored
        # Nulling the remaining rows restores every built-in default.
        assert (
            await browser_settings.update({"user_chrome": None, "desktop": None})
            == DEFAULTS
        )
        assert stored == {}
        assert len(writes) == 4 and all(isinstance(writer, dict) for writer in writes)

    import asyncio

    asyncio.run(check())


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"unknown": True},
        {"enabled": "true"},
        {"enabled": 1},
        {"user_chrome": None, "desktop": "yes"},
        {"retention_days": 0},
        {"retention_days": -1},
        {"retention_days": 1.5},
        {"retention_days": True},
        {"retention_days": "14"},
    ],
)
def test_update_rejects_bad_input_without_writing(
    _stored: tuple[dict[str, str], list[dict[str, str | None]]], changes: dict[str, Any]
) -> None:
    stored, writes = _stored

    async def check() -> None:
        with pytest.raises(ValueError):
            await browser_settings.update(changes)
        assert stored == {} and writes == []
        assert browser_settings.view() == DEFAULTS

    import asyncio

    asyncio.run(check())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_settings_round_trip_through_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await chat_settings.load()
        assert browser_settings.view() == DEFAULTS
        await pool.execute(
            "INSERT INTO system_settings (key, value) VALUES "
            "($1, 'true'), ($2, 'false'), ($3, 'true'), ($4, '21')",
            *KEYS,
        )
        await chat_settings.load()
        assert browser_settings.view() == {
            "enabled": True,
            "user_chrome": False,
            "desktop": True,
            "retention_days": 21,
        }
        await browser_settings.update({"enabled": None, "retention_days": None})
        rows = dict(
            await pool.fetch(
                "SELECT key, value FROM system_settings WHERE key = ANY($1::text[])",
                list(KEYS),
            )
        )
        assert rows == {
            browser_settings.USER_CHROME_KEY: "false",
            browser_settings.DESKTOP_KEY: "true",
        }
        assert browser_settings.enabled() is False
        assert (
            browser_settings.retention_days() == browser_settings.DEFAULT_RETENTION_DAYS
        )

    run_db(test, monkeypatch, timeout=30)


def test_a_failed_settings_read_turns_every_switch_off_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(keys: list[str], **_: object) -> dict[str, str]:
        raise OSError("database unavailable")

    monkeypatch.setattr(chat_settings.db_settings, "get_settings", boom)

    async def check() -> None:
        with caplog.at_level(
            logging.WARNING, logger="looper.agents.browser.browser_settings"
        ):
            with pytest.raises(OSError, match="database unavailable"):
                await chat_settings.load()
        assert browser_settings.view() == DEFAULTS
        assert browser_settings.generation() == 0
        browser_settings.apply_stored(
            {
                browser_settings.ENABLED_KEY: "true",
                browser_settings.USER_CHROME_KEY: "true",
                browser_settings.DESKTOP_KEY: "true",
                browser_settings.RETENTION_DAYS_KEY: "30",
            }
        )
        caplog.clear()
        with caplog.at_level(
            logging.WARNING, logger="looper.agents.browser.browser_settings"
        ):
            with pytest.raises(OSError, match="database unavailable"):
                await chat_settings.load()
        assert browser_settings.view() == DEFAULTS
        assert browser_settings.generation() == 1
        assert [record.levelname for record in caplog.records] == ["WARNING"]
        assert "leaving them off" in caplog.records[0].getMessage()

    import asyncio

    asyncio.run(check())


def test_browser_settings_are_not_chat_settings(
    _stored: tuple[dict[str, str], list[dict[str, str | None]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, writes = _stored
    monkeypatch.setattr(
        chat_settings.db_ledger, "enabled", AsyncMock(return_value=False)
    )

    async def check() -> None:
        await chat_settings.load()
        assert chat_settings.FIELDS == (
            "stop_enabled",
            "completion_turns",
            "research_order",
            "ledger_enabled",
        )
        assert await chat_settings.settings_view() == {
            "stop_enabled": False,
            "completion_turns": "off",
            "research_order": False,
            "ledger_enabled": False,
        }
        for body in ({"enabled": True}, {"browser_enabled": True}, {"retention_days": 7}):
            with pytest.raises(ValueError, match="unknown setting"):
                chat_settings.checked(body)
            with pytest.raises(ValueError, match="unknown setting"):
                await chat_settings.update(body)
        assert writes == []
        assert browser_settings.view() == DEFAULTS

    import asyncio

    asyncio.run(check())


def test_no_browser_tool_or_operation_exists_while_the_capability_is_off() -> None:
    from looper.agents.api_v1 import operations

    manager_names = [tool.name for tool in service.MANAGER_TOOLS]
    planner_names = [tool.name for tool in (*planner.TOOLS, *planner.ROLE_TOOLS)]
    worker_names = [
        tool.name for tool in (*room.tools_for(MemberKind.WORK), *vault.TOOLS)
    ]
    assert all(
        "browser" not in name
        for name in (*manager_names, *planner_names, *worker_names)
    )
    assert all("browser" not in operation.path for operation in operations())
    # The browser package exposes settings only; it registers no ops Tool.
    import pkgutil

    from looper.agents import ops

    assert "browser" not in {info.name for info in pkgutil.iter_modules(ops.__path__)}


# Captured on origin/main a2876936f. Wiring these settings must not change a model-facing tool name or listing.
MAIN_BASELINE = {
    "manager:names": "e21c252de1cf2e317a45e2af4678878855c2a8c93c2e0deb5555c684cb396324",
    "manager:listings": "88d1de01be909942e37135699521f10c08fc78e9e090a74f3194c8a1a95d059d",
    "planner:names": "04fb21cb7613c78265b058dc2e8cb8ba2771aca4868820d7513b073f5d5e4597",
    "worker:names": "aa369e1fc795f7cc7a6bea761a545f21b5aeb04982af0579a395e75af1dcc3cc",
}


def test_model_facing_tool_lists_remain_byte_identical_to_main() -> None:
    observed = {
        "manager:names": _hash([tool.name for tool in service.MANAGER_TOOLS]),
        "manager:listings": _hash(
            {tool.name: _hash(tool.listing()) for tool in service.MANAGER_TOOLS}
        ),
        "planner:names": _hash(
            [tool.name for tool in (*planner.TOOLS, *planner.ROLE_TOOLS)]
        ),
        "worker:names": _hash(
            [tool.name for tool in (*room.tools_for(MemberKind.WORK), *vault.TOOLS)]
        ),
    }
    assert observed == MAIN_BASELINE


# The package version is redacted so a release bump does not look like an API change.
# Captured on origin/main a2876936f; this change does not edit the operation catalog.
OPENAPI_BASELINE = "87b3ae1ada46d5b7269dd367082f136828cb2f73ada75d4c4450545f51d99c08"


def _openapi_bytes() -> bytes:
    from looper.agents import api_v1

    document = api_v1.document()
    info = dict(document["info"])
    info["version"] = "<version>"
    body = {**document, "info": info}
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def test_openapi_document_stays_byte_identical_to_main() -> None:
    assert hashlib.sha256(_openapi_bytes()).hexdigest() == OPENAPI_BASELINE
