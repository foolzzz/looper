"""The outbound review's hook answered by the Manager (``ManagerOps.check_outbound``, ``outbound_hook``) on a real
PostgreSQL, with a Claude Code stand-in as the classifier: what runs at once, what is reviewed or refused by code,
the notice, the switch, and the reviews' spend and cleanup.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from tests.db_support import DSN, drop_tables, run_db
from tests.fake_claude import fake_claude
from tests.test_outbound_db import ALLOW, BLOCK, _STAND_IN, _isolated, _seen, _setup  # noqa: F401 - the fixture

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

WEB = "web"


@pytest.fixture(autouse=True)
def _switch_forgotten() -> Any:
    """The switch as the server last read it is the module's: each test starts from off, as a fresh server does."""
    from looper.agents import outbound_hook

    outbound_hook._known = False
    yield
    outbound_hook._known = False


def _ops(tmp_path: Path, library: Any = None) -> ManagerOps:
    router = SimpleNamespace(has_planner=lambda task_id: False, forget_task=lambda task_id: None)
    ops = ManagerOps(Platform(bus=MessageBus(runtime_root=tmp_path), task_router=router, runtime_root=tmp_path),
                     url="http://127.0.0.1:9", token="tok")
    ops.activate_tool_hook(ops.tool_hook(tmp_path / "rt", fake_claude(tmp_path / "claude", _STAND_IN), library))
    return ops


class _Manager:
    """A Manager's side of the hook: its turns, and each call streamed in its turn (``expect``) before the hook asks
    about it, as the Manager's Claude Code does."""

    def __init__(self, tmp_path: Path, library: Any = None) -> None:
        self.ops = _ops(tmp_path, library)
        self.binding: str | None = None
        self.calls = 0

    def turn(self, text: str, *, channel: str = WEB, group: bool = False) -> Turn:
        envelope = MessageEnvelope.create(source=channel, target=channel, message_type=MessageType.USER_MESSAGE,
                                          payload={"text": text}, correlation_id=f"corr-{text[:8]}")
        turn = Turn(channel=channel, chat="s-1", message_id=envelope.message_id, envelope=envelope, sender="ou_x",
                    group=group)
        self.binding = f"b-{envelope.message_id}"
        self.ops.begin_turn(turn, self.binding)
        return turn

    async def call(self, tool: str, *, streamed: bool = True, parent: str | None = None, **tool_input: Any) -> Any:
        self.calls += 1
        call_id = f"toolu_{self.calls}"
        if streamed:
            self.ops.expect(self.binding, tool, tool_input, call_id=call_id, parent_call_id=parent)
        answer = await self.ops.check_outbound({"tool_name": tool, "tool_input": tool_input, "tool_use_id": call_id,
                                                "session_id": "s", "cwd": "/w"})
        return answer, call_id


