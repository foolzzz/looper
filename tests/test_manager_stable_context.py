"""With ``manager.stable_context`` on, the Manager's turns start with one state block: whole when its session may
not hold the last one, else one unchanged line or the changes since it (``state_view``).

Claude Code is a subprocess stand-in that saves each user turn; the database is ``agent_db``'s in-memory one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers.manager import TOOLS_CONTRACT, ManagerAgent, ManagerConfig
from looper.agents.tiers.state_view import STABLE_CONTEXT_CONTRACT, STABLE_CONTEXT_KEY
from tests.test_manager_reply import _fake_claude, _project

WEB = channel_address("web")
PROJECTS = [_project(f"project-{i}", f"/srv/p{i}", f"what project {i} is about " * 4, project_id=f"proj-{i:012d}")
            for i in range(10)]


def _claude(*, compact_on: tuple[int, ...] = ()) -> str:
    """A resident Claude Code that saves each user turn to ``turns.jsonl`` (numbered across its processes) and
    answers it in session ``sess-1``; on the turns ``compact_on`` it reports a compaction first."""
    return (
        f"compact = {list(compact_on)!r}\n"
        "for line in sys.stdin:\n"
        "    open('turns.jsonl', 'a').write(json.dumps(json.loads(line)['message']['content']) + '\\n')\n"
        "    number = sum(1 for _ in open('turns.jsonl'))\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)\n"
        "    if number in compact:\n"
        "        print(json.dumps({'type': 'system', 'subtype': 'compact_boundary',\n"
        "                          'compact_metadata': {'trigger': 'auto', 'pre_tokens': 1000}}), flush=True)\n"
        "    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'ok'}]}}),\n"
        "          flush=True)\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok',\n"
        "                      'session_id': 'sess-1'}), flush=True)\n"
    )


def _send(manager: ManagerAgent, text: str, index: int) -> Any:
    return manager._handle(MessageEnvelope.create(
        source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": "chat-1"}}, correlation_id=f"corr-{index}",
        metadata={"origin_channel_id": "channel-web"}))


def _chat(root: Path, messages: list[str], *, body: str | None = None, between: Any = None,
          managers: int = 1) -> list[str]:
    """Send ``messages`` to a Manager (``managers`` of them one after another, as a server restart would, each
    sending every message); ``between(index, manager)`` runs before each message after the first. The user turns
    Claude got, in order."""

    async def run() -> None:
        claude = _fake_claude(root, body or _claude())
        for _ in range(managers):
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(config=ManagerConfig(claude_bin=claude, work_dir=root), runtime_root=root)
            manager._bus = bus
            try:
                for index, text in enumerate(messages):
                    if index and between is not None:
                        await between(index, manager)
                    await _send(manager, text, index)
            finally:
                await manager.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 60))
    path = root / "turns.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def stable(agent_db: Any) -> Any:
    agent_db.get_settings.return_value = {STABLE_CONTEXT_KEY: "true"}
    agent_db.list_projects.return_value = PROJECTS
    return agent_db


def _views(agent_db: Any) -> list[str]:
    return [call.args[1]["looper_state"]["view"] for call in agent_db.add_metadata.await_args_list
            if "looper_state" in call.args[1]]


def _whole(turn: str) -> bool:
    return turn.startswith("Looper state (rev ") and "\nProjects:\n  - project-0 " in turn


def test_off_a_turn_starts_with_the_sections_as_before(agent_db: Any, tmp_path: Path) -> None:
    agent_db.list_projects.return_value = PROJECTS[:1]
    (turn,) = _chat(tmp_path, ["hello"])
    assert turn == ("Projects:\n  - project-0 (default directory /srv/p0): " + ("what project 0 is about " * 4)[:79]
                    + "…\n\nChannel: the Looper dashboard chat\n\nhello")
    assert _views(agent_db) == []


def test_the_first_turn_starts_with_the_whole_block(stable: Any, tmp_path: Path) -> None:
    (turn,) = _chat(tmp_path, ["hello"])
    head, _, rest = turn.partition("\n")
    assert head.startswith("Looper state (rev ") and rest.startswith("Projects:\n  - project-0 ")
    assert turn.endswith("\n\nChannel: the Looper dashboard chat\n\nhello")
    assert _views(stable) == ["whole"]


def test_an_unchanged_state_is_one_line(stable: Any, tmp_path: Path) -> None:
    first, second = _chat(tmp_path, ["hello", "again"])
    rev = first.partition("\n")[0].removeprefix("Looper state (rev ").removesuffix("):")
    assert second == f"Looper state: unchanged since rev {rev}\n\nChannel: the Looper dashboard chat\n\nagain"
    assert _views(stable) == ["whole", "unchanged"]


def test_a_small_change_lists_it_against_the_whole_block(stable: Any, tmp_path: Path) -> None:
    async def add_ready_task(index: int, manager: ManagerAgent) -> None:
        stable.list_tasks.return_value = [
            {"id": f"task-{index:012d}", "project_name": "project-1", "requirement": f"build {index}"}]

    first, second, third = _chat(tmp_path, ["hello", "again", "and again"], between=add_ready_task)
    rev = first.partition("\n")[0].removeprefix("Looper state (rev ").removesuffix("):")
    # Each note is against the whole block: the third does not list the task the second added as removed.
    for turn, task in ((second, "task-000000000001"), (third, "task-000000000002")):
        assert turn.startswith(f"Looper state: changes since rev {rev} (+ added, - removed):\n"
                               f"+Ready tasks (created, not started; newest first):\n"
                               f"+  - {task} (project project-1): build {task[-1]}\n\nChannel: ")
    assert _views(stable) == ["whole", "changes", "changes"]


def test_a_server_restart_sends_the_whole_block(stable: Any, tmp_path: Path) -> None:
    turns = _chat(tmp_path, ["hello", "again"], managers=2)  # the second Manager resumes the saved session
    assert [_whole(turn) for turn in turns] == [True, False, True, False]


def test_a_new_process_sends_the_whole_block(stable: Any, tmp_path: Path) -> None:
    async def stop_claude(index: int, manager: ManagerAgent) -> None:
        if index == 2:
            await manager._claude.stop()  # an idle close: the next turn's process resumes the session

    turns = _chat(tmp_path, ["hello", "again", "and again"], between=stop_claude)
    assert [_whole(turn) for turn in turns] == [True, False, True]


def test_new_sends_the_whole_block(stable: Any, tmp_path: Path) -> None:
    turns = _chat(tmp_path, ["hello", "again", "/new", "after"])
    assert [_whole(turn) for turn in turns] == [True, False, True]  # /new is Looper's own


def test_a_context_full_handoff_sends_the_whole_block(stable: Any, tmp_path: Path) -> None:
    async def fill(index: int, manager: ManagerAgent) -> None:
        if index == 2:  # the session reports its context full, as the api runtime's does (ReportsContext)
            manager._claude.context_tokens, manager._claude.context_limit = 200_001, 200_000
            manager._claude.context_full = True

    first, second, handoff, third = _chat(tmp_path, ["hello", "again", "after"], between=fill)
    assert _whole(first) and not _whole(second)
    assert handoff.startswith("[HANDOFF]")
    assert "\n\nLooper state (rev " in third and third.endswith("\n\nafter")  # after the new session's brief


def test_a_compaction_sends_the_whole_block_on_the_next_turn(stable: Any, tmp_path: Path) -> None:
    turns = _chat(tmp_path, ["hello", "again", "after", "later"], body=_claude(compact_on=(2,)))
    assert [_whole(turn) for turn in turns] == [True, False, True, False]


def _claude_with_a_turn_of_its_own(before: int) -> str:
    """A resident Claude Code that marks each user turn with its replay (``--replay-user-messages``) and, once it has
    read user turn ``before``, first runs a turn of its own (no replay: background work ended) in which it compacts
    its context, then answers that message."""
    return (
        "def say(event):\n"
        "    print(json.dumps(event), flush=True)\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    open('turns.jsonl', 'a').write(json.dumps(message['message']['content']) + '\\n')\n"
        "    number = sum(1 for _ in open('turns.jsonl'))\n"
        f"    if number == {before}:\n"
        "        say({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'})\n"
        "        say({'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': {'trigger': 'auto'}})\n"
        "        say({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'background done'}]}})\n"
        "        say({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'background done',\n"
        "             'session_id': 'sess-1'})\n"
        "    say({'type': 'user', 'isReplay': True, 'uuid': message.get('uuid'), 'message': message['message']})\n"
        "    say({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'})\n"
        "    say({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'ok'}]}})\n"
        "    say({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok', 'session_id': 'sess-1'})\n"
    )


def test_a_turn_of_its_own_while_a_message_waits_sends_the_whole_block_next(stable: Any, tmp_path: Path) -> None:
    # The second message was sent as "unchanged"; before Claude starts it, Claude runs a turn of its own that compacts.
    turns = _chat(tmp_path, ["hello", "again", "after", "later"], body=_claude_with_a_turn_of_its_own(2))
    assert [_whole(turn) for turn in turns] == [True, False, True, False]
    assert _views(stable) == ["whole", "unchanged", "whole", "unchanged"]


def test_a_turn_of_its_own_after_the_state_was_rendered_sends_it_whole(
    stable: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = ManagerAgent._start_turn

    async def own_turn_begins_after(self: ManagerAgent, token: Any, prepared: Any) -> None:
        await start(self, token, prepared)  # the turn's state is rendered: "unchanged"
        if prepared.message == "again":  # Claude begins a turn of its own before the message is sent
            await self._autonomous_turns(self._process.command, prepared.system_prompt, prepared.launch)(None)

    monkeypatch.setattr(ManagerAgent, "_start_turn", own_turn_begins_after)
    turns = _chat(tmp_path, ["hello", "again"])
    assert [_whole(turn) for turn in turns] == [True, True]
    assert [call.args[1]["looper_state"] for call in stable.add_metadata.await_args_list
            if "looper_state" in call.args[1]][-1]["reason"] == "a turn of its own"


def test_a_runtime_without_resident_compaction_events_gets_the_whole_block_every_turn(
    stable: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(CLAUDE_CODE), "resident_compaction_events", False)
    turns = _chat(tmp_path, ["hello", "again", "and again"])
    assert [_whole(turn) for turn in turns] == [True, True, True]
    assert {call.args[1]["looper_state"]["reason"] for call in stable.add_metadata.await_args_list
            if "looper_state" in call.args[1]} == {"no compaction events"}


@pytest.mark.parametrize("on", [False, True])
def test_on_an_open_gate_says_when_it_opened_instead_of_how_long_ago(agent_db: Any, tmp_path: Path, on: bool) -> None:
    from datetime import datetime, timezone

    agent_db.open_gates.return_value = [{
        "task_id": "task-1", "project_name": "calc", "step_key": "g1", "topic": "storage",
        "opened_at": datetime(2026, 11, 20, 9, 42, 30, tzinfo=timezone.utc),
        "agenda": [{"id": "q1", "question": "PostgreSQL or SQLite?", "status": "open"}]}]
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._stable_context = on
    heading, line, blank = asyncio.run(manager._open_gates_section(None))
    # Off, as before: how long it has been open, which changes every minute; on, a time that does not.
    head, _, tail = line.partition(": open for " if not on else ": opened 2026-11-20 09:42 UTC, ")
    assert head == '  - task task-1 (project calc), gate g1 "storage"' and tail
    assert tail.endswith("waiting for the user; chat: the task page only; open questions: q1 PostgreSQL or SQLite?")
    assert ("open for" in line) is not on


@pytest.mark.parametrize("on", [False, True])
def test_the_contract_tells_of_the_block_only_when_on(tmp_path: Path, on: bool) -> None:
    from looper.agents.agent_settings import resolve

    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._stable_context = on
    prompt = manager._composed_prompt(resolve("manager", {}))
    assert prompt.layers["contract"]["name"] == ("TOOLS_CONTRACT+STABLE_CONTEXT_CONTRACT" if on else "TOOLS_CONTRACT")
    assert prompt.text.endswith(TOOLS_CONTRACT + ("\n" + STABLE_CONTEXT_CONTRACT if on else ""))
    assert ("Looper state" in prompt.text) is on
