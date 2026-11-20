"""A bot's ``operator_sender_id`` (message-leads-intents RFC, section 5): set through the channel API, checked like the
allowlist it must be on, read by ``operator_identity``, and carried by the configuration's export and import.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.operator_identity import operator_sender_id
from tests.db_support import DSN, run_with_channel_configs
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_lark_channel_db import _call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TELEGRAM = {"allowed_user_ids": ["12345", "67890"]}
LARK = {"allowed_open_ids": [OPEN_ID]}


async def _put(channel_id: str, config: dict[str, Any]) -> tuple[int, Any]:
    return await _call("PUT", channel_id, {"config": config})


def test_a_telegram_bot_names_an_allowlisted_sender_as_its_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        status, answer = await _put("channel-telegram", {**TELEGRAM, "operator_sender_id": "67890"})
        assert status == 200, answer
        assert answer["config"]["operator_sender_id"] == "67890"
        assert await operator_sender_id("channel-telegram") == "67890"
        # Cleared with the rest of the configuration: no operator, and no turn is the operator's.
        status, answer = await _put("channel-telegram", dict(TELEGRAM))
        assert status == 200 and "operator_sender_id" not in answer["config"]
        assert await operator_sender_id("channel-telegram") is None

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("config", [
    {**TELEGRAM, "operator_sender_id": "-100123"},       # a group's id, not a sender's
    {**TELEGRAM, "operator_sender_id": "11111"},         # a sender, not on the allowlist
    {"operator_sender_id": "12345"},                     # no allowlist to be on
])
def test_a_telegram_operator_outside_the_allowlist_is_refused(monkeypatch: pytest.MonkeyPatch,
                                                             config: dict[str, Any]) -> None:
    async def test(pool: Any) -> None:
        await _put("channel-telegram", dict(TELEGRAM))
        status, answer = await _put("channel-telegram", config)
        assert status == 400 and "operator_sender_id" in answer["error"], answer
        assert config["operator_sender_id"] not in answer["error"]  # a refusal never repeats what was sent
        assert await operator_sender_id("channel-telegram") is None

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("operator, accepted", [(OPEN_ID, True), (OTHER, False), ("12345", False)])
def test_a_lark_bot_names_its_operator_by_an_allowlisted_open_id(
    monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path, operator: str, accepted: bool,
) -> None:
    async def test(pool: Any) -> None:
        status, answer = await _put("channel-lark", {**LARK, "operator_sender_id": operator})
        if accepted:
            assert status == 200, answer
            assert await operator_sender_id("channel-lark") == operator
        else:
            assert status == 400 and "operator_sender_id" in answer["error"], answer
            assert await operator_sender_id("channel-lark") is None

    run_with_channel_configs(test, monkeypatch)


def test_the_export_carries_the_operator_and_the_import_restores_it(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    from looper.agents.config_transfer import ConfigFileError, run_import, write_export

    async def test(pool: Any) -> None:
        status, answer = await _put("channel-telegram", {**TELEGRAM, "operator_sender_id": "12345"})
        assert status == 200, answer
        path = tmp_path / "export.json"
        async with pool.acquire() as conn:
            await write_export(conn, path, include_projects=False, include_secrets=False)
        document = json.loads(path.read_text())
        (entry,) = [c for c in document["channels"] if c["channel_id"] == "channel-telegram"]
        assert entry["config"]["operator_sender_id"] == "12345"  # not a secret: shown, never a placeholder

        await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-telegram'",
                           json.dumps(TELEGRAM))
        async with pool.acquire() as conn:
            await run_import(conn, document, apply=True)
        assert await operator_sender_id("channel-telegram") == "12345"

        # A file naming an operator its allowlist lacks is refused, and the channel stays as it is.
        entry["config"]["operator_sender_id"] = "99999"
        async with pool.acquire() as conn:
            with pytest.raises(ConfigFileError, match="operator_sender_id"):
                await run_import(conn, document, apply=True)
        assert await operator_sender_id("channel-telegram") == "12345"

    run_with_channel_configs(test, monkeypatch)