def test_a_reading_call_runs_at_once_and_a_shell_command_is_reviewed_in_its_turn(monkeypatch: pytest.MonkeyPatch,
                                                                               tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        manager = _Manager(tmp_path)
        manager.turn("跑一下测试")
        assert (await manager.call("Read", file_path="/w/a.py"))[0] == {"decision": "allow"}
        assert (await manager.call("mcp__looper__looper_tasks", action="list"))[0] == {"decision": "allow"}
        assert _seen(tmp_path) == [] and await pool.fetchval("SELECT count(*) FROM outbound_reviews") == 0
        answer, _ = await manager.call("Bash", command="uv run pytest -q")
        assert answer == {"decision": "allow"}
        row = await pool.fetchrow("SELECT agent_id, source, tool, level, target, verdict, record_id FROM "
                                  "outbound_reviews")
        assert tuple(row)[:6] == ("manager", "hook", "Bash", "unknown", "uv run pytest -q", "allow")
        (seen,) = _seen(tmp_path)
        assert '"who": "operator"' in seen["turn"] and "跑一下测试" in seen["turn"]

    run_db(test, monkeypatch, setup=_setup)


def test_a_call_looper_cannot_place_or_from_a_turn_that_ended_is_no_ones(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    """A call is placed in the turn whose stream showed it (a subagent's, by the call that started it), never in the
    turn that runs when it comes: an earlier turn's background subagent does not borrow the operator's next turn."""
    from looper.agents import outbound_hook

    async def test(pool: Any) -> None:
        monkeypatch.setattr(outbound_hook, "PLACE_WAIT_SEC", 0.2)
        manager = _Manager(tmp_path)
        first = manager.turn("在后台跑个子任务")
        _, task = await manager.call("Task", prompt="watch the build")
        manager.ops.end_turn(first)
        manager.turn("跑一下测试")  # the operator's next turn runs now
        late, _ = await manager.call("Bash", parent=task, command="gh pr comment 1 --body done")
        assert late["decision"] == "block"
        unplaced, _ = await manager.call("Bash", streamed=False, command="uv run pytest -q")
        assert unplaced["decision"] == "block"
        orphan, _ = await manager.call("Bash", parent="toolu_unknown", command="ls")
        assert orphan["decision"] == "block"
        own, _ = await manager.call("Bash", command="uv run pytest -q")
        assert own == {"decision": "allow"}
        rows = await pool.fetch("SELECT failure, verdict FROM outbound_reviews ORDER BY id")
        assert [tuple(r) for r in rows] == [("not_operator", "block")] * 3 + [(None, "allow")]

    run_db(test, monkeypatch, setup=_setup)


def test_a_subagents_call_in_the_running_turn_is_that_turns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        manager = _Manager(tmp_path)
        manager.turn("让子 agent 跑一下测试")
        _, task = await manager.call("Task", prompt="run the tests")
        answer, _ = await manager.call("Bash", parent=task, command="uv run pytest -q")
        assert answer == {"decision": "allow"} and '"who": "operator"' in _seen(tmp_path)[0]["turn"]

    run_db(test, monkeypatch, setup=_setup)


def test_an_mcp_servers_tool_is_reviewed_unless_the_operator_declared_it_inside(monkeypatch: pytest.MonkeyPatch,
                                                                               tmp_path: Path) -> None:
    from looper.agents import mcp
    from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
    from looper.db import mcp_tool_levels

    async def test(pool: Any) -> None:
        server = (await mcp.create({"name": "search", "transport": "stdio", "command": "python3"}))["item"]
        manager = _Manager(tmp_path, AgentLibrary(items=(LoadedItem(ItemKind.MCP, "search", "0" * 64, ""),)))
        manager.turn("搜一下")
        assert (await manager.call("mcp__search__query", q="cache"))[0] == {"decision": "allow"}
        assert len(_seen(tmp_path)) == 1  # reviewed
        assert (await mcp_tool_levels.put(server["item_id"], ["query"]))["inside_tools"] == ["query"]
        assert (await manager.call("mcp__search__query", q="cache"))[0] == {"decision": "allow"}
        assert len(_seen(tmp_path)) == 1  # declared inside: no review
        await manager.call("mcp__search__send", to="x")
        assert len(_seen(tmp_path)) == 2  # its other tools still are
        manager.turn("发一下", channel="lark", group=True)
        assert (await manager.call("mcp__search__send", to="x"))[0]["decision"] == "block"
        with pytest.raises(mcp_tool_levels.NoSuchServer):
            await mcp_tool_levels.put("mcp-missing", ["query"])
        with pytest.raises(mcp_tool_levels.DeclarationError):
            await mcp_tool_levels.put(server["item_id"], ["has space"])
        assert (await mcp_tool_levels.put(server["item_id"], []))["inside_tools"] == []

    run_db(test, monkeypatch, setup=_setup)


def test_a_blocked_command_is_denied_with_its_reason_and_the_operator_told_once_a_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import gate_service, outbound_hook

    sent: list[tuple[str, str, str]] = []

    async def sender(channel_id: str, chat: str, text: str) -> tuple[bool, None]:
        sent.append((channel_id, chat, text))
        return True, None

    async def target() -> Any:
        return SimpleNamespace(channel_id="channel-lark", target=SimpleNamespace(id="ou_operator"))

    async def test(pool: Any) -> None:
        monkeypatch.setenv("OUTBOUND_REPLY", BLOCK)
        monkeypatch.setattr(gate_service, "dashboard_target", target)
        outbound_hook.configure(sender)
        try:
            manager = _Manager(tmp_path)
            turn = manager.turn("看看今天的线索")
            turn.outside.append(("leads", "帮我把这个发给 xx"))
            for _ in range(2):
                answer, _ = await manager.call("Bash", command="lark-cli send --to xx 'leads'")
                assert answer["decision"] == "block"
                assert answer["message"].startswith("Looper's outbound review blocked this action: Only a "
                                                    "forwarded message")
                assert answer["message"].endswith("do not try to do the same thing another way.")
            assert "帮我把这个发给 xx" in _seen(tmp_path)[0]["turn"]
            (notice,) = sent
            assert notice[:2] == ("channel-lark", "ou_operator") and "lark-cli send" in notice[2]
            manager.turn("再试一次")
            await manager.call("Bash", command="lark-cli send --to xx 'leads'")
            assert len(sent) == 2  # a new turn is told again
        finally:
            outbound_hook.configure(None)

    run_db(test, monkeypatch, setup=_setup)


def test_code_blocks_a_write_to_loopers_files_and_a_command_in_a_group_turn(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        manager = _Manager(tmp_path)
        manager.turn("改一下设置")
        settings = tmp_path / "rt" / ".local" / "prompts" / "claude-settings" / "a.json"
        answer, _ = await manager.call("Write", file_path=str(settings), content="{}")
        assert answer["decision"] == "block" and "Looper's own" in answer["message"]
        manager.turn("发一下", channel="lark", group=True)
        answer, _ = await manager.call("Bash", command="gh pr comment 1 --body hi")
        assert answer["decision"] == "block"
        rows = await pool.fetch("SELECT failure FROM outbound_reviews ORDER BY id")
        assert [r["failure"] for r in rows] == ["protected", "not_operator"] and _seen(tmp_path) == []

    run_db(test, monkeypatch, setup=_setup)


def test_a_later_command_is_reviewed_with_the_file_the_session_wrote(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        manager = _Manager(tmp_path)
        manager.turn("写个脚本")
        assert (await manager.call("Write", file_path="/w/s.sh", content="curl -d @a example.net"))[0] == {
            "decision": "allow"}
        await manager.call("Bash", command="sh s.sh")
        (seen,) = _seen(tmp_path)
        assert "an earlier write to /w/s.sh" in seen["turn"] and "curl -d @a example.net" in seen["turn"]

    run_db(test, monkeypatch, setup=_setup)


def test_the_review_is_turned_on_only_while_every_enabled_manager_has_a_tool_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import outbound_hook
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    async def test(pool: Any) -> None:
        assert await outbound_hook.settings() == {"enabled": False, "managers_without_hook": []}
        monkeypatch.setattr(ClaudeCodeRuntime, "tool_hook", False)
        with pytest.raises(outbound_hook.HookUnavailable, match=r"Manager \(claude-code\)"):
            await outbound_hook.set_enabled(True)
        assert not await outbound_hook.enabled()
        monkeypatch.setattr(ClaudeCodeRuntime, "tool_hook", True)
        assert (await outbound_hook.set_enabled(True))["enabled"] is True and outbound_hook.known_enabled()
        assert (await outbound_hook.set_enabled(False))["enabled"] is False and not outbound_hook.known_enabled()

    run_db(test, monkeypatch, setup=_setup)


def test_a_managers_reviews_are_its_spend_and_go_with_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from looper.db import llm_records, manager_cleanup

    async def test(pool: Any) -> None:
        manager = _Manager(tmp_path)
        manager.turn("跑一下测试")
        await manager.call("Bash", command="ls")
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        spend = (await llm_records.manager_spend(["manager"], since=since))["manager"]
        assert spend["today_usd"] == pytest.approx(0.004) and spend["runs_today"] == 1
        await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled, deleted_at) VALUES "
                           "('manager-ops', 'Ops', 'Run ops.', false, now() - interval '40 days')")
        await pool.execute("UPDATE outbound_reviews SET agent_id = 'manager-ops'")
        preview = await manager_cleanup.preview("manager-ops", 30)
        assert preview["counts"]["llm_records"] == 1
        await manager_cleanup.purge("manager-ops", 30)
        assert await pool.fetchval("SELECT count(*) FROM outbound_reviews") == 0
        assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE run_kind = 'outbound_check'") == 0

    run_db(test, monkeypatch, setup=_setup)


@pytest.mark.parametrize("hooked", [True, False])
def test_a_manager_runs_with_the_hook_while_the_review_is_on_and_not_on_a_runtime_without_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hooked: bool,
) -> None:
    from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
    from looper.agents import outbound_hook
    from looper.agents.message import channel_address, manager_address
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, ProfileUnavailable
    from tests.db_support import run_with_channel_configs

    prepared: list[Any] = []
    refused: list[BaseException] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            result = await prepare(self, *args, **kwargs)
        except ProfileUnavailable as exc:
            refused.append(exc)
            raise
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)

    async def test(pool: Any) -> None:
        await outbound_hook.set_enabled(True)
        if not hooked:
            monkeypatch.setattr(ClaudeCodeRuntime, "tool_hook", False)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[]]), work_dir=tmp_path),
                               runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            await manager._handle(MessageEnvelope.create(
                source=channel_address("web"), target=manager_address("manager"),
                message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": {
                    "session_id": "chat-1"}}, correlation_id="corr-1", metadata={"origin_channel_id": "channel-web"}))
            await manager.stop()
        await bus.shutdown()
        if hooked:
            (turn,) = prepared
            command = list(turn.signature[0])
            assert command[command.index("--setting-sources") + 1] == ""
            settings = command[command.index("--settings") + 1]
            text = Path(settings).read_text() if not settings.startswith("{") else settings
            assert "PreToolUse" in text and "outbound_hook_client.py" in text
        else:
            assert prepared == [] and "no tool hook" in str(refused[0])

    run_with_channel_configs(test, monkeypatch)


