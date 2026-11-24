"""Persist the input policy version so unsafe routed histories cannot be resumed after upgrade."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import agent_sessions, connection, manager_sessions, user_gates
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import _Rig, _Resident, _lark, routed
from tests.lark_cli_stand_in import OTHER

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _session(**extra):
    return {"manager_id": "manager", "session_key": "group:channel-lark:oc_synthetic_policy", "kind": "group",
            "channel_id": "channel-lark", "chat": "oc_synthetic_policy", "session_id": "session-policy",
            "session_totals": {}, "runtime_type": "claude-code", "profile_id": None, "provider_id": None,
            "clean_start": False, "pending_handoff": None, **extra}


def test_context_policy_version_is_stored_and_legacy_callers_default_to_zero(monkeypatch):
    async def check(pool):
        row = _session()
        await manager_sessions.save(row)
        assert (await manager_sessions.get("manager", row["session_key"]))["context_policy_version"] == 0
        await manager_sessions.save(row | {"context_policy_version": 1})
        assert (await manager_sessions.get("manager", row["session_key"]))["context_policy_version"] == 1
        assert (await manager_sessions.list("manager"))[0]["context_policy_version"] == 1
        await manager_sessions.save(row)
        assert (await manager_sessions.get("manager", row["session_key"]))["context_policy_version"] == 0

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("kind", [SessionKind.GROUP, SessionKind.DM])
def test_a_legacy_routed_history_starts_fresh_without_a_handoff(routed, tmp_path, kind):
    async def check(pool):
        row = _session(kind=kind.value, session_key=f"{kind.value}:channel-lark:oc_synthetic_policy",
                       pending_handoff="PRIVATE_LEGACY_HANDOFF")
        await manager_sessions.save(row)
        rig = _Rig(tmp_path)
        route = SessionKey(kind, row["channel_id"], row["chat"])
        resident = _Resident("session-safe")
        rig.manager._router.session(route).claude = resident
        handoff = AsyncMock(side_effect=AssertionError("legacy models cannot write a handoff"))
        routed.setattr(rig.manager, "_handoff", handoff)
        await rig.start()
        try:
            question = _lark("Hello", row["chat"], OTHER, group=kind is SessionKind.GROUP)
            await rig.bus.publish(question)
            await rig.reply(question)
            assert len(resident.turns) == 1
            assert "PRIVATE_LEGACY_HANDOFF" not in resident.turns[0]
            assert row["session_id"] not in resident.starts[0]
            handoff.assert_not_awaited()
            saved = await manager_sessions.get("manager", route.text)
            assert saved["session_id"] == "session-safe"
            assert saved["pending_handoff"] is None
            assert saved["context_policy_version"] == 1
        finally:
            await rig.close()

    run_db(check, routed, timeout=60)


@pytest.mark.parametrize("kind", [SessionKind.GROUP, SessionKind.DM])
def test_an_allowlisted_history_and_its_handoff_survive_load_and_save(monkeypatch, tmp_path, kind):
    async def check(pool):
        row = _session(kind=kind.value, session_key=f"{kind.value}:channel-lark:oc_synthetic_policy",
                       pending_handoff="Own-chat handoff", context_policy_version=1)
        await manager_sessions.save(row)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        session = manager._router.session(SessionKey(kind, row["channel_id"], row["chat"]))
        await manager._load_session_state(session)
        assert session.session_id == row["session_id"]
        assert session.pending_handoff == row["pending_handoff"]
        await manager._write_session_state(session=session)
        stored = await manager_sessions.get("manager", session.key)
        assert stored["session_id"] == row["session_id"]
        assert stored["pending_handoff"] == row["pending_handoff"]
        assert stored["context_policy_version"] == 1

    run_db(check, monkeypatch, timeout=30)


def test_main_history_and_handoff_remain_in_the_original_store(monkeypatch, tmp_path):
    async def check(pool):
        await agent_sessions.save_session("manager", "main-saved", {}, runtime_type="claude-code",
                                          pending_handoff="Operator's handoff")
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        await manager._load_session_state()
        session = manager._session_now()
        assert session.session_id == "main-saved"
        assert session.pending_handoff == "Operator's handoff"
        await manager._write_session_state()
        stored = await agent_sessions.get_session("manager")
        assert stored["session_id"] == "main-saved"
        assert stored["pending_handoff"] == "Operator's handoff"
        assert await manager_sessions.list("manager") == []

    run_db(check, monkeypatch, timeout=30)


def test_other_direct_chat_gate_command_cannot_query_global_gates(routed, tmp_path):
    async def check(pool):
        rig = _Rig(tmp_path)
        rig.manager.ops = ManagerOps(
            Platform(bus=rig.bus, task_router=SimpleNamespace(has_planner=lambda task_id: False)),
            url="http://127.0.0.1:9/", token="synthetic-policy-token")
        gates = AsyncMock(side_effect=AssertionError("other DM must not list global gates"))
        routed.setattr(user_gates, "open_gates", gates)
        await rig.start()
        try:
            message = _lark("/gates", "oc_synthetic_gate_dm", OTHER, group=False)
            await rig.bus.publish(message)
            assert await rig.reply(message) == "此会话无法读取任务或待办，请操作员在自己的会话中查看。"
            gates.assert_not_called()
            assert not rig.main.turns
        finally:
            await rig.close()

    run_db(check, routed, timeout=30)


def test_upgrade_marks_existing_histories_legacy_and_is_idempotent(monkeypatch):
    async def check(pool):
        row = _session(pending_handoff="legacy history must be discarded")
        await manager_sessions.save(row)
        await pool.execute("ALTER TABLE manager_sessions DROP COLUMN context_policy_version")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'manager_session_context_policy'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        stored = await manager_sessions.get("manager", row["session_key"])
        assert stored["context_policy_version"] == 0
        assert stored["session_id"] == row["session_id"]
        assert stored["pending_handoff"] == row["pending_handoff"]
        await manager_sessions.save(row | {"context_policy_version": 1})
        await connection.ensure_schema(pool)
        assert (await manager_sessions.get("manager", row["session_key"]))["context_policy_version"] == 1
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)

    run_db(check, monkeypatch, timeout=30)


def test_unclassified_injected_notes_are_not_a_routed_input_grant(routed, tmp_path):
    async def check(pool):
        rig = _Rig(tmp_path)
        route = SessionKey(SessionKind.DM, "channel-lark", "oc_synthetic_notes")
        resident = _Resident("session-notes")
        session = rig.manager._router.session(route)
        session.claude = resident
        await rig.start()
        current = rig.manager._current.set(session)
        try:
            envelope = _lark("Hello", route.chat, OTHER, group=False)
            await rig.manager._run_turn("Hello", envelope, note="PRIVATE_UNCLASSIFIED_NOTE")
            assert len(resident.turns) == 1
            assert "Hello" in resident.turns[0]
            assert "PRIVATE_UNCLASSIFIED_NOTE" not in resident.turns[0]
        finally:
            rig.manager._current.reset(current)
            await rig.close()

    run_db(check, routed, timeout=30)
