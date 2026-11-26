"""An API client's turns run in a Manager session of the client's own, not in the operator's main session.

The runtimes are stand-ins put in place before the first turn (``_Resident``); what each one is started with and
sent is what the model would see. Each scenario runs with session routing off (the default) and on. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from looper.agents import operator_identity, persona, session_routing, session_visibility, vault
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.ops import Risk, Tool
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import ManagerAgent
from looper.agents.v1_domains import manager_sessions as sessions_view
from looper.db import chat as db_chat
from looper.db import manager_sessions
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import OPEN_ID
from tests.test_manager_session_routing_db import LARK, WEB, _lark, _published, _Resident, _Rig, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

DASHBOARD_MARKER, DM_MARKER = "MARKER_OPERATOR_DASHBOARD_7f3a", "MARKER_OPERATOR_DM_91c2"
CLIENT_A_MARKER, CLIENT_B_MARKER = "MARKER_CLIENT_A_5d10", "MARKER_CLIENT_B_e8b4"
OPERATOR_OTHER_CHAT_MARKER = "MARKER_OPERATOR_OTHER_CHAT_2b77"


@pytest.fixture(params=[False, True], ids=["routing_off", "routing_on"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    async def operator(channel_id: str) -> str | None:
        return OPEN_ID if channel_id == LARK.agent_id else None

    monkeypatch.setattr(session_routing, "_routing_enabled", request.param)
    monkeypatch.setattr(session_routing, "_max_resident", 2)
    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    return monkeypatch


def _client(text: str, client_id: str, thread: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": thread,
                                                   "client": {"id": client_id, "name": f"name-{client_id}"}}})


async def _sent(rig: _Rig, pool: Any, message: MessageEnvelope) -> MessageEnvelope:
    """Publish ``message``; a web message is stored first, as the web channel's own path stores it before the Manager
    sees it (the Manager stores the other channels' messages itself)."""
    context = message.payload["channel_context"]
    if message.source == WEB:
        await db_chat.save_message(session_id=context["session_id"], message_id=message.message_id, role="user",
                                   content=message.payload["text"], pool=pool)
    return await _published(rig, message)


def _key(client_id: str) -> str:
    return session_routing.client_key(client_id).text


async def _thread(pool: Any, thread: str, client_id: str | None) -> None:
    await db_chat.claim_thread(thread, "manager", client_id=client_id, pool=pool)


class _Capture:
    """The complete model input of each session: its stand-in's start commands (argv), the system prompt files they
    name, every turn's stdin text, and what ``_prepare_turn`` assembled (system prompt, turn text, state, tools)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.prepared: dict[str, list[Any]] = {}
        prepare = ManagerAgent._prepare_turn
        capture = self

        async def spy(agent: Any, *args: Any, **kwargs: Any) -> Any:
            prepared = await prepare(agent, *args, **kwargs)
            if prepared.signature is not None:
                capture.prepared.setdefault(agent._session_now().key, []).append({
                    "system_prompt": prepared.system_prompt.text, "turn": prepared.turn_text,
                    "state": prepared.state, "command": list(prepared.signature[0])})
            return prepared

        monkeypatch.setattr(ManagerAgent, "_prepare_turn", spy)

    def complete(self, key: str, resident: _Resident) -> str:
        parts = [*resident.turns, json.dumps(resident.starts), json.dumps(self.prepared.get(key, []), default=str)]
        for command in resident.starts:
            for flag in ("--system-prompt-file", "--append-system-prompt-file"):
                if flag in command:
                    parts.append(Path(command[command.index(flag) + 1]).read_text())
        return "\n".join(parts)


def _run(test: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            await test(rig, pool)
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def _client_resident(rig: _Rig, client_id: str, resident: _Resident) -> _Resident:
    rig.manager._router.session(session_routing.client_key(client_id)).claude = resident
    return resident


def test_operator_markers_never_reach_a_client_turn(mode: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        capture = _Capture(mode)
        await _thread(pool, "thread-a", "client-a")
        await db_chat.save_message(session_id="thread-other", message_id="m0", role="user",
                                   content=f"seeded {OPERATOR_OTHER_CHAT_MARKER}", pool=pool)
        a = _client_resident(rig, "client-a", _Resident("sess-a"))
        # The operator's own turns first: the dashboard and a direct chat; their text is stored.
        await rig.reply(await _sent(rig, pool, _web(f"dashboard {DASHBOARD_MARKER}")))
        await rig.reply(await _sent(rig, pool, _lark(f"dm {DM_MARKER}", "oc_operator_dm", OPEN_ID, group=False)))
        main_input = capture.complete("main", rig.main)
        # Positive controls: the capture sees the operator's markers in main's input, and main's new-session brief
        # carries other chats' stored text; so the client's absence below is not an empty capture.
        assert DASHBOARD_MARKER in main_input and DM_MARKER in main_input
        assert OPERATOR_OTHER_CHAT_MARKER in main_input
        # Then the client's first turn (a new session), and a later one.
        await rig.reply(await _sent(rig, pool, _client(f"hello {CLIENT_A_MARKER}", "client-a", "thread-a")))
        await rig.reply(await _sent(rig, pool, _client("again", "client-a", "thread-a")))
        a_input = capture.complete(_key("client-a"), a)
        assert CLIENT_A_MARKER in a_input and len(a.turns) == 2
        for marker in (DASHBOARD_MARKER, DM_MARKER, OPERATOR_OTHER_CHAT_MARKER):
            assert a_input.count(marker) == 0, marker
        # The turns ran in the client's own session, on its own saved session id, never main's.
        assert not any("hello" in turn or "again" in turn for turn in rig.main.turns)
        row = await manager_sessions.get("manager", _key("client-a"))
        assert row is not None and row["kind"] == "client" and row["session_id"] == "sess-a"

    _run(test, mode, tmp_path)


def test_a_client_marker_never_reaches_mains_input(mode: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        capture = _Capture(mode)
        await _thread(pool, "thread-a", "client-a")
        await db_chat.save_message(session_id="thread-other", message_id="m0", role="user",
                                   content=f"seeded {OPERATOR_OTHER_CHAT_MARKER}", pool=pool)
        a = _client_resident(rig, "client-a", _Resident("sess-a"))
        await rig.reply(await _sent(rig, pool, _client(f"hello {CLIENT_A_MARKER}", "client-a", "thread-a")))
        assert CLIENT_A_MARKER in capture.complete(_key("client-a"), a)  # positive control
        # Main's first turn builds its resume brief from the stored chats, the client's among them.
        await rig.reply(await _sent(rig, pool, _web(f"dashboard {DASHBOARD_MARKER}")))
        await rig.reply(await _sent(rig, pool, _lark(f"dm {DM_MARKER}", "oc_operator_dm", OPEN_ID, group=False)))
        main_input = capture.complete("main", rig.main)
        assert OPERATOR_OTHER_CHAT_MARKER in main_input and DASHBOARD_MARKER in main_input  # positive controls
        assert main_input.count(CLIENT_A_MARKER) == 0

    _run(test, mode, tmp_path)


def test_a_client_never_sees_another_clients_turns(mode: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        capture = _Capture(mode)
        await _thread(pool, "thread-a", "client-a")
        await _thread(pool, "thread-b", "client-b")
        a = _client_resident(rig, "client-a", _Resident("sess-a"))
        b = _client_resident(rig, "client-b", _Resident("sess-b"))
        await rig.reply(await _sent(rig, pool, _client(f"A says {CLIENT_A_MARKER}", "client-a", "thread-a")))
        await rig.reply(await _sent(rig, pool, _client(f"B says {CLIENT_B_MARKER}", "client-b", "thread-b")))
        await rig.reply(await _sent(rig, pool, _client("A again", "client-a", "thread-a")))
        a_input, b_input = capture.complete(_key("client-a"), a), capture.complete(_key("client-b"), b)
        assert CLIENT_A_MARKER in a_input and CLIENT_B_MARKER in b_input  # positive controls
        assert CLIENT_B_MARKER not in a_input
        assert CLIENT_A_MARKER not in b_input
        assert len(a.turns) == 2 and len(b.turns) == 1 and not rig.main.turns

    _run(test, mode, tmp_path)


def test_a_client_session_lists_only_the_allowlisted_tools_and_refuses_the_rest(
        mode: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        reads: list[str] = []

        async def future_read(call: Any) -> dict[str, Any]:
            reads.append("ran")
            return {"secret": "operator-only"}

        future = Tool("synthetic_future_read", "Read operator state", {"type": "object", "properties": {}},
                      Risk.READ, future_read)
        ops = ManagerOps(Platform(bus=None, task_router=None, manager=rig.manager),
                         url="http://127.0.0.1:9/", token="synthetic-token")
        ops._working_at = ops._clock()
        rig.manager.ops = ops
        scope = await ops.current_scope()
        ops._scope = replace(scope, tools=MappingProxyType({**scope.tools, future.name: future}))
        await _thread(pool, "thread-a", "client-a")
        a = _client_resident(rig, "client-a", _Resident("sess-a"))
        seen: dict[str, Any] = {"calls": []}

        async def calls(on_event: Any) -> None:
            binding = rig.manager._running_token.binding
            seen["listing"] = sorted(item["name"] for item in await ops.current_listing(session_key=_key("client-a")))
            for name, arguments in (("looper_settings", {"action": "get"}), ("looper_projects", {"action": "list"}),
                                    ("looper_overview", {}), ("looper_confirm", {"confirmation_id": "x"}),
                                    ("looper_session_search", {"query": "x"}), (future.name, {})):
                seen["calls"].append((name, await ops.call(name, arguments, binding=binding,
                                                            session_key=_key("client-a"))))
            seen["tasks"] = await ops.call("looper_tasks", {"action": "list"}, binding=binding,
                                           session_key=_key("client-a"))

        a.on_turn = calls
        await rig.reply(await _sent(rig, pool, _client("list my tasks", "client-a", "thread-a")))
        assert seen["listing"] == sorted(session_visibility.CLIENT_TOOLS)
        assert len(seen["calls"]) == 6
        for name, answer in seen["calls"]:
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", (name, answer)
        assert reads == []
        assert seen["tasks"]["status"] == "ok", seen["tasks"]  # positive control: a listed tool runs

    _run(test, mode, tmp_path)


def test_client_sessions_count_toward_the_resident_limit_and_are_reclaimed(
        mode: pytest.MonkeyPatch, tmp_path: Path) -> None:
    routing_on = session_routing.routing_enabled()

    async def test(rig: _Rig, pool: Any) -> None:
        await _thread(pool, "thread-a", "client-a")
        await _thread(pool, "thread-b", "client-b")
        a = _client_resident(rig, "client-a", _Resident("sess-a", hold=True))
        b = _client_resident(rig, "client-b", _Resident("sess-b"))
        await rig.reply(await _sent(rig, pool, _web("你好")))  # main holds a process
        assert rig.main.started
        first = await _sent(rig, pool, _client("one", "client-a", "thread-a"))
        await asyncio.wait_for(a.running.wait(), 20)
        assert a.started
        second = await _sent(rig, pool, _client("two", "client-b", "thread-b"))
        await asyncio.sleep(0.5)
        # main and client A hold the two process slots and A is busy: B waits.
        assert not b.turns and not b.started
        a.release.set()
        await rig.reply(first)
        assert await rig.reply(second) == "sess-b 回复 1"
        if routing_on:
            # Resident until another session needs the slot: A was stopped for B, its session id kept.
            assert b.started and not a.started
            assert (await manager_sessions.get("manager", _key("client-a")))["session_id"] == "sess-a"
        else:
            # Routing off: a client's process stops once it is idle.
            await rig.manager._router.sweep()
            assert not a.started and not b.started
        assert rig.main.started  # main is never stopped for a client
        assert not rig.main.turns[1:]

    _run(test, mode, tmp_path)


def test_the_operators_session_list_does_not_show_client_sessions(mode: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        await _thread(pool, "thread-a", "client-a")
        _client_resident(rig, "client-a", _Resident("sess-a"))
        await rig.reply(await _sent(rig, pool, _client("hi", "client-a", "thread-a")))
        assert await manager_sessions.get("manager", _key("client-a")) is not None
        rows = await sessions_view._rows(rig.manager)
        assert [row["key"] for row in rows] == ["main"]

    _run(test, mode, tmp_path)


@pytest.mark.parametrize("stable", [False, True], ids=["plain", "stable_context"])
def test_a_client_turn_carries_no_operator_profile_persona_or_prompt_layers(
        mode: pytest.MonkeyPatch, tmp_path: Path, stable: bool) -> None:
    sentinels = {name: f"PRIVATE_{name.upper()}_SENTINEL" for name in (
        "profile", "tone", "notes", "role_prompt", "harness", "custom_prompt")}

    async def test(rig: _Rig, pool: Any) -> None:
        capture = _Capture(mode)
        note_root = tmp_path / "synthetic-vault"
        note_root.mkdir()
        (note_root / "关于我.md").write_text(f"## Name\n{sentinels['profile']}\n", encoding="utf-8")
        await vault.update_settings({"path": str(note_root.resolve()), "writable": [], "rules_file": None,
                                     "manager_read": True})
        mode.setattr(persona, "_current", persona.Persona(name="Synthetic Manager", tone=sentinels["tone"],
                                                          notes=sentinels["notes"]))
        for key in ("manager.profile.enabled", *(("manager.stable_context",) if stable else ())):
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true') "
                               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key)
        await pool.execute("INSERT INTO role_configs (role, system_prompt) VALUES ('manager', $1) "
                           "ON CONFLICT (role) DO UPDATE SET system_prompt = EXCLUDED.system_prompt",
                           sentinels["role_prompt"])
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, harness_prompt) VALUES ('role', 'manager', $1) "
                           "ON CONFLICT DO NOTHING", sentinels["harness"])
        await pool.execute("UPDATE agent_profiles SET custom_prompt = $1", sentinels["custom_prompt"])
        await _thread(pool, "thread-a", "client-a")
        a = _client_resident(rig, "client-a", _Resident("sess-a"))
        await rig.reply(await _sent(rig, pool, _web("operator turn")))
        main_input = capture.complete("main", rig.main)
        for name, sentinel in sentinels.items():  # positive controls: main has every one of them
            assert sentinel in main_input, name
        await rig.reply(await _sent(rig, pool, _client("hello", "client-a", "thread-a")))
        await rig.reply(await _sent(rig, pool, _client("again", "client-a", "thread-a")))
        a_input = capture.complete(_key("client-a"), a)
        for name, sentinel in sentinels.items():
            assert sentinel not in a_input, name
        assert "Synthetic Manager" in a_input  # the persona's name is the one thing of it a client gets
        assert "API client" in a_input and "Projects" in a_input

    _run(test, mode, tmp_path)