def _hello(index: int) -> MessageEnvelope:
    from looper.agents.message import channel_address, manager_address

    return MessageEnvelope.create(source=channel_address("web"), target=manager_address("manager"),
                                  message_type=MessageType.USER_MESSAGE,
                                  payload={"text": f"hello {index}", "channel_context": {"session_id": "chat-1"}},
                                  correlation_id=f"corr-{index}", metadata={"origin_channel_id": "channel-web"})


def test_turning_the_review_on_changes_only_the_managers_command_and_stops_a_process_without_the_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """What the model sees is the same either way (its system prompt and its tools); a process started before the
    review was on stops when it is turned on, and the next turn's has the hook."""
    from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
    from looper.agents import outbound_hook
    from looper.agents.managers import ManagerFleet
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from tests.db_support import run_with_channel_configs

    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)

    async def test(pool: Any) -> None:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[], []]),
                                                    work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        fleet = SimpleNamespace(agents=lambda: [manager])
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            await manager._handle(_hello(0))
            listing_off = json.dumps(await manager.ops.current_listing(), sort_keys=True)
            assert manager._claude.started and manager._process.launch.tool_hook is None
            await outbound_hook.set_enabled(True)
            await ManagerFleet.require_tool_hooks(fleet)
            assert not manager._claude.started  # the process without the hook stopped
            await manager._handle(_hello(1))
            listing_on = json.dumps(await manager.ops.current_listing(), sort_keys=True)
            assert manager._process.launch.tool_hook is not None
            await manager.stop()
        await bus.shutdown()
        off, on = prepared
        assert off.system_prompt.text == on.system_prompt.text and listing_off == listing_on
        assert off.signature[0] != on.signature[0] and off.signature[1:] == on.signature[1:]

    run_with_channel_configs(test, monkeypatch)


