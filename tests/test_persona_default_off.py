"""Unset persona changes no block, no settings tool, and no configuration file.

The Manager prompt is not wired in this change, so the off check is the parts that are: the section helper,
``looper_settings``, the operator tool listing, and configuration export.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from looper.agents import persona
from looper.agents.config_transfer import ExportSummary, export_chunks
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.ops.settings import TOOLS
from tests.db_support import DSN, run_db

# looper_settings Tool.listing() on main before this change, sort_keys, ensure_ascii=False, separators=(",", ":").
SETTINGS_LISTING_SHA256 = "f32c5f0a9c71560b91bf0fe897fe05b1fd96769b06d8a03d5f5f3055f7c1439b"


def _listing_blob() -> str:
    tool = next(item for item in TOOLS if item.name == "looper_settings")
    return json.dumps(tool.listing(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_an_unset_persona_does_not_change_a_block() -> None:
    block = ["Projects:", "  alpha", "", "Ready tasks:", "  none", ""]
    assert persona.section(persona.Persona()) == []
    bare = hashlib.sha256("\n".join(block).encode()).hexdigest()
    with_section = hashlib.sha256("\n".join([*persona.section(persona.Persona()), *block]).encode()).hexdigest()
    assert bare == with_section


def test_looper_settings_does_not_list_persona() -> None:
    blob = _listing_blob()
    assert "persona" not in blob
    assert hashlib.sha256(blob.encode()).hexdigest() == SETTINGS_LISTING_SHA256


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_an_operator_listing_and_an_empty_export_omit_persona(monkeypatch) -> None:
    async def test(pool: Any) -> None:
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
        listed = json.dumps(await ops.current_listing(Turn(channel="web", chat="chat-1", message_id="m-1")),
                            ensure_ascii=False)
        assert "persona" not in listed

        summary = ExportSummary()
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            text = "".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False, summary=summary)])
        data = json.loads(text)
        assert list(data["settings"]) == ["layers", "roles"]
        assert "system" not in data["settings"]
        assert "manager.persona" not in json.dumps(data["settings"])
        assert "settings.system" not in summary.counts

    run_db(test, monkeypatch, timeout=60)
