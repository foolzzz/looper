"""Boundary guards of scoped chat sessions: tool calls, completion principal, refresh report, saved scope."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_scope_alerts, chat_scope_metrics, chat_scopes, session_visibility
from looper.agents.ops import Turn, TurnOrigin, session_tools
from looper.agents.ops.service import ManagerOps, Platform
from looper.db import manager_sessions
from tests.db_support import DSN, run_db
from tests.test_scoped_launch_db import AgentLibrary, ItemKind, LoadedItem
from tests.test_scoped_routing_db import GROUP_A, LARK, MANAGER, OPERATOR, OTHER, _message, _send, _setup

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
KEY = "group:channel-lark:synthetic-team"
TOOL = "looper_chat_task_create"
ARGUMENTS = {"title": "Shared task", "requirement": "Do the work", "project": "Synthetic"}


def _grant(monkeypatch: Any, ops: ManagerOps, tools: frozenset[str]) -> None:
    manager_id = ops._scope.manager_id
    # A private registry, put back with the test's other patches.
    monkeypatch.setattr(session_visibility, "_SCOPED", {**session_visibility._SCOPED})
    session_visibility.register_scoped(manager_id, KEY, replace(session_visibility.SCOPED_CHAT_ALLOWLIST, tools=tools))


@pytest.mark.parametrize("sender,allowed", [("synthetic-operator", True), ("synthetic-other", False)])
def test_granted_tool_in_a_scoped_session_takes_the_checked_path(monkeypatch, tmp_path, sender, allowed):
    async def body(pool):
        async def operator(_): return "synthetic-operator"
        monkeypatch.setattr(session_tools, "operator_sender_id", operator)
        await pool.execute("INSERT INTO projects (id,name,work_dir) VALUES ('synthetic-project','Synthetic',$1)",
                           str(tmp_path))
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic")
        _grant(monkeypatch, ops, frozenset({TOOL}))
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=True,
                    message_id="synthetic-message", sender=sender, origin=TurnOrigin.USER)
        ops.begin_turn(turn, binding="synthetic-binding", session_key=KEY)
        answer = await ops.call(TOOL, ARGUMENTS, binding="synthetic-binding", session_key=KEY)
        tasks = await pool.fetchval("SELECT count(*) FROM tasks")
        if allowed:
            assert answer["status"] == "ok", answer
            assert tasks == 1
            assert (await pool.fetchrow("SELECT origin_session FROM tasks"))["origin_session"] == KEY
        else:
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", answer
            assert tasks == 0 and not ops._pending  # refused before any side effect, nothing waits to confirm
            assert await pool.fetchval("SELECT status FROM manager_actions WHERE tool=$1", TOOL) == "error"
    run_db(body, monkeypatch)


def test_granted_tool_without_a_session_implementation_is_out_of_scope(monkeypatch):
    async def body(pool):
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic")
        _grant(monkeypatch, ops, frozenset({"looper_task_create"}))
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=True,
                    message_id="synthetic-message", sender="synthetic-operator", origin=TurnOrigin.USER)
        ops.begin_turn(turn, binding="synthetic-binding", session_key=KEY)
        answer = await ops.call("looper_task_create", {"project": "p", "requirement": "r"},
                                binding="synthetic-binding", session_key=KEY)
        assert answer["error"]["reason"] == "out_of_scope", answer
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(body, monkeypatch)


@pytest.mark.parametrize("sender,kind", [(OTHER, chat_scopes.PrincipalKind.PERSON), (None, chat_scopes.PrincipalKind.BOT)])
def test_completion_principal_is_a_person_only_with_a_recorded_sender(monkeypatch, tmp_path, sender, kind):
    async def body(pool):
        rig, _ = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        router = rig.manager._router
        seen = []
        resolve = router._resolve_scope

        async def spy(origin, principal_kind):
            seen.append(principal_kind)
            return await resolve(origin, principal_kind)

        monkeypatch.setattr(router, "_resolve_scope", spy)
        await router.completion_key(dict(channel="lark", channel_id=LARK.agent_id, chat=GROUP_A, group=True,
                                         sender=sender))
        assert seen == [kind]
    run_db(body, monkeypatch, timeout=60)


def test_refresh_invariant_is_reported_once(monkeypatch, tmp_path):
    async def body(pool):
        rig, _ = await _setup(pool, tmp_path, monkeypatch)
        reports = []
        real_alert, real_metric = chat_scope_alerts.invariant_violation, chat_scope_metrics.invariant

        async def alert(*args, **kwargs):
            reports.append(("alert", args[1]))
            return await real_alert(*args, **kwargs)

        def metric(*args, **kwargs):
            reports.append(("metric", args[1]))
            return real_metric(*args, **kwargs)

        monkeypatch.setattr(chat_scope_alerts, "invariant_violation", alert)
        monkeypatch.setattr(chat_scope_metrics, "invariant", metric)
        await rig.start()
        try:
            await _send(rig, "first")
            member = next(s for s in rig.manager._sessions.values() if s.scope is not None)
            member.claude.pid = 12345
            monkeypatch.setattr(member.claude, "try_stop_if_idle", lambda: _false())
            rig.manager._current.set(member)
            original = rig.manager._prepare_turn

            async def changed(*args, **kwargs):
                prepared = await original(*args, **kwargs)
                library = AgentLibrary(items=(LoadedItem(ItemKind.SKILL, "synthetic-refresh", "b" * 64),))
                return replace(prepared, launch=replace(prepared.launch, library=library), switch=None,
                               signature=(*prepared.signature[:-2], "different-fingerprint", prepared.signature[-1]))

            monkeypatch.setattr(rig.manager, "_prepare_turn", changed)
            assert not reports  # positive control: the first turn reported nothing
            await _send(rig, "refresh mismatch")
            assert sorted(reports) == [("alert", "launch"), ("metric", "launch")]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


async def _false() -> bool:
    return False


# The key embeds the fingerprint and the audience is tied to the scope by constraints, so the id and the version
# are the fields a stored row can disagree on.
@pytest.mark.parametrize("field,stale", [("scope_version", 999), ("scope_id", "another_scope")])
def test_saved_row_with_other_scope_fields_is_reopened_not_restored(monkeypatch, tmp_path, caplog, field, stale):
    async def body(pool):
        rig, _ = await _setup(pool, tmp_path, monkeypatch)
        session = await rig.manager._router.target(_message(rig, "load"))
        scope = session.scope
        row = {"manager_id": MANAGER, "session_key": session.key, "kind": "group", "channel_id": LARK.agent_id,
               "chat": GROUP_A, "session_id": "saved-model-session", "session_totals": {}, "runtime_type": None,
               "profile_id": None, "provider_id": None, "clean_start": False, "pending_handoff": None,
               "activity_cursor": None, "context_policy_version": session_visibility.for_session(
                   session.key, manager_id=MANAGER).version, "scope_id": scope.resolved.scope_id,
               "scope_version": scope.version, "audience": scope.resolved.audience.value,
               "scope_fingerprint": scope.fingerprint}
        # Positive control: a row of the same scope is restored.
        await manager_sessions.save(row)
        session.session_loaded, session.session_id = False, None
        await rig.manager._load_session_state(session)
        assert session.session_id == "saved-model-session"
        # The same row with one scope field changed is not.
        await manager_sessions.save({**row, field: stale})
        session.session_loaded, session.session_id = False, None
        with caplog.at_level(logging.ERROR):
            await rig.manager._load_session_state(session)
        assert session.session_id is None
        assert (await manager_sessions.get(MANAGER, session.key))["session_id"] is None
        assert any("saved scope disagrees" in r.getMessage() for r in caplog.records)
    run_db(body, monkeypatch, timeout=60)