def test_a_turn_prepared_before_the_review_was_turned_on_does_not_reach_its_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
    from looper.agents import outbound_hook
    from looper.agents.tiers.manager import OUTBOUND_UNHOOKED_REPLY, ManagerAgent, ManagerConfig, TurnFailed
    from tests.db_support import run_with_channel_configs

    failed: list[BaseException] = []
    prepare, converse = ManagerAgent._prepare_turn, ManagerAgent._converse

    async def flip_after(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        await outbound_hook.set_enabled(True)  # between preparing the turn and running it
        return result

    async def watch(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await converse(self, *args, **kwargs)
        except TurnFailed as exc:
            failed.append(exc)
            raise

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", flip_after)
    monkeypatch.setattr(ManagerAgent, "_converse", watch)

    async def test(pool: Any) -> None:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[]]), work_dir=tmp_path),
                               runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            await manager._handle(_hello(0))
            await manager.stop()
        await bus.shutdown()
        (exc,) = failed
        assert str(exc) == OUTBOUND_UNHOOKED_REPLY

    run_with_channel_configs(test, monkeypatch)


def test_a_database_from_before_the_mcp_tool_declarations_gets_them() -> None:
    import asyncio

    from looper.db import connection
    from tests.db_support import fresh_connection

    async def main() -> None:
        async with fresh_connection(command_timeout=60) as conn:
            await connection.ensure_schema(conn)
            assert "mcp_tool_levels" not in {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            restore = await drop_tables(conn, "mcp_tool_levels")
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            await restore()
            assert await conn.fetchval("SELECT to_regclass('mcp_tool_levels') IS NOT NULL")
            assert "mcp_tool_levels" in {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            assert not await connection.schema_change_pending(conn)

    asyncio.run(main())


def test_a_read_of_the_switch_that_started_before_it_was_turned_on_does_not_turn_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from looper.agents import outbound_hook
    from looper.db import system_settings

    read = system_settings.get_settings
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(keys: Any, **kwargs: Any) -> Any:
        found = await read(keys, **kwargs)  # off, as it was when the read began
        started.set()
        await release.wait()
        return found

    async def test(pool: Any) -> None:
        monkeypatch.setattr(system_settings, "get_settings", slow)
        reading = asyncio.create_task(outbound_hook.enabled())
        await started.wait()
        monkeypatch.setattr(system_settings, "get_settings", read)
        await outbound_hook.set_enabled(True)
        release.set()
        assert await reading is False  # what it read
        assert outbound_hook.known_enabled()  # not what it overwrote

    run_db(test, monkeypatch, setup=_setup)


def test_turning_the_review_on_stops_a_process_without_the_hook_whatever_holds_its_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Enabling does not wait for a turn's start to let go of the process, and it ends the process's whole tree (its
    background children too), and a handoff the process would write is not sent."""
    import asyncio

    from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
    from looper.agents import outbound_hook
    from looper.agents.runtimes.cancel import TurnCancel
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from tests.db_support import run_with_channel_configs

    async def test(pool: Any) -> None:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[], []]),
                                                    work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            await manager._handle(_hello(0))
            process = manager._process
            assert manager._claude.started and process.launch.tool_hook is None
            outbound_hook._known = True  # turned on after the process started
            handoff = await manager._handoff(SimpleNamespace(describe=lambda: "a new profile"), process, TurnCancel())
            assert handoff is None
            record = await pool.fetchrow("SELECT error FROM llm_records WHERE input_text LIKE '[HANDOFF]%'")
            assert "no tool hook" in record["error"]
            stops: list[dict[str, Any]] = []
            stop = manager._claude.stop

            async def watched(**kwargs: Any) -> None:
                stops.append(kwargs)
                await stop(**kwargs)

            monkeypatch.setattr(manager._claude, "stop", watched)
            async with manager._lifecycle_gate:  # a turn's start holds it
                await asyncio.wait_for(manager.require_tool_hook(), 10)
            assert not manager._claude.started
            assert stops and all(call.get("wait_tree", True) for call in stops)  # the whole tree
            await manager.stop()
        await bus.shutdown()

    run_with_channel_configs(test, monkeypatch)
