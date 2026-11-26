"""Stored-only scoped routing, exercised through the real Manager bus with resident stand-ins."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_scopes as scopes, completion_events, session_routing
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.tiers.manager_routing import RouteOutcome, SCOPE_UNAVAILABLE_REPLY
from looper.db import capability_sets, chat_bindings, manager_events, manager_sessions
from tests.db_support import DSN, run_db, until
from tests.test_manager_session_routing_db import GROUP_A, LARK, OTHER, _Resident, _lark, _published
from tests.test_scenario_session_routing_db import MANAGER, _scenario, _to

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
OPERATOR = "synthetic-operator"


def _message(rig: Any, text: str, sender: str = OTHER, kind: str | None = "person") -> MessageEnvelope:
    message = _to(rig, _lark(text, GROUP_A, sender, group=True))
    context = dict(message.payload["channel_context"])
    if kind is not None:
        context["sender_kind"] = kind
    return replace(message, payload={**message.payload, "channel_context": context})


async def _setup(pool: Any, root: Path, monkeypatch: Any, *, operator: str | None = OPERATOR,
                 default: str | None = None) -> tuple[Any, list[_Resident]]:
    rig = await _scenario(pool, root, "scoped")
    await pool.execute("UPDATE channel_configs SET config = jsonb_build_object('operator_sender_id', $1::text) WHERE channel_id = $2", operator, LARK.agent_id)
    await pool.execute("UPDATE manager_chat_bindings SET default_scope = $1, legacy_allowed = true "
                       "WHERE channel_id = $2 AND chat_id = $3", default, LARK.agent_id, GROUP_A)
    session_routing.set_scenario_routing(MANAGER, "scoped")
    monkeypatch.setattr(session_routing, "_max_resident", 8)
    residents: list[_Resident] = []

    class Resident(_Resident):
        def __init__(self, label: str) -> None:
            super().__init__(f"native-{len(residents)}")
            self.stops: list[dict[str, Any]] = []
            residents.append(self)

        async def run_turn(self, text: str, on_event: Any, **kwargs: Any) -> Any:
            cancel = kwargs.get("cancel")
            unregister = cancel.on_request(self.release.set) if cancel is not None else lambda: None
            try:
                return await super().run_turn(text, on_event, **kwargs)
            finally:
                unregister()

        async def stop(self, **kwargs: Any) -> None:
            self.stops.append(kwargs)
            await super().stop(**kwargs)

    from looper.agents.runtimes import CLAUDE_CODE
    monkeypatch.setattr(CLAUDE_CODE, "resident_session", Resident)
    return rig, residents


async def _send(rig: Any, text: str, sender: str = OTHER, kind: str | None = "person") -> Any:
    message = await _published(rig, _message(rig, text, sender, kind))
    await rig.reply(message)
    await until(lambda: not rig.manager._router.pending())
    return message


def test_operator_and_member_get_separate_sessions(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        # An operator override must not precede the trusted operator identity.
        await pool.execute("INSERT INTO binding_principal_scopes "
            "(channel_id, chat_id, sender_id, principal_id, principal_kind, scope_id) "
            "VALUES ($1, $2, $3, $3, 'person', 'chat_only')", LARK.agent_id, GROUP_A, OPERATOR)
        await rig.start()
        try:
            await _send(rig, "operator", OPERATOR)
            await _send(rig, "member")
            rows = await manager_sessions.list(MANAGER)
            assert {(r["scope_id"], r["audience"]) for r in rows} == {("full", "operator"), ("chat_only", "shared")}
            assert all(len(r["session_key"].rsplit("~", 1)[1]) == 16 for r in rows)
            assert len(residents) == 2 and all(len(r.turns) == 1 for r in residents)
            assert not rig.main.turns
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_no_operator_id_means_no_full_session(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, operator=None)
        await rig.start()
        try:
            await _send(rig, "operator", OPERATOR)
            await _send(rig, "member")
            rows = await manager_sessions.list(MANAGER)
            assert len(rows) == 1 and rows[0]["scope_id"] == "chat_only" and rows[0]["audience"] == "shared"
            assert len(residents) == 1 and len(residents[0].turns) == 2
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("default,kind,expected", [("legacy", "bot", "legacy"), (None, "bot", "chat_only"),
                                                   (None, None, "chat_only")])
def test_bot_senders(default: str | None, kind: str | None, expected: str, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default=default)
        # Check routing directly too. An absent kind is a shared non-person sender; an unknown kind is held, not
        # routed (test_unknown_sender_kind_is_held_not_routed).
        session = await rig.manager._router.target(_message(rig, "classification", OPERATOR, kind))
        assert session.scope.resolved.audience is scopes.ScopeAudience.SHARED
        await rig.start()
        try:
            await _send(rig, "bot", OPERATOR, kind)
            rows = await manager_sessions.list(MANAGER)
            assert len(rows) == 1 and rows[0]["scope_id"] == expected and rows[0]["audience"] == "shared"
            assert len(residents[0].turns) == 1 and not rig.manager._router.pending()
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_unknown_sender_kind_completion_settles_batch(monkeypatch: Any, tmp_path: Path) -> None:
    from looper.agents.tiers.manager import _message_origin, _sender_kind

    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        notice = {"channel": "lark", "channel_id": LARK.agent_id, "text": "Synthetic completion notice",
                  "target": {"kind": "chat", "id": GROUP_A}}
        await pool.execute("INSERT INTO projects(id, name, work_dir) VALUES('unknown-kind-project', 'Synthetic', $1)", str(tmp_path))
        await pool.execute("INSERT INTO tasks(id, requirement, status, project_id) "
                           "VALUES('unknown-kind-task', 'Synthetic', 'completed', 'unknown-kind-project')")
        await manager_events.record(manager_id=MANAGER, task_id="unknown-kind-task", origin="chat", target_kind="dm",
            channel_id=LARK.agent_id, chat=GROUP_A, dedupe_key="unknown-kind-completion",
            payload={"chat_id": GROUP_A}, fallback=[{"via": "notice", "notice": notice}], state="pending", reason=None)
        await rig.start()
        try:
            batch = uuid.uuid4()
            rows = await manager_events.claim(MANAGER, LARK.agent_id, GROUP_A, batch)
            assert len(rows) == 1 and rows[0]["state"] == "running"
            envelope = replace(_message(rig, "completion", kind="unknown"), message_type=MessageType.COMPLETION_DUE)
            with pytest.raises(scopes.ScopeInvariantViolation):  # routing reads the kind strictly; completions never do
                _sender_kind(envelope)
            key = await rig.manager._router.completion_key(_message_origin(envelope))
            settled: list[str] = []
            settle = rig.bus.settle_handled

            def record(message: Any) -> None:
                settled.append(message.message_id)
                settle(message)

            monkeypatch.setattr(rig.bus, "settle_handled", record)
            rig.manager._router.dispatch_completion(envelope, rows, batch, None, key=key)
            await until(lambda: envelope.message_id in settled)
            row = await pool.fetchrow("SELECT state, reason FROM manager_events WHERE task_id='unknown-kind-task'")
            assert dict(row) == {"state": "fallback", "reason": "group_chat"}
            assert settled.count(envelope.message_id) == 1
            assert not rig.manager._router.pending()
            assert not any(resident.starts or resident.turns for resident in residents)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_overrides_pick_the_session(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        for sender, principal, kind in [(OTHER, OTHER, "person"), ("bot-open", "bot-principal", "bot"),
                                        ("bot-app", "bot-principal", "bot")]:
            await pool.execute("INSERT INTO binding_principal_scopes "
                "(channel_id, chat_id, sender_id, principal_id, principal_kind, scope_id) "
                "VALUES ($1, $2, $3, $4, $5, 'chat_only')", LARK.agent_id, GROUP_A, sender, principal, kind)
        await rig.start()
        try:
            await _send(rig, "person")
            await _send(rig, "bot-open", "bot-open", "bot")
            await _send(rig, "bot-app", "bot-app", "bot")
            assert len(residents) == 1 and len(residents[0].turns) == 3
            assert (await manager_sessions.list(MANAGER))[0]["scope_id"] == "chat_only"
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_scope_change_opens_a_new_session_without_handoff(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        await rig.start()
        try:
            await _send(rig, "old")
            old = (await manager_sessions.list(MANAGER))[0]
            await pool.execute("UPDATE manager_sessions SET pending_handoff = 'SYNTHETIC_HANDOFF_SECRET' "
                               "WHERE manager_id = $1 AND session_key = $2", MANAGER, old["session_key"])
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'chat_only'")
            await _send(rig, "new")
            retired = await manager_sessions.get(MANAGER, old["session_key"])
            assert retired["retired_reason"] == "scope_changed" and retired["retired_at"] is not None
            assert old["session_key"] not in rig.manager._sessions
            assert residents[0].stops[-1] == {"kill": True}
            assert len(residents) == 2 and "SYNTHETIC_HANDOFF_SECRET" not in residents[1].turns[0]
            assert residents[1].turns[0].startswith("[GROUP SESSION]")
            assert "SYNTHETIC_ALPHA_PRIVATE" in residents[1].turns[0]  # actual chat excerpt read
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("queued", [False, True], ids=["running-finishes", "queued-moves"])
def test_running_and_queued_messages_move_safely(queued: bool, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        first = _message(rig, "first")
        old = await rig.manager._router.target(first)
        old.claude.hold = True
        settled: list[str] = []
        settle = rig.bus.settle_handled
        def record(message: Any) -> None:
            settled.append(message.message_id)
            settle(message)
        monkeypatch.setattr(rig.bus, "settle_handled", record)
        await rig.start()
        try:
            await rig.bus.publish(first)
            await asyncio.wait_for(residents[0].running.wait(), 15)
            second = _message(rig, "second")
            if queued:
                await rig.bus.publish(second)
                await until(lambda: rig.manager._router.queued(old))
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'chat_only'")
            if not queued:
                await rig.bus.publish(second)
                await rig.reply(second)
                assert old.retired is not None and not residents[0].stops
            residents[0].release.set()
            await rig.reply(first)
            await rig.reply(second)
            await until(lambda: old.key not in rig.manager._sessions)
            assert len(residents[0].turns) == 1 and len(residents[1].turns) == 1
            assert residents[0].stops[-1] == {"kill": True}
            assert settled.count(first.message_id) == settled.count(second.message_id) == 1
            assert (await manager_sessions.get(MANAGER, old.key))["retired_reason"] == "scope_changed"
        finally:
            residents[0].release.set()
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_stop_now_interrupts(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        message = _message(rig, "first")
        old = await rig.manager._router.target(message)
        old.claude.hold = True
        calls: list[Any] = []
        stop = rig.manager.stop_turn
        async def stopped(*args: Any, **kwargs: Any) -> Any:
            result = await stop(*args, **kwargs)
            calls.append(result)
            return result
        monkeypatch.setattr(rig.manager, "stop_turn", stopped)
        await rig.start()
        try:
            await rig.bus.publish(message)
            await asyncio.wait_for(residents[0].running.wait(), 15)
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'chat_only'")
            await rig.manager.scope_config_changed(LARK.agent_id, GROUP_A, stop_now=True)
            await until(lambda: old.key not in rig.manager._sessions)
            from looper.agents.tiers.manager import StopResult
            assert calls == [StopResult.STOPPING]
            assert any(call == {"kill": True} for call in residents[0].stops)
        finally:
            residents[0].release.set()
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_revert_reopens_fresh(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        await rig.start()
        try:
            await _send(rig, "a")
            old = (await manager_sessions.list(MANAGER))[0]
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'chat_only'")
            await _send(rig, "b")
            await pool.execute("UPDATE manager_sessions SET pending_handoff = 'SYNTHETIC_HANDOFF_SECRET' "
                               "WHERE manager_id = $1 AND session_key = $2", MANAGER, old["session_key"])
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'legacy' WHERE legacy_allowed")
            next_message = _message(rig, "a-again")
            fresh = await rig.manager._router.target(next_message)
            await rig.manager._load_session_state(fresh)
            assert fresh.session_id is None and fresh.pending_handoff is None and fresh.pending_switch is None
            row = await manager_sessions.get(MANAGER, old["session_key"])
            assert row["retired_at"] is None and row["session_id"] is None and row["pending_handoff"] is None
            await rig.bus.publish(next_message)
            await rig.reply(next_message)
            assert len(residents) == 3 and "--resume" not in residents[2].starts[-1]
            assert "SYNTHETIC_HANDOFF_SECRET" not in residents[2].turns[0]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_per_chat_rows_are_retired_and_never_resumed(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        key = f"group:{LARK.agent_id}:{GROUP_A}"
        await manager_sessions.save(dict(manager_id=MANAGER, session_key=key, kind="group", channel_id=LARK.agent_id,
            chat=GROUP_A, session_id="SYNTHETIC_OLD_NATIVE", session_totals={}, clean_start=False,
            runtime_type="claude_code", profile_id=None, provider_id=None, pending_handoff=None))
        await rig.start()
        try:
            await _send(rig, "new")
            assert (await manager_sessions.get(MANAGER, key))["retired_reason"] == "scope_changed"
            assert "--resume" not in residents[0].starts[-1]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_read_failure_retries_and_never_falls_back(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        failed, resume = asyncio.Event(), asyncio.Event()
        original = chat_bindings.scope_config
        async def unavailable(*args: Any, **kwargs: Any) -> Any:
            if not failed.is_set():
                failed.set()
                raise OSError("synthetic unavailable")
            await resume.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(chat_bindings, "scope_config", unavailable)
        monkeypatch.setattr(completion_events, "RETRY_SECONDS", .01)
        settled: list[str] = []
        settle = rig.bus.settle_handled
        def record(message: Any) -> None:
            settled.append(message.message_id)
            settle(message)
        monkeypatch.setattr(rig.bus, "settle_handled", record)
        await rig.start()
        try:
            message = await _published(rig, _message(rig, "retry"))
            await asyncio.wait_for(failed.wait(), 10)
            assert message.message_id not in settled and not residents and not rig.main.turns
            resume.set()
            await rig.reply(message)
            assert len(residents) == 1 and len(residents[0].turns) == 1
        finally:
            resume.set()
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("items", [[{"kind": "unknown"}], [{"kind": "chat"},
    {"kind": "files.read", "roots": ["chat_attachments"], "deny": [], "required": False}]])
def test_unusable_set_answers_and_starts_nothing(items: Any, monkeypatch: Any, tmp_path: Path) -> None:
    import json
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        await pool.execute("INSERT INTO capability_set_versions (set_id, version, items) VALUES ('chat_only', 2, $1::jsonb)",
                           json.dumps(items))
        await pool.execute("UPDATE capability_sets SET current_version = 2 WHERE id = 'chat_only'")
        await rig.start()
        try:
            message = await _published(rig, _message(rig, "unusable"))
            assert await rig.reply(message) == SCOPE_UNAVAILABLE_REPLY
            await until(lambda: not rig.manager._router.pending())
            assert not residents and not rig.main.turns
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_unknown_sender_kind_is_held_not_routed(monkeypatch: Any, tmp_path: Path, caplog: Any) -> None:
    """Routing reads the channel's sender kind strictly: a value that is neither person nor bot never reaches
    scope resolution (stored provenance tolerates it, authority does not)."""
    from looper.agents.tiers.manager import _sender_kind

    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        invalid = _message(rig, "held", kind="robot")
        with pytest.raises(scopes.ScopeInvariantViolation):
            _sender_kind(invalid)
        await rig.start()
        try:
            held = await _published(rig, invalid)
            await _send(rig, "next")
            rig.collect()
            assert held.message_id not in rig.replies and len(residents[0].turns) == 1
            assert "held" not in residents[0].turns[0]
            assert any(record.levelno == logging.CRITICAL for record in caplog.records)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_invariant_holds_the_message(monkeypatch: Any, tmp_path: Path, caplog: Any) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        original = scopes.resolve
        def invalid(**kwargs: Any) -> Any:
            if kwargs["sender"] == "synthetic-forged":
                raise scopes.ScopeInvariantViolation("synthetic invalid scope")
            return original(**kwargs)
        monkeypatch.setattr(scopes, "resolve", invalid)
        await rig.start()
        try:
            held = await _published(rig, _message(rig, "held", "synthetic-forged"))
            await _send(rig, "next")
            rig.collect()
            assert held.message_id not in rig.replies and len(residents[0].turns) == 1
            assert any(record.levelno == logging.CRITICAL for record in caplog.records)
            # Journal replay is the observable held/unsettled contract.
            await rig.manager.stop()
            await rig.bus.shutdown()
            from looper.agents.bus import MessageBus
            replay = MessageBus(runtime_root=tmp_path)
            await replay.start()
            assert held.message_id in [m.message_id for m in await replay.drain_queue(MANAGER)]
            await replay.shutdown()
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_database_builtins_match_code(monkeypatch: Any) -> None:
    async def body(pool: Any) -> None:
        rows = await capability_sets.current(list(scopes.BUILTIN_VERSIONS))
        assert {name: (version, scopes.parse_items(items, scope=name)) for name, (version, items) in rows.items()} == (
            scopes.BUILTIN_VERSIONS)
    run_db(body, monkeypatch, timeout=60)


def test_completion_follows_its_origin(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, _ = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        router = rig.manager._router
        for sender, expected in [(OPERATOR, "full"), (OTHER, "legacy"), (None, "legacy")]:
            origin = dict(channel="lark", channel_id=LARK.agent_id, chat=GROUP_A, group=True, sender=sender)
            key = await router.completion_key(origin)
            actual = await router.target(_message(rig, "compare", sender or "synthetic-bot", "person" if sender else "bot"))
            assert key.text == actual.key and actual.scope.resolved.scope_id == expected
    run_db(body, monkeypatch, timeout=60)


def test_retiring_one_chat_keeps_another_chats_principal(monkeypatch: Any, tmp_path: Path) -> None:
    from tests.test_manager_session_routing_db import GROUP_B
    async def body(pool: Any) -> None:
        rig, _ = await _setup(pool, tmp_path, monkeypatch)
        a = _message(rig, "a")
        b = _to(rig, _lark("b", GROUP_B, OTHER, group=True))
        session_a = await rig.manager._router.target(a)
        session_b = await rig.manager._router.target(b)
        principal = rig.manager._router.principal(b, session_b)
        assert session_a.scope.fingerprint == session_b.scope.fingerprint  # positive control
        await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'legacy' WHERE chat_id = $1", GROUP_A)
        config = await chat_bindings.scope_config(LARK.agent_id, GROUP_A, MANAGER)
        await rig.manager._router.reconcile(LARK.agent_id, GROUP_A, config=config, operator_id=OPERATOR)
        assert session_a.key not in rig.manager._sessions
        assert rig.manager._router.principal(b, session_b) == principal
    run_db(body, monkeypatch, timeout=60)


@pytest.mark.parametrize("source", ["operator", "set"])
def test_identity_reads_fail_closed_even_with_an_old_operator_cache(source: str, monkeypatch: Any, tmp_path: Path) -> None:
    from looper.agents import operator_identity
    from looper.agents.tiers.manager_routing import IdentityUnavailable
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        async def failed(*args: Any, **kwargs: Any) -> Any:
            raise OSError("synthetic read failure")
        if source == "operator":
            rig.manager._router._operator_ids[LARK.agent_id] = (0, OPERATOR)
            monkeypatch.setattr(operator_identity, "operator_sender_id", failed)
        else:
            monkeypatch.setattr(capability_sets, "current", failed)
        with pytest.raises(IdentityUnavailable):
            await rig.manager._router._user_key(_message(rig, "fail", OPERATOR))
        assert not residents and not rig.main.turns and list(rig.manager._sessions) == ["main"]
    run_db(body, monkeypatch, timeout=60)


def test_a_deleted_set_answers_without_starting_a_resident(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        await pool.execute("UPDATE capability_sets SET deleted_at = now() WHERE id = 'chat_only'")
        await rig.start()
        try:
            message = await _published(rig, _message(rig, "deleted"))
            assert await rig.reply(message) == SCOPE_UNAVAILABLE_REPLY
            assert not residents and list(rig.manager._sessions) == ["main"]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


def test_queued_return_to_a_retired_key_waits_for_a_fresh_resident(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        first, second = _message(rig, "running"), _message(rig, "queued")
        old = await rig.manager._router.target(first)
        residents[0].hold = True
        await rig.start()
        try:
            await rig.bus.publish(first)
            await asyncio.wait_for(residents[0].running.wait(), 15)
            await rig.bus.publish(second)
            await until(lambda: rig.manager._router.queued(old))
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'chat_only' WHERE chat_id = $1", GROUP_A)
            await rig.manager.scope_config_changed(LARK.agent_id, GROUP_A, stop_now=False)
            assert old.retired is not None and not residents[0].stops
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'legacy' WHERE chat_id = $1", GROUP_A)
            residents[0].release.set()
            await rig.reply(first)
            await rig.reply(second)
            await until(lambda: not rig.manager._router.pending())
            assert rig.manager._sessions[old.key] is not old
            assert residents[0].stops[-1] == {"kill": True}
            assert len(residents) == 2 and "--resume" not in residents[1].starts[-1]
            assert (await manager_sessions.get(MANAGER, old.key))["retired_at"] is None
        finally:
            residents[0].release.set()
            await rig.close()
    run_db(body, monkeypatch, timeout=90)



def test_retired_queue_returns_to_main_when_routing_is_switched_off(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch, default="legacy")
        first, second = _message(rig, "running"), _message(rig, "queued")
        old = await rig.manager._router.target(first)
        residents[0].hold = True
        await rig.start()
        try:
            await rig.bus.publish(first)
            await asyncio.wait_for(residents[0].running.wait(), 15)
            await rig.bus.publish(second)
            await until(lambda: rig.manager._router.queued(old))
            await pool.execute("UPDATE manager_chat_bindings SET default_scope = 'chat_only' WHERE chat_id = $1", GROUP_A)
            await rig.manager.scope_config_changed(LARK.agent_id, GROUP_A, stop_now=False)
            await pool.execute("UPDATE managers SET session_routing = 'off' WHERE id = $1", MANAGER)
            await rig.manager._load_db_config()
            residents[0].release.set()
            await rig.reply(first)
            await rig.reply(second)
            await until(lambda: old.key not in rig.manager._sessions)
            assert len(rig.main.turns) == 1 and "queued" in rig.main.turns[0]
            assert len(residents) == 1 and len(residents[0].turns) == 1
            assert list(rig.manager._sessions) == ["main"]
        finally:
            residents[0].release.set()
            await rig.close()
    run_db(body, monkeypatch, timeout=90)
