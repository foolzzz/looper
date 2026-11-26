"""Scoped turn authority and the stored-only routing switch."""

import asyncio

import pytest

from looper.agents import chat_scopes, managers_api
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.ops.service import _operator_turn, operator_call
from looper.agents.v1_domains.manager_sessions import parse_key
from looper.agents.v1_domains import scenario_session_routing
from tests.db_support import DSN, run_db


@pytest.mark.parametrize(("audience", "origin", "expected"), [
    (chat_scopes.ScopeAudience.OPERATOR, TurnOrigin.USER, True),
    (chat_scopes.ScopeAudience.SHARED, TurnOrigin.USER, False),
    (chat_scopes.ScopeAudience.OPERATOR, TurnOrigin.COMPLETION, False),
    (None, TurnOrigin.USER, True),
])
def test_operator_turn_reads_the_principal(audience, origin, expected, monkeypatch) -> None:
    from looper.agents.ops import service

    async def operator(channel):
        return "synthetic-operator"

    monkeypatch.setattr(service, "operator_sender_id", operator)
    principal = (chat_scopes.Principal(audience, chat_scopes.PrincipalKind.PERSON,
                  chat_scopes.FULL if audience is chat_scopes.ScopeAudience.OPERATOR else chat_scopes.CHAT_ONLY, 1)
                 if audience is not None else None)
    turn = Turn(channel="web" if principal is None else "lark", chat="synthetic-group",
                message_id="synthetic-message", group=principal is not None, principal=principal, origin=origin)
    assert asyncio.run(_operator_turn(turn)) is expected


def test_operator_api_call_keeps_its_authority() -> None:
    assert asyncio.run(operator_call(Turn(channel="api", chat=None)))
    assert not asyncio.run(operator_call(Turn(channel="api", chat=None, client_id="synthetic-client")))


