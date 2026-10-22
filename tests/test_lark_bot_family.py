"""Both Lark transports share message origins, notification targets and gate routing."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import gate_routing, gate_service
from looper.agents.channels.allowlist import SENDER_ID_FORMATS, sender_ids
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address, is_lark
from looper.agents.task_notice import NotifyChannel, NotifyTarget, TargetKind, link_line, notify_channel, notify_target
from looper.agents.tiers.manager import ManagerAgent, channel_line


def _message(*, group: bool = False, **context: Any) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=channel_address("lark_bot"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": "hello", "channel_context": {
            "chat_id": "oc_group" if group else "oc_dm", "sender_id": "ou_requester123", "sender_name": "张三",
            "chat_type": "group" if group else "p2p", **context}})


@pytest.mark.parametrize(("channel", "expected"), [
    ("lark", True), ("lark_bot", True), ("telegram", False), ("web", False), (None, False), ("lark_extra", False),
])
def test_lark_family(channel: str | None, expected: bool) -> None:
    assert is_lark(channel) is expected


@pytest.mark.parametrize("channel", [NotifyChannel.LARK, NotifyChannel.LARK_BOT])
def test_lark_notification_targets_and_links(channel: NotifyChannel) -> None:
    channel_id = channel_address(channel.value).agent_id
    assert notify_channel(channel_id) is channel and channel.is_lark
    assert notify_target(channel_id, sender="ou_requester123", chat="oc_dm", group=False) == \
        NotifyTarget(TargetKind.USER, "ou_requester123")
    assert notify_target(channel_id, sender="ou_requester123", chat="oc_group", group=True) == \
        NotifyTarget(TargetKind.CHAT, "oc_group", mention="ou_requester123")
    assert notify_target(channel_id, sender=None, chat="oc_dm", group=False) is None
    assert notify_target(channel_id, sender="ou_requester123", chat=None, group=True) is None
    assert link_line("https://example.test/tasks/a", channel) == \
        "[https://example.test/tasks/a](https://example.test/tasks/a)"
    assert not NotifyChannel.TELEGRAM.is_lark


@pytest.mark.parametrize(("context", "reference"), [
    ({"reply_to": "om_reply", "root_id": "om_root"}, "om_reply"),
    ({"root_id": "om_root"}, "om_root"),
    ({}, None),
    ({"reply_to": "om_reply", "chat_id": ""}, None),
])
def test_bot_gate_references(context: dict[str, Any], reference: str | None) -> None:
    message = _message(group=True, **context)
    found = gate_routing.references(message)
    if reference is None:
        assert found is None
    else:
        assert found == ("lark_bot", "channel-lark_bot", "oc_group", reference, message.payload["channel_context"])


@pytest.mark.parametrize("group", [False, True])
def test_bot_group_origin_and_gate_target(group: bool) -> None:
    message = _message(group=group)
    context = message.payload["channel_context"]
    assert gate_routing._group("lark_bot", context) is group
    assert gate_routing._author("lark_bot", context) == ("张三" if group else None)
    assert channel_line(message) == ("Channel: a Lark group chat, from 张三" if group else
                                     "Channel: a Lark direct message")
    assert gate_service.origin_chat("lark_bot", context["chat_id"], group=group,
                                    sender=context["sender_id"]) == ("oc_group" if group else "ou_requester123")
    target = NotifyTarget(TargetKind.CHAT if group else TargetKind.USER,
                          "oc_group" if group else "ou_requester123")
    chat = gate_service.GateChat(NotifyChannel.LARK_BOT, target)
    assert chat.lark_group is group
    assert chat.label() == ("Lark (bot token) group oc_group" if group else "Lark (bot token) ou_requester123")
    dashboard = gate_service._dashboard_chat(json.dumps({"channel": "lark_bot", "chat": "ou_requester123"}))
    assert dashboard == gate_service.GateChat(NotifyChannel.LARK_BOT,
                                             NotifyTarget(TargetKind.USER, "ou_requester123"))


def test_bot_open_gates_are_filtered_to_its_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.tiers import manager

    calls = []

    async def open_gates(limit: int, **filters: Any) -> list[Any]:
        calls.append(filters)
        return []

    async def dashboard_target() -> None:
        return None

    monkeypatch.setattr(manager.db_user_gates, "open_gates", open_gates)
    monkeypatch.setattr(manager, "gate_dashboard_target", dashboard_target)
    default_manager = SimpleNamespace(agent_id="manager", _log="Manager")
    assert asyncio.run(ManagerAgent._open_gates_section(default_manager, _message())) == []
    assert calls == [{"chat": ("channel-lark_bot", "ou_requester123"), "manager_id": "manager", "dashboard": False}]


def test_bot_allowlist_uses_lark_open_ids() -> None:
    assert SENDER_ID_FORMATS["lark_bot"] == SENDER_ID_FORMATS["lark"]
    assert sender_ids("lark_bot", ["ou_requester123", "ou_requester123"]) == ("ou_requester123",)
    with pytest.raises(ValueError, match="open_id"):
        sender_ids("lark_bot", ["on_union123456"])


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="LOOPER_TEST_DATABASE_URL is not set")
def test_bot_schedule_targets_use_its_own_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.agents.schedules import notify_targets
    from looper.db import connection

    async def test() -> None:
        dsn = os.environ["LOOPER_TEST_DATABASE_URL"]
        schema = f"looper_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(dsn)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await pool.execute(
                "INSERT INTO channel_configs (channel_id, channel_type, enabled, config) VALUES "
                "('channel-lark_bot', 'lark_bot', true, $1::jsonb), "
                "('channel-lark', 'lark', false, $2::jsonb)",
                json.dumps({"allowed_open_ids": ["ou_requester123"]}),
                json.dumps({"allowed_open_ids": ["ou_cliuser1234"]}))
            assert await notify_targets() == [
                {"channel": "lark", "chat": "ou_cliuser1234", "label": "Lark · ou_cliuser1234",
                 "channel_enabled": False},
                {"channel": "lark_bot", "chat": "ou_requester123", "label": "Lark · ou_requester123",
                 "channel_enabled": True},
            ]
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(test())
