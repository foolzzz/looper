"""The session a conversation runs in (``session_routing.session_key_for``), over its rule table."""

from __future__ import annotations

import pytest

from looper.agents import session_routing
from looper.agents.session_routing import GroupIsolation, SessionKey, SessionKind, session_key_for

OPERATOR, OTHER = "ou_operator", "ou_other"
LARK, TELEGRAM = "channel-lark", "channel-telegram"


def _origin(channel: str, channel_id: str | None, chat: str | None, *, sender: str | None = None,
            group: bool = False) -> dict[str, object]:
    return {"channel": channel, "channel_id": channel_id, "chat": chat, "sender": sender, "group": group}


@pytest.mark.parametrize(("origin", "client_id", "operator_id", "key"), [
    (_origin("web", "channel-web", "chat-1"), None, None, "main"),
    (_origin("web", "channel-web", "chat-1"), "client-1", None, "main"),
    (_origin("lark", LARK, "oc_group", sender=OTHER, group=True), None, OPERATOR, f"group:{LARK}:oc_group"),
    (_origin("telegram", TELEGRAM, "-100", sender="7", group=True), None, None, f"group:{TELEGRAM}:-100"),
    (_origin("lark", LARK, "oc_dm", sender=OPERATOR), None, OPERATOR, "main"),
    (_origin("lark", LARK, "oc_dm", sender=OTHER), None, None, "main"),  # no operator id: status quo
    (_origin("lark", LARK, "oc_dm", sender=OTHER), None, OPERATOR, f"dm:{LARK}:oc_dm"),
    (_origin("telegram", TELEGRAM, "8", sender="8"), None, "7", f"dm:{TELEGRAM}:8"),
])
def test_each_conversation_gets_its_session(origin: dict[str, object], client_id: str | None,
                                            operator_id: str | None, key: str) -> None:
    assert session_key_for(origin, client_id=client_id, operator_id=operator_id).text == key


def test_a_key_names_a_chat_exactly_when_it_is_not_main() -> None:
    for kind, channel_id, chat in ((SessionKind.MAIN, LARK, None), (SessionKind.GROUP, LARK, None),
                                   (SessionKind.DM, None, "oc_dm")):
        with pytest.raises(ValueError):
            SessionKey(kind, channel_id, chat)


def test_only_the_default_managers_group_turns_are_isolated_while_routing_is_on(monkeypatch) -> None:
    assert session_routing.group_isolation("manager") is GroupIsolation.SHARED
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    assert session_routing.group_isolation("manager") is GroupIsolation.ISOLATED
    assert session_routing.group_isolation("manager-review") is GroupIsolation.SHARED


def test_scenario_per_chat_selects_a_group_session_without_the_global_switch(tmp_path) -> None:
    import asyncio

    from looper.agents.managers_api import ManagerDefinition
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from tests.test_manager_session_routing_db import _lark

    definition = ManagerDefinition.from_row({
        "id": "manager-synthetic", "name": "Synthetic", "tools": "all", "task_scope": "own",
        "task_policy": "free", "enabled": True, "session_routing": "per_chat",
    })
    agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, definition=definition)
    target = asyncio.run(agent._router.target(_lark("synthetic question", "oc_synthetic", OTHER, group=True)))
    assert target is not None and target.key == "group:channel-lark:oc_synthetic"


def test_a_completion_turn_runs_in_the_session_of_the_chat_it_tells(monkeypatch, tmp_path) -> None:
    """The actually claimed event's chat decides: another sender's DM stays apart from the operator's main."""
    import asyncio

    from types import SimpleNamespace

    from looper.agents import chat_settings, completion_events, operator_identity
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import manager_events

    rows = {person: {"target_kind": "dm", "channel_id": LARK, "chat": person, "payload": {"chat_id": chat},
                     "fallback": [{"notice": {"kind": "user", "chat": person}}]}
            for person, chat in ((OTHER, "oc_dm_other"), (OPERATOR, "oc_dm_mine"))}

    async def claim(manager_id: str, channel_id: str, chat: str, batch: object) -> list[dict]:
        return [rows[chat]]

    async def operator(channel_id: str) -> str:
        return OPERATOR

    monkeypatch.setattr(manager_events, "claim", claim)
    monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)

    async def keys() -> list[str]:
        manager._running = True
        manager._bus = SimpleNamespace(settle_handled=lambda envelope: None)
        found: list[str] = []
        told = asyncio.Event()

        async def tell(claimed: list[dict], batch: object) -> None:
            found.append(manager._session_now().key)
            told.set()

        monkeypatch.setattr(manager, "_tell_completion", tell)
        try:
            for person in (OTHER, OPERATOR):
                told.clear()
                wake = completion_events.wake(manager.agent_address, completion_events.Target("manager", LARK, person))
                assert await manager._router.target(wake) is None  # no route is chosen before the claim
                await manager._handle_completion_due(wake)
                await asyncio.wait_for(told.wait(), 1)
            return found
        finally:
            await manager._router.stop()

    assert asyncio.run(keys()) == [f"dm:{LARK}:oc_dm_other", "main"]