@pytest.mark.parametrize("text", ["group:channel-lark:synthetic~0123456789abcdef", "dm:bot:chat~bad", "main~bad"])
def test_default_manager_session_operations_reject_fingerprints(text) -> None:
    with pytest.raises(ValueError):
        parse_key(text)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_scoped_is_not_selectable_yet(monkeypatch) -> None:
    async def body(pool) -> None:
        from looper.agents.projects import RequestError

        await pool.execute("INSERT INTO managers (id, name, system_prompt) "
                           "VALUES ('manager-scoped', 'Synthetic', 'Use the synthetic scenario contract.')")
        await pool.execute("UPDATE managers SET session_routing = 'scoped' WHERE id = 'manager-scoped'")
        assert await managers_api.scenario_session_routing_view("manager-scoped") == {
            "manager_id": "manager-scoped", "session_routing": "scoped"}
        with pytest.raises(RequestError, match="session_routing must be off, per_chat or null"):
            await managers_api.update_scenario_session_routing("manager-scoped", "scoped")
        update = next(op for op in scenario_session_routing.OPERATIONS
                      if op.operation_id == "scenario_session_routing.update")
        assert update.request["properties"]["session_routing"]["enum"] == ["off", "per_chat", None]
        assert "scoped" in scenario_session_routing.VIEW["properties"]["session_routing"]["enum"]

    run_db(body, monkeypatch)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_turn_principal_mismatch_answers_without_starting_a_resident(monkeypatch, tmp_path) -> None:
    from tests.test_manager_session_routing_db import GROUP_A, OTHER, _Resident, _lark, _published
    from tests.test_scenario_session_routing_db import _scenario, _to

    async def body(pool) -> None:
        rig = await _scenario(pool, tmp_path, "scoped")
        message = _to(rig, _lark("synthetic question", GROUP_A, OTHER, group=True))
        session = await rig.manager._router.target(message)
        resident = _Resident("synthetic-session")
        session.claude = resident

        def reject(envelope, current):
            raise chat_scopes.ScopeInvariantViolation("synthetic principal mismatch")

        monkeypatch.setattr(rig.manager._router, "principal", reject)
        await rig.start()
        try:
            reply = await rig.reply(await _published(rig, message))
            assert reply == "这条消息没有处理：范围检查没有通过。"
            assert not resident.starts and not resident.turns and not rig.main.turns
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=45)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_completion_handler_routes_the_recorded_group_requester(monkeypatch, tmp_path) -> None:
    from looper.agents import chat_settings, completion_events
    from looper.db import manager_events
    from tests.db_support import until
    from tests.test_manager_session_routing_db import GROUP_A, LARK, OTHER
    from tests.test_scenario_session_routing_db import _scenario

    async def body(pool) -> None:
        rig = await _scenario(pool, tmp_path, "scoped")
        operator = "synthetic-operator"
        await pool.execute("UPDATE channel_configs SET config = jsonb_build_object('operator_sender_id', $1::text) "
                           "WHERE channel_id = $2", operator, LARK.agent_id)
        await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'legacy', legacy_allowed = true "
                           "WHERE channel_id = $1 AND chat_id = $2", LARK.agent_id, GROUP_A)
        monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
        rig.manager._bus, rig.manager._running = rig.bus, True
        observed = []
        active = None

        async def pending(*args, **kwargs):
            return active

        async def claim(*args, **kwargs):
            nonlocal active
            row, active = active, None
            return [row]

        async def tell(rows, batch):
            session = rig.manager._session_now()
            observed.append((session.key, session.scope.resolved.scope_id, session.scope.resolved.audience))

        # Group completions remain skipped in today's event store. Supply future group events while
        # exercising the real handler, scope database, transaction, dispatch and session worker.
        monkeypatch.setattr(manager_events, "pending_one", pending)
        monkeypatch.setattr(manager_events, "claim", claim)
        monkeypatch.setattr(rig.manager, "_tell_completion", tell)
        target = completion_events.Target(rig.manager.agent_id, LARK.agent_id, GROUP_A)
        try:
            for index, sender in enumerate((operator, OTHER), 1):
                active = {"target_kind": "group", "channel_id": LARK.agent_id, "chat": GROUP_A,
                          "payload": {"chat_id": GROUP_A},
                          "fallback": [{"notice": {"kind": "chat", "chat": GROUP_A, "mention": sender}}]}
                await rig.manager._handle_completion_due(completion_events.wake(rig.manager.agent_address, target))
                await until(lambda: len(observed) == index)
            assert [scope for _, scope, _ in observed] == ["full", "legacy"]
            assert [audience for _, _, audience in observed] == [chat_scopes.ScopeAudience.OPERATOR,
                                                               chat_scopes.ScopeAudience.SHARED]
            assert observed[0][0] != observed[1][0] and all("~" in key for key, _, _ in observed)
            assert not rig.main.turns
        finally:
            await rig.manager._router.stop()
            await rig.bus.shutdown()

    run_db(body, monkeypatch, timeout=45)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_completion_read_failure_rolls_back_its_claim(monkeypatch, tmp_path) -> None:
    from looper.agents import chat_settings, completion_events
    from looper.agents.tiers.manager_routing import IdentityUnavailable
    from looper.db import manager_events, projects, tasks
    from tests.test_manager_session_routing_db import LARK, OTHER
    from tests.test_scenario_session_routing_db import _scenario

    async def body(pool) -> None:
        rig = await _scenario(pool, tmp_path, "scoped")
        project = await projects.create_project(name="Synthetic", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="synthetic-completion", requirement="Synthetic completion", project_id=project["id"],
                                work_dir=str(tmp_path), status="completed", manager_id=rig.manager.agent_id, pool=pool)
        recorded = await manager_events.record(manager_id=rig.manager.agent_id, task_id="synthetic-completion",
            origin="chat", target_kind="dm", channel_id=LARK.agent_id, chat=OTHER, dedupe_key="synthetic-completion",
            payload={"chat_id": "oc_synthetic_dm"}, fallback=[{"notice": {"kind": "user", "chat": OTHER}}],
            state="pending", reason=None, pool=pool)
        monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
        monkeypatch.setattr(completion_events, "RETRY_SECONDS", 0)
        rig.manager._bus, rig.manager._running = rig.bus, True
        original = rig.manager._router.completion_key
        reads = []

        async def fail_after_claim(origin):
            reads.append(origin)
            if len(reads) == 2:
                raise IdentityUnavailable("synthetic post-claim scope read failure")
            return await original(origin)

        monkeypatch.setattr(rig.manager._router, "completion_key", fail_after_claim)
        target = completion_events.Target(rig.manager.agent_id, LARK.agent_id, OTHER)
        try:
            await rig.manager._handle_completion_due(completion_events.wake(rig.manager.agent_address, target))
            row = await pool.fetchrow("SELECT state, batch FROM manager_events WHERE id = $1", recorded.row["id"])
            assert len(reads) == 2 and row["state"] == "pending" and row["batch"] is None
            assert list(rig.manager._sessions) == ["main"] and not rig.main.turns
            queued = rig.bus.get_queue(rig.manager.agent_id)
            assert queued.qsize() == 1
            assert queued.get_nowait().payload == target.payload()
        finally:
            await rig.manager._router.stop()
            await rig.bus.shutdown()

    run_db(body, monkeypatch, timeout=45)
