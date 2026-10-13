"""The Manager must always answer a chat message, even when Claude fails."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from loopx.agents.tiers.manager import ManagerAgent, ManagerConfig

WEB = channel_address("web")


def _fake_claude(tmp: Path, body: str) -> str:
    """A stand-in `claude` binary that ignores its arguments."""
    script = tmp / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        "open('argv.json', 'w').write(json.dumps(sys.argv[1:]))\n"
        f"{body}\n"
    )
    script.chmod(0o755)
    return str(script)


def _ask(body: str) -> dict[str, Any]:
    """Send one chat message to a Manager backed by a fake claude.

    Returns the final reply ``text``, the streamed ``deltas``, the fake's
    ``argv`` and, when the fake wrote one, its ``pid``.
    """

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(
                config=ManagerConfig(claude_bin=_fake_claude(root, body), work_dir=root),
                runtime_root=root,
            )
            manager._bus = bus
            await manager._handle(MessageEnvelope.create(
                source=WEB,
                target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE,
                payload={"text": "hello"},
                correlation_id="corr-1",
            ))
            await manager.stop()
            replies = []
            deltas = []
            queue = bus.get_queue(WEB.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    replies.append(envelope)
                elif envelope.message_type == MessageType.STATUS_UPDATE and envelope.payload.get("delta"):
                    deltas.append(envelope.payload["text"])
            await bus.shutdown()
            assert len(replies) == 1, "the Manager must send exactly one final reply"
            assert replies[0].correlation_id == "corr-1"
            pid_file = root / "pid"
            return {
                "text": replies[0].payload["text"],
                "deltas": deltas,
                "argv": json.loads((root / "argv.json").read_text()),
                "pid": int(pid_file.read_text()) if pid_file.exists() else None,
            }

    return asyncio.run(_run())


@pytest.mark.usefixtures("agent_db")
class TestManagerReply:
    def test_long_stream_json_line_is_read(self) -> None:
        # Claude's init line lists every tool and skill; it can exceed 64 KiB.
        text = _ask(
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'tools': ['t' * 200_000]}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi there'}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'hi there'}))"
        )["text"]
        assert text == "hi there"

    def test_reply_is_recorded(self, agent_db: Any) -> None:
        _ask(
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-x', 'session_id': 's-9'}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi'}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'hi'}))"
        )
        start = agent_db.start_record.await_args.kwargs
        assert start["agent_id"] == "manager" and start["input_text"] == "hello"
        assert "--append-system-prompt" in start["metadata"]["command"]
        finish = agent_db.finish_record.await_args.kwargs
        assert (finish["status"], finish["output_text"], finish["model"], finish["session_id"]) == (
            "completed", "hi", "claude-x", "s-9")

    def test_empty_run_is_reported(self) -> None:
        text = _ask("sys.exit(1)")["text"]
        assert "Manager" in text and "1" in text

    def test_read_failure_is_reported_and_claude_is_killed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents import claude_cli

        async def _broken_lines(stream: Any) -> Any:
            yield await stream.readline()
            raise OSError("pipe broke")

        monkeypatch.setattr(claude_cli, "iter_lines", _broken_lines)
        reply = _ask(
            "open('pid', 'w').write(str(os.getpid()))\n"
            "print('{}', flush=True)\n"
            "time.sleep(30)"
        )
        text, pid = reply["text"], reply["pid"]
        assert "pipe broke" in text
        assert "Manager" in text
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # type: ignore[arg-type]


def _delta(text: str, parent: str | None = None) -> str:
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        "parent_tool_use_id": parent,
    }
    return f"print(json.dumps({event!r}), flush=True)\n"


def _message(text: str, parent: str | None = None) -> str:
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "parent_tool_use_id": parent,
    }
    return f"print(json.dumps({event!r}), flush=True)\n"


_RESULT = "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}))\n"


@pytest.mark.usefixtures("agent_db")
class TestManagerStreaming:
    def test_reply_streams_token_by_token(self) -> None:
        reply = _ask(_delta("杭州") + _delta("是浙江") + _delta("的省会。") + _message("杭州是浙江的省会。") + _RESULT)
        assert "--include-partial-messages" in reply["argv"]
        # Each partial chunk is pushed once; the full message is not pushed again.
        assert reply["deltas"] == ["杭州", "是浙江", "的省会。"]
        assert reply["text"] == "杭州是浙江的省会。"

    def test_whole_messages_still_stream_without_partials(self) -> None:
        reply = _ask(_message("first part. ") + _message("second part.") + _RESULT)
        assert reply["deltas"] == ["first part. ", "second part."]
        assert reply["text"] == "first part. second part."

    def test_subagent_chunks_are_not_streamed(self) -> None:
        reply = _ask(
            _delta("internal note", parent="toolu_1")
            + _message("internal note", parent="toolu_1")
            + _delta("答案")
            + _message("答案")
            + _RESULT
        )
        assert reply["deltas"] == ["答案"]
        assert reply["text"] == "答案"


def _resident(*, exit_after: int | None = None, die_on: int | None = None, ignore_eof: bool = False) -> str:
    """A fake claude that reads user turns from stdin like the real one.

    It appends its pid to ``pids`` on start and answers each turn with
    ``turn N: <message>`` (the user's message, after the context sections)
    and a result. Like Claude, the result's
    ``total_cost_usd`` is the process's running total (0.25 per turn). It
    exits on EOF, after turn ``exit_after``, or without a result during turn
    ``die_on``. With ``ignore_eof`` it keeps running after stdin closes.
    """
    return (
        "open('pids', 'a').write(f'{os.getpid()}\\n')\n"
        "turn = 0\n"
        "for line in sys.stdin:\n"
        "    turn += 1\n"
        "    text = json.loads(line)['message']['content'].rpartition('\\n\\n')[2]\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)\n"
        f"    if turn == {die_on!r}:\n"
        "        sys.exit(3)\n"
        "    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': f'turn {turn}: {text}'}]}}), flush=True)\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'sess-1',\n"
        "                      'total_cost_usd': turn * 0.25}), flush=True)\n"
        f"    if turn == {exit_after!r}:\n"
        "        break\n"
        + ("time.sleep(30)\n" if ignore_eof else "")
    )


def _converse(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    messages: list[str],
    *,
    prompts: list[str] | None = None,
    between: Any = None,
) -> dict[str, Any]:
    """Send ``messages`` one at a time to one Manager backed by a fake claude, then stop it.

    ``prompts[i]`` is the system prompt the database holds for message ``i``;
    ``between(spawned)`` runs before every message after the first. Returns the
    final ``replies``, every started Claude as ``(argv, process)`` in
    ``spawned``, whether each was ``running`` just before ``stop()``, and the
    ``pids`` the fake logged.
    """
    from loopx.db import agent_configs

    spawned: list[tuple[list[str], Any]] = []
    real_exec = asyncio.create_subprocess_exec

    async def _exec(*args: Any, **kwargs: Any) -> Any:
        process = await real_exec(*args, **kwargs)
        spawned.append(([str(a) for a in args[1:]], process))
        return process

    current: dict[str, int] = {"message": 0}

    async def _get_config(agent_id: str) -> dict[str, Any] | None:
        if not prompts:
            return None
        return {"system_prompt": prompts[current["message"]]}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(agent_configs, "get_config", _get_config)

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(
                config=ManagerConfig(claude_bin=_fake_claude(root, body), work_dir=root),
                runtime_root=root,
            )
            manager._bus = bus
            queue = bus.get_queue(WEB.agent_id)
            replies: list[str] = []
            for index, text in enumerate(messages):
                current["message"] = index
                if index and between is not None:
                    await between(spawned)
                await manager._handle(MessageEnvelope.create(
                    source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": text}, correlation_id=f"corr-{index}",
                ))
                answers = []
                while not queue.empty():
                    envelope = queue.get_nowait()
                    if envelope.message_type == MessageType.AGENT_REPLY:
                        answers.append(envelope.payload["text"])
                assert len(answers) == 1, "each message gets exactly one final reply"
                replies.append(answers[0])
            running = [process.returncode is None for _, process in spawned]
            await manager.stop()
            await bus.shutdown()
            pid_file = root / "pids"
            pids = pid_file.read_text().split() if pid_file.exists() else []
            return {"replies": replies, "spawned": spawned, "running": running, "pids": pids}

    return asyncio.run(_run())


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


async def _first_claude_exits(spawned: list[tuple[list[str], Any]]) -> None:
    await spawned[0][1].wait()


@pytest.mark.usefixtures("agent_db")
class TestManagerResidentClaude:
    def test_one_process_serves_every_message(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        chat = _converse(monkeypatch, _resident(), ["hello", "again"])
        assert chat["replies"] == ["turn 1: hello", "turn 2: again"]
        assert len(chat["spawned"]) == 1 and len(chat["pids"]) == 1
        # Each message is still one recorded run, with the resident process's command.
        starts = [call.kwargs for call in agent_db.start_record.await_args_list]
        assert [start["input_text"] for start in starts] == ["hello", "again"]
        assert all(start["metadata"]["command"][1:] == chat["spawned"][0][0] for start in starts)
        finishes = [call.kwargs for call in agent_db.finish_record.await_args_list]
        assert [finish["status"] for finish in finishes] == ["completed", "completed"]
        # Claude reports the process's running cost; each record holds its own turn's share.
        assert [finish["cost_usd"] for finish in finishes] == [0.25, 0.25]

    def test_messages_go_on_stdin_not_in_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat = _converse(monkeypatch, _resident(), ["hello"])
        ((argv, _),) = chat["spawned"]
        assert _flag(argv, "--input-format") == "stream-json"
        assert _flag(argv, "--output-format") == "stream-json"
        assert {"--print", "--verbose", "--include-partial-messages"} <= set(argv)
        takes_value = {"--input-format", "--output-format", "--append-system-prompt", "--resume"}
        positional = [arg for i, arg in enumerate(argv) if not arg.startswith("--") and argv[i - 1] not in takes_value]
        assert positional == []
        assert _flag(argv, "--resume") is None  # no session yet

    def test_claude_that_exited_is_restarted_with_its_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat = _converse(monkeypatch, _resident(exit_after=1), ["hello", "again"], between=_first_claude_exits)
        assert chat["replies"] == ["turn 1: hello", "turn 1: again"]
        (first, _), (second, _) = chat["spawned"]
        assert _flag(first, "--resume") is None
        assert _flag(second, "--resume") == "sess-1"
        assert len(chat["pids"]) == 2

    def test_claude_dying_mid_turn_is_reported_then_restarted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat = _converse(monkeypatch, _resident(die_on=2), ["hello", "again", "third"])
        first, failed, third = chat["replies"]
        assert first == "turn 1: hello"
        assert "Manager" in failed and "3" in failed  # the exit code
        assert third == "turn 1: third"
        assert [_flag(argv, "--resume") for argv, _ in chat["spawned"]] == [None, "sess-1"]

    def test_new_system_prompt_restarts_claude(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        chat = _converse(monkeypatch, _resident(), ["hello", "again", "third"],
                         prompts=["prompt A", "prompt B", "prompt B"])
        assert chat["replies"] == ["turn 1: hello", "turn 1: again", "turn 2: third"]
        # A new process starts its running cost from zero.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [0.25, 0.25, 0.25]
        (first, _), (second, _) = chat["spawned"]
        assert _flag(first, "--append-system-prompt").startswith("prompt A")
        assert _flag(second, "--append-system-prompt").startswith("prompt B")
        assert _flag(second, "--resume") == "sess-1"
        assert chat["running"] == [False, True]  # the old process was stopped and reaped

    @pytest.mark.parametrize(("ignore_eof", "returncode"), [(False, 0), (True, -9)])
    def test_stop_ends_and_reaps_claude(
        self, monkeypatch: pytest.MonkeyPatch, ignore_eof: bool, returncode: int,
    ) -> None:
        from loopx.agents import claude_cli

        monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)
        chat = _converse(monkeypatch, _resident(ignore_eof=ignore_eof), ["hello"])
        ((_, process),) = chat["spawned"]
        assert chat["running"] == [True]
        # Closing stdin ends Claude; one that keeps running is killed after the grace period.
        assert process.returncode == returncode



def _task_json(**fields: Any) -> str:
    """A fake claude that saves the user turn it gets to ``turn.txt`` and answers with this task JSON."""
    report = {"action": "task_request", "goal_id": "g", "structured_requirement": "build it",
              "acknowledgment": "ok", **fields}
    text = "```json\n" + json.dumps(report) + "\n```"
    return (
        "open('turn.txt', 'w').write(json.loads(sys.stdin.readline())['message']['content'])\n"
        f"print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': {text!r}}}]}}}}))\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': {text!r}}}))"
    )


def _ask_for_task(body: str) -> SimpleNamespace:
    """Run one Manager turn; return its ``reply``, any TASK_REQUEST ``requests``, its ``argv`` and the user ``turn``."""

    async def _run() -> SimpleNamespace:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(
                config=ManagerConfig(claude_bin=_fake_claude(root, body), work_dir=root),
                runtime_root=root,
            )
            manager._bus = bus
            await manager._handle(MessageEnvelope.create(
                source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={"text": "确认"}, correlation_id="corr-1",
            ))
            await manager.stop()
            reply = ""
            queue = bus.get_queue(WEB.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    reply = envelope.payload["text"]
            router = bus.get_queue("task-router")
            requests = []
            while not router.empty():
                requests.append(router.get_nowait())
            argv = json.loads((root / "argv.json").read_text())
            turn = (root / "turn.txt").read_text()
            await bus.shutdown()
            return SimpleNamespace(reply=reply, requests=requests, argv=argv, turn=turn)

    return asyncio.run(_run())


def _project(name: str, work_dir: str = "", description: str = "", project_id: str = "proj-111111111111") -> dict[str, Any]:
    return {"id": project_id, "name": name, "description": description, "work_dir": work_dir}


TETRIS_DIR = os.path.join(os.path.expanduser("~"), "projects", "tetris")


@pytest.mark.usefixtures("agent_db")
class TestManagerTaskPlacement:
    def test_task_carries_the_chosen_directory(self, agent_db: Any) -> None:
        result = _ask_for_task(_task_json(project="tetris", work_dir="~/projects/tetris"))
        (request,) = result.requests
        assert request.payload["work_dir"] == TETRIS_DIR
        assert result.reply.startswith("ok")
        # The project and directory rule is code-owned and appended to whatever prompt the DB holds.
        prompt = result.argv[result.argv.index("--append-system-prompt") + 1]
        assert '"work_dir"' in prompt and '"project"' in prompt

    def test_projects_section_starts_each_user_turn(self, agent_db: Any) -> None:
        agent_db.list_projects.return_value = [
            _project("tetris", "/srv/tetris", "falling\nblocks " + "x" * 100),
            _project("notes", project_id="proj-222222222222"),
        ]
        turn = _ask_for_task(_task_json(project="tetris")).turn
        lines = turn.splitlines()
        assert lines[0] == "Projects:"
        assert lines[1].startswith("  - tetris (default directory /srv/tetris): falling blocks xxx")
        assert len(lines[1].partition("): ")[2]) == 80  # the description, cut to 80 characters
        assert lines[2] == "  - notes (no default directory)"
        assert lines[-1] == "确认"

    def test_projects_section_says_when_there_are_none(self) -> None:
        turn = _ask_for_task(_task_json(project="tetris")).turn
        assert turn.splitlines()[:2] == ["Projects:", "  (none yet)"]

    def test_existing_project_is_matched_by_name(self, agent_db: Any) -> None:
        agent_db.find_project_by_name.return_value = _project("Tetris", "/srv/tetris")
        result = _ask_for_task(_task_json(project="tetris", work_dir="~/projects/tetris"))
        agent_db.find_project_by_name.assert_awaited_once_with("tetris")
        agent_db.create_project.assert_not_awaited()
        (request,) = result.requests
        assert (request.payload["project_id"], request.payload["work_dir"]) == ("proj-111111111111", TETRIS_DIR)
        assert result.reply == "ok"

    def test_work_dir_defaults_to_the_projects_directory(self, agent_db: Any) -> None:
        agent_db.find_project_by_name.return_value = _project("Tetris", "/srv/tetris")
        (request,) = _ask_for_task(_task_json(project="TETRIS", work_dir="")).requests
        assert (request.payload["project_id"], request.payload["work_dir"]) == ("proj-111111111111", "/srv/tetris")

    def test_unknown_project_is_created_with_the_task_directory(self, agent_db: Any) -> None:
        result = _ask_for_task(_task_json(project=" Tetris ", work_dir="~/projects/tetris"))
        agent_db.create_project.assert_awaited_once_with(name="Tetris", work_dir=TETRIS_DIR)
        (request,) = result.requests
        assert request.payload["project_id"] == "proj-000000000000"
        assert result.reply.startswith("ok") and "Tetris" in result.reply

    @pytest.mark.parametrize("fields", [{}, {"project": ""}, {"project": "  "}, {"project": "x" * 81}])
    def test_task_without_a_project_asks_and_creates_nothing(self, agent_db: Any, fields: dict[str, Any]) -> None:
        result = _ask_for_task(_task_json(work_dir="~/projects/tetris", **fields))
        assert result.requests == []
        assert "项目" in result.reply
        agent_db.create_project.assert_not_awaited()

    @pytest.mark.parametrize("fields", [{}, {"work_dir": ""}, {"work_dir": "relative/dir"}])
    @pytest.mark.parametrize("known", [False, True])
    def test_task_without_a_directory_is_not_created(self, agent_db: Any, fields: dict[str, Any], known: bool) -> None:
        if known:  # an existing project without a default directory
            agent_db.find_project_by_name.return_value = _project("tetris")
        result = _ask_for_task(_task_json(project="tetris", **fields))
        assert result.requests == []
        assert "目录" in result.reply
        agent_db.create_project.assert_not_awaited()

    def test_database_failure_is_answered(self, agent_db: Any) -> None:
        agent_db.find_project_by_name.side_effect = ConnectionError("database is down")
        result = _ask_for_task(_task_json(project="tetris", work_dir="/tmp/t"))
        assert result.requests == []
        assert "database is down" in result.reply
