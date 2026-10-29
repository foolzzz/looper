"""Manager idle retention configuration on disposable PostgreSQL schemas."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from looper.agents import config_transfer as transfer, managers_api
from looper.db import agent_sessions, managers
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _create(**fields: Any) -> dict[str, Any]:
    return (await managers_api.create_manager({"key": "idle", "name": "Idle", "system_prompt": "Help.", **fields}))["manager"]


async def _import(pool: Any, entry: dict[str, Any], *, apply: bool = True) -> Any:
    async with pool.acquire() as conn:
        return await transfer.run_import(conn, {"format": transfer.V4_FORMAT, "managers": [entry]}, apply=apply)


@pytest.mark.parametrize("timeout", [0, 60, 86400])
def test_idle_timeout_storage_accepts_boundaries(timeout: int, monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        made = await _create(idle_timeout_seconds=timeout)
        assert made["idle_timeout_seconds"] == timeout
        listed = await managers_api.managers_view()
        assert {m["id"]: m["idle_timeout_seconds"] for m in listed["managers"]} == {
            "manager": 0, "manager-idle": timeout}

    run_db(test, monkeypatch, timeout=10)


@pytest.mark.parametrize("timeout", [-1, 59, 86401, None])
def test_idle_timeout_database_rejects_invalid_values(timeout: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _create()
        error = asyncpg.NotNullViolationError if timeout is None else asyncpg.CheckViolationError
        with pytest.raises(error):
            await pool.execute("UPDATE managers SET idle_timeout_seconds = $1 WHERE id = 'manager-idle'", timeout)

    run_db(test, monkeypatch, timeout=10)


def test_default_manager_timeout_must_stay_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        assert (await managers_api.update_manager("manager", {"idle_timeout_seconds": 0}))["manager"]["idle_timeout_seconds"] == 0
        with pytest.raises(managers_api.RequestError):
            await managers_api.update_manager("manager", {"idle_timeout_seconds": 60})
        with pytest.raises(asyncpg.CheckViolationError, match="managers_default_check"):
            await pool.execute("UPDATE managers SET idle_timeout_seconds = 60 WHERE id = 'manager'")

    run_db(test, monkeypatch, timeout=10)


def test_idle_timeout_transfer_preview_and_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        made = await _create(idle_timeout_seconds=60)
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            exported = json.loads("".join([part async for part in transfer.export_chunks(
                conn, include_projects=False, include_secrets=False)]))
        assert {m["id"]: m["idle_timeout_seconds"] for m in exported["managers"]} == {
            "manager": 0, "manager-idle": 60}
        entry = next(m for m in exported["managers"] if m["id"] == made["id"])
        entry["idle_timeout_seconds"] = 86400
        preview = await _import(pool, entry, apply=False)
        change = next(c for c in preview.view()["changes"] if c["section"] == "managers")
        assert change["fields"] == ["idle_timeout_seconds"]
        assert (await managers.get_manager(made["id"]))["idle_timeout_seconds"] == 60
        applied = await _import(pool, entry)
        assert applied.view() == {**preview.view(), "applied": True}
        assert (await managers.get_manager(made["id"]))["idle_timeout_seconds"] == 86400
        assert not (await _import(pool, entry)).view()["changes"]

    run_db(test, monkeypatch, timeout=20)


def test_old_transfer_keeps_existing_timeout_and_defaults_new_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _create(idle_timeout_seconds=0)
        legacy = {"id": "manager-idle", "name": "Idle", "system_prompt": "Help."}
        await _import(pool, legacy)
        assert (await managers.get_manager("manager-idle"))["idle_timeout_seconds"] == 0
        await _import(pool, {**legacy, "id": "manager-new", "name": "New"})
        assert (await managers.get_manager("manager-new"))["idle_timeout_seconds"] == 1800
        await _import(pool, {"id": "manager", "name": "Manager"})
        assert (await managers.get_manager("manager"))["idle_timeout_seconds"] == 0

    run_db(test, monkeypatch, timeout=20)


def test_timeout_edit_and_transfer_leave_session_identity_and_usage_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        profile = await pool.fetchval("SELECT id FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1")
        await _create(profile_id=profile)
        await agent_sessions.select_profile("manager-idle", profile)
        await agent_sessions.save_session("manager-idle", "saved-session", {"input_tokens": 100, "cost_usd": 0.25},
                                          runtime_type="claude", profile_id=profile)
        before = copy.deepcopy(await agent_sessions.get_session("manager-idle"))
        await managers_api.update_manager("manager-idle", {"idle_timeout_seconds": 60})
        await _import(pool, {"id": "manager-idle", "name": "Idle", "idle_timeout_seconds": 0})
        assert await agent_sessions.get_session("manager-idle") == before
        assert (await managers.get_manager("manager-idle"))["profile_id"] == profile

    run_db(test, monkeypatch, timeout=20)


def test_suspended_manager_restart_resumes_persisted_profile_and_incremental_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, channel_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from tests.test_manager_reply import _fake_claude, _resident

    async def test(pool: Any) -> None:
        profile = "agent-claude-code-opus"
        row = await _create(profile_id=profile, idle_timeout_seconds=60)
        await agent_sessions.select_profile(row["id"], profile)
        binary = _fake_claude(tmp_path, _resident())
        definition = managers_api.ManagerDefinition.from_row(row)
        bus = MessageBus(runtime_root=tmp_path)
        first = ManagerAgent(config=ManagerConfig(claude_bin=binary, work_dir=tmp_path), runtime_root=tmp_path,
                             definition=definition, clock=lambda: 0.0)
        second = None

        async def ask(agent: ManagerAgent, text: str) -> None:
            await agent._handle(MessageEnvelope.create(
                source=channel_address("web"), target=agent.agent_address,
                message_type=MessageType.USER_MESSAGE, payload={"text": text}))

        try:
            await first.start(bus)
            await ask(first, "remember amber")
            saved = await agent_sessions.get_session(row["id"])
            assert saved["session_id"] == "sess-1" and saved["selected_profile_id"] == profile
            assert saved["profile_id"] == profile and saved["session_totals"]["cost_usd"] == 0.25
            assert await first.try_suspend_idle(60)
            assert await agent_sessions.get_session(row["id"]) == saved
            await first.stop()
            second = ManagerAgent(config=ManagerConfig(claude_bin=binary, work_dir=tmp_path), runtime_root=tmp_path,
                                  definition=definition, clock=lambda: 60.0)
            await second.start(bus)
            assert second._session_id == saved["session_id"] and second._session_totals.cost_usd == 0.25
            await ask(second, "what color")
            assert "--resume" in second._claude.command
            after = await agent_sessions.get_session(row["id"])
            assert after["session_id"] == saved["session_id"] and after["selected_profile_id"] == profile
            assert after["runtime_type"] == saved["runtime_type"] and after["provider_id"] == saved["provider_id"]
            assert after["session_totals"]["cost_usd"] == 0.5
            costs = await pool.fetch("SELECT cost_usd FROM llm_records WHERE agent_id = $1 ORDER BY id", row["id"])
            assert [float(r["cost_usd"]) for r in costs] == [0.25, 0.25]
        finally:
            await first.stop()
            if second is not None:
                await second.stop()
            await bus.shutdown()

    run_db(test, monkeypatch, timeout=20)
