"""Scoped refusals, violations and readback. Counters and alerts stay off the unscoped path."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from looper.agents import chat_scope_alerts, chat_scope_metrics, chat_settings, completion_events, session_routing
from looper.agents.managers_api import ScenarioSessionRouting
from looper.agents.ops import Turn
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers import manager_routing
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, default_definition
from looper.agents.v1_domains.manager_metrics import SUMMARY, run as metrics_run
from looper.db import manager_sessions
from tests.db_support import DSN, run_db, until
from tests.lark_cli_stand_in import OPEN_ID
from tests.test_manager_session_routing_db import GROUP_A, GROUP_B, LARK, OTHER, _Resident, _lark, _published
from tests.test_scenario_session_routing_db import MANAGER, _scenario, _to
from tests.test_scoped_launch_db import OPERATOR, _endpoint, _message, _send, _setup, _unsupported
from tests.test_scoped_resident_cap import Resident, group, message

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_PRIVATE = (OPEN_ID, OTHER, OPERATOR, "synthetic-secret")
_WHEN = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_scope_observation(monkeypatch: pytest.MonkeyPatch):
    chat_scope_metrics.reset()
    chat_scope_alerts.reset()
    manager_routing.pool.waits.clear()
    monkeypatch.setattr(session_routing, "_scenario_routing", {})
    yield
    chat_scope_metrics.reset()
    chat_scope_alerts.reset()
    chat_settings.set_metrics_enabled(False)
    manager_routing.pool.waits.clear()


async def _arm(pool: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    await pool.execute("UPDATE channel_configs SET enabled = true WHERE channel_id = $1", LARK.agent_id)
    await pool.execute(
        "INSERT INTO channel_seen_chats (channel_id, chat_id, name) VALUES ($1, $2, 'Synthetic Group') "
        "ON CONFLICT (channel_id, chat_id) DO UPDATE SET name = EXCLUDED.name", LARK.agent_id, GROUP_A)
    sent: list[str] = []

    async def sender(channel_id: str, chat: str, text: str, **kwargs: Any) -> tuple[bool, str | None]:
        sent.append(text)
        return True, None

    chat_scope_alerts.configure(sender)
    monkeypatch.setattr(chat_scope_alerts, "_now", lambda: _WHEN)
    return sent


def _assert_texts(texts: list[str]) -> None:
    joined = "\n".join(texts)
    assert all(secret not in joined for secret in _PRIVATE)
    assert "Synthetic" in joined and "Synthetic Group" in joined


async def _metrics() -> dict[str, Any]:
    from looper.agents import turn_metrics

    pending = list(turn_metrics._pending)
    if pending:
        await asyncio.gather(*pending)
    result = await metrics_run(
        SimpleNamespace(_started_at=datetime.now(timezone.utc), _channels=None), None, {"days": 7}, None, None)
    Draft202012Validator(SUMMARY).validate(result)
    return result


def _report_context(resident: _Resident) -> None:
    kind = type(resident)
    kind.context_tokens = property(lambda self: getattr(self, "_context_tokens", 0))
    kind.context_limit = property(lambda self: 100)
    kind.context_full = property(lambda self: (getattr(self, "_context_tokens", 0) or 0) >= 100)
    resident._context_tokens = 1000


def test_drill_unsupported_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        sent = await _arm(pool, monkeypatch)
        _unsupported(monkeypatch)
        await rig.start()
        try:
            await _send(rig, "unsupported one")
            await _send(rig, "unsupported two")
            assert not any(resident.starts or resident.turns for resident in residents)
            assert chat_scope_metrics.view()["start_refusals"] == [
                {"manager_id": MANAGER, "runtime": "claude-code", "mode": "platform_tools", "count": 2}]
            assert len(sent) == 1
            _assert_texts(sent)
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_drill_injected_skip_permissions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        sent = await _arm(pool, monkeypatch)
        original = rig.manager._claude_command
        monkeypatch.setattr(rig.manager, "_claude_command",
                            lambda *args, **kwargs: [*original(*args, **kwargs), "--dangerously-skip-permissions"])
        await rig.start()
        try:
            await _send(rig, "injected one")
            await _send(rig, "injected two")
            assert not any(resident.starts or resident.turns for resident in residents)
            assert chat_scope_metrics.view()["invariants"] == [
                {"manager_id": MANAGER, "where": "launch", "count": 2}]
            assert len(sent) == 2
            _assert_texts(sent)
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_drill_guessed_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, _residents = await _setup(pool, tmp_path, monkeypatch)
        sent = await _arm(pool, monkeypatch)
        clock = {"now": _WHEN, "mono": 1_000.0}
        monkeypatch.setattr(chat_scope_alerts, "_now", lambda: clock["now"])
        monkeypatch.setattr(chat_scope_alerts, "_mono", lambda: clock["mono"])
        async with _endpoint(rig, tmp_path) as ops:
            envelope = _message(rig, "guess a tool")
            session = await rig.manager._router.target(envelope)
            principal = rig.manager._router.principal(envelope, session)
            turn = Turn(channel="lark", chat=GROUP_A, channel_id=LARK.agent_id, message_id=envelope.message_id,
                        sender=OTHER, group=True, principal=principal)
            ops.begin_turn(turn, session_key=session.key)
            raw = {"project": "synthetic-secret"}
            for _ in range(10):
                await ops.call("looper_nonexistent_synthetic", raw, session_key=session.key)
            assert len(sent) == 1
            await ops.call("looper_nonexistent_synthetic", raw, session_key=session.key)
            assert len(sent) == 1
            scopes = (await _metrics())["scopes"]
            assert scopes["refusals"] == [{
                "manager_id": MANAGER, "capability": "unknown", "principal_kind": "person", "count": 11}]
            clock["now"] = clock["now"] + timedelta(days=1)
            clock["mono"] += 86_400
            for _ in range(10):
                await ops.call("looper_nonexistent_synthetic", raw, session_key=session.key)
            assert len(sent) == 2
            _assert_texts(sent)

    run_db(body, monkeypatch, timeout=90)


def test_resolution_retry_alert_once_until_recovered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, _residents = await _setup(pool, tmp_path, monkeypatch)
        sent = await _arm(pool, monkeypatch)
        clock = {"t": 0.0}
        monkeypatch.setattr(chat_scope_alerts, "_mono", lambda: clock["t"])
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds: float, *args: Any, **kwargs: Any) -> None:
            if seconds == completion_events.RETRY_SECONDS:
                clock["t"] += 301
                return
            await real_sleep(seconds, *args, **kwargs)

        monkeypatch.setattr(manager_routing.asyncio, "sleep", fast_sleep)
        router = rig.manager._router
        original = router._user_key
        left = {"n": 3}

        async def flaky(envelope: Any) -> Any:
            if left["n"]:
                left["n"] -= 1
                raise manager_routing.IdentityUnavailable("synthetic")
            return await original(envelope)

        monkeypatch.setattr(router, "_user_key", flaky)
        await rig.start()
        try:
            await router.target(_message(rig, "retry one"))
            assert len(sent) == 1
            left["n"] = 2
            await router.target(_message(rig, "retry two"))
            assert len(sent) == 2
            assert chat_scope_metrics.view()["resolution_retries"] == [{"manager_id": MANAGER, "count": 5}]
            _assert_texts(sent)
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_metrics_scopes_readback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        sent = await _arm(pool, monkeypatch)
        chat_settings.set_metrics_enabled(True)
        key = f"group:{LARK.agent_id}:{GROUP_A}"
        await manager_sessions.save(dict(
            manager_id=MANAGER, session_key=key, kind="group", channel_id=LARK.agent_id, chat=GROUP_A,
            session_id="SYNTHETIC_OLD_NATIVE", session_totals={}, clean_start=False, runtime_type="claude_code",
            profile_id=None, provider_id=None, pending_handoff=None))
        await rig.start()
        try:
            await _send(rig, "operator turn", OPERATOR)
            await _send(rig, "member turn")
            _report_context(residents[1])
            await _send(rig, "member context full")
            await pool.execute(
                "UPDATE manager_chat_bindings SET default_scope = 'legacy', legacy_allowed = true "
                "WHERE chat_id = $1", GROUP_B)
            result = await _metrics()
            scopes = result["scopes"]
            assert {row["audience"] for row in scopes["turns"]} >= {"operator", "shared"}
            assert {row["scope"] for row in scopes["turns"]} >= {"full", "chat_only"}
            assert all(row["manager_id"] == MANAGER and row["count"] >= 1 for row in scopes["turns"])
            legacy = await pool.fetchval("SELECT count(*) FROM manager_chat_bindings WHERE default_scope = 'legacy'")
            assert scopes["legacy_bindings"] == legacy and legacy >= 1
            opened = {row["reason"] for row in scopes["since_start"]["sessions_opened"]}
            assert {"first", "scope_changed", "context_full"} <= opened
            assert sent == []
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=120)


def test_unscoped_records_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "per_chat")
        monkeypatch.setattr(CLAUDE_CODE, "resident_session", _Resident)
        await rig.start()
        try:
            await rig.reply(await _published(rig, _to(rig, _lark("synthetic per chat", GROUP_A, OTHER, group=True))))
            rows = await pool.fetch("SELECT metadata FROM llm_records")
            assert rows
            for row in rows:
                meta = row["metadata"]
                if isinstance(meta, str):
                    meta = json.loads(meta)
                assert not meta or "scope" not in meta
            await _metrics()
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_resident_queue_wait_is_metric_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        await pool.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) "
            "VALUES ('channel-lark', 'lark', 'Synthetic', true, "
            "jsonb_build_object('operator_sender_id', 'synthetic-operator')) "
            "ON CONFLICT (channel_id) DO UPDATE SET enabled = true, config = EXCLUDED.config")
        sent: list[str] = []

        async def sender(channel_id: str, chat: str, text: str, **kwargs: Any) -> tuple[bool, str | None]:
            sent.append(text)
            return True, None

        chat_scope_alerts.configure(sender)
        monkeypatch.setattr(manager_routing, "pool", manager_routing.ResidentPool())
        monkeypatch.setattr(session_routing, "_routing_enabled", True)
        monkeypatch.setattr(session_routing, "_max_resident", 2)
        monkeypatch.setattr(session_routing, "_max_resident_total", 3)
        real_monotonic = manager_routing.time.monotonic
        offset = {"n": 0.0}
        monkeypatch.setattr(manager_routing.time, "monotonic", lambda: real_monotonic() + offset["n"])

        def make(name: str) -> ManagerAgent:
            definition = replace(default_definition(), id=name, name=name, idle_timeout_seconds=0,
                                 session_routing=ScenarioSessionRouting("scoped"))
            agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path,
                                 definition=definition, clock=lambda: 0.0)
            agent._sessions["main"].claude = Resident()
            agent._send_status = AsyncMock()
            agent._running = True
            return agent

        first, second = make("scoped-a"), make("scoped-b")
        busy = group(first)
        await busy.turn_lock.acquire()
        waiting_session = group(second, started=False)
        waiting = asyncio.create_task(second._router.admit(waiting_session, message(second)))
        try:
            await until(lambda: second._send_status.await_count == 1, timeout=2)
            offset["n"] = 130
            busy.turn_lock.release()
            await first._router._turn_ended(busy)
            await asyncio.wait_for(waiting, 2)
            await second._router.release(waiting_session)
            scopes = (await _metrics())["scopes"]
            assert scopes["residents"]["max_wait_s_24h"] > 120
            assert sent == []
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    run_db(body, monkeypatch, timeout=60)
