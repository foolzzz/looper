"""The Manager must always answer a chat message, even when Claude fails."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_claude import fake_claude
from library_fixtures import ALPHA_SKILL, DEMO_PLUGIN, plugin_dirs
from loopx.agents.agent_settings import DEFAULT_HARNESS_PROMPT
from loopx.agents.bus import MessageBus
from loopx.agents.claude_cli import ISOLATION_ID, isolation_args
from loopx.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from loopx.agents.tiers.manager import TASK_PLACEMENT_RULE, ManagerAgent, ManagerConfig

WEB = channel_address("web")


def _fake_claude(tmp: Path, body: str) -> str:
    """A stand-in `claude` binary that saves its argv and the system prompt its file holds."""
    return fake_claude(tmp / "fake-claude", (
        "import json, os, sys, time\n"
        "open('argv.json', 'w').write(json.dumps(sys.argv[1:]))\n"
        "if '--append-system-prompt-file' in sys.argv:\n"
        "    prompt = open(sys.argv[sys.argv.index('--append-system-prompt-file') + 1], encoding='utf-8').read()\n"
        "    open('prompts.jsonl', 'a').write(json.dumps(prompt) + '\\n')\n"
        f"{body}\n"
    ))


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
                "prompts": _prompts(root),
                "pid": int(pid_file.read_text()) if pid_file.exists() else None,
            }

    return asyncio.run(_run())


def _prompts(root: Path) -> list[str]:
    """The system prompt each started fake read from its file."""
    path = root / "prompts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.usefixtures("agent_db")
class TestManagerReply:
    def test_reply_is_recorded(self, agent_db: Any) -> None:
        # Claude's init line lists every tool and skill; it can exceed 64 KiB.
        asked = _ask(
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-x', 'session_id': 's-9',\n"
            "                  'tools': ['t' * 200_000]}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi'}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'hi'}))"
        )
        start = agent_db.start_record.await_args.kwargs
        assert start["agent_id"] == "manager" and start["input_text"] == "hello"
        command = start["metadata"]["command"]
        # The system prompt is a file named by its hash, not an argument.
        assert "--append-system-prompt" not in command
        (system_prompt,) = asked["prompts"]
        assert Path(command[command.index("--append-system-prompt-file") + 1]).name == f"{_sha(system_prompt)}.md"
        # The Manager has three layers (no project, no task): its harness, its role, then the placement rule.
        assert start["metadata"]["system_prompt"] == system_prompt
        assert system_prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nYou are the Manager Agent")
        assert system_prompt.endswith(TASK_PLACEMENT_RULE)
        assert start["metadata"]["system_prompt_sha256"] == _sha(system_prompt)
        layers = start["metadata"]["prompt_layers"]
        assert layers["harness"] == {"source": "builtin_default", "sha256": _sha(DEFAULT_HARNESS_PROMPT)}
        assert layers["role"]["config_id"] == "manager" and layers["role"]["recommended"] is True
        assert (layers["project"], layers["task"]) == (None, None)
        assert layers["contract"] == {"name": "TASK_PLACEMENT_RULE", "sha256": _sha(TASK_PLACEMENT_RULE)}
        assert layers["isolation"] == ISOLATION_ID
        finish = agent_db.finish_record.await_args.kwargs
        assert (finish["status"], finish["output_text"], finish["model"], finish["session_id"]) == (
            "completed", "hi", "claude-x", "s-9")
        assert asked["text"] == "hi"

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
    @pytest.mark.parametrize(("body", "deltas", "text"), [
        # Each partial chunk is pushed once; the full message is not pushed again.
        (_delta("杭州") + _delta("是浙江") + _delta("的省会。") + _message("杭州是浙江的省会。"),
         ["杭州", "是浙江", "的省会。"], "杭州是浙江的省会。"),
        # Without partial chunks, whole messages still stream.
        (_message("first part. ") + _message("second part."), ["first part. ", "second part."],
         "first part. second part."),
        # A subagent's output stays internal.
        (_delta("internal note", parent="toolu_1") + _message("internal note", parent="toolu_1")
         + _delta("答案") + _message("答案"), ["答案"], "答案"),
    ], ids=["partials", "whole-messages", "subagent"])
    def test_reply_streams_token_by_token(self, body: str, deltas: list[str], text: str) -> None:
        reply = _ask(body + _RESULT)
        assert "--include-partial-messages" in reply["argv"]
        assert (reply["deltas"], reply["text"]) == (deltas, text)


def _resident(*, exit_after: int | None = None, die_on: int | None = None, ignore_eof: bool = False) -> str:
    """A fake claude that reads user turns from stdin like the real one.

    It appends its pid to ``pids`` on start and answers each turn with
    ``turn N: <message>`` (the user's message, after the context sections)
    and a result. Like Claude, the result's ``total_cost_usd`` is the
    session's running total (0.25 per turn): a process started with
    ``--resume`` carries on from the total the session had (kept in
    ``session_cost``). It exits on EOF, after turn ``exit_after``, or
    without a result during turn ``die_on``. With ``ignore_eof`` it keeps
    running after stdin closes.
    """
    return (
        "open('pids', 'a').write(f'{os.getpid()}\\n')\n"
        "turn = 0\n"
        "cost = float(open('session_cost').read()) if '--resume' in sys.argv and os.path.exists('session_cost') else 0.0\n"
        "for line in sys.stdin:\n"
        "    turn += 1\n"
        "    text = json.loads(line)['message']['content'].rpartition('\\n\\n')[2]\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)\n"
        f"    if turn == {die_on!r}:\n"
        "        sys.exit(3)\n"
        "    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': f'turn {turn}: {text}'}]}}), flush=True)\n"
        "    cost += 0.25\n"
        "    open('session_cost', 'w').write(str(cost))\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'sess-1',\n"
        "                      'total_cost_usd': cost}), flush=True)\n"
        f"    if turn == {exit_after!r}:\n"
        "        break\n"
        + ("time.sleep(30)\n" if ignore_eof else "")
    )


def _forgetting() -> str:
    """Like Claude given a ``--resume`` id it no longer has: an error result, no init; else ``_resident()``."""
    return (
        "if '--resume' in sys.argv:\n"
        "    sys.stdin.readline()\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'num_turns': 0, 'total_cost_usd': 0, 'errors': ['No conversation found with session ID: gone']}), flush=True)\n"
        "    sys.exit(0)\n"
    ) + _resident()


def _converse(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    messages: list[str],
    *,
    prompts: list[str] | None = None,
    rows: list[dict[str, Any]] | None = None,
    settings: list[dict[str, str]] | None = None,
    between: Any = None,
    saved_session: str | None = None,
    saved_totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send ``messages`` one at a time to one Manager backed by a fake claude, then stop it.

    ``prompts[i]`` is the system prompt the database holds for message ``i``
    (``rows[i]`` the whole manager row, instead) and ``settings[i]`` what the
    Settings table holds; ``between(spawned)`` runs before every message after
    the first. Returns the final ``replies``, every started Claude as
    ``(argv, process)`` in ``spawned`` with its launch keywords in
    ``launched``, whether each was ``running`` just before ``stop()``, the
    ``pids`` the fake logged and the work directory ``root``. ``saved_session`` (with ``saved_totals``, the
    running totals saved beside it) is what an earlier run of the server
    left; the fake's session then has spent ``saved_totals``' cost. The saved session is kept in
    memory in place of ``agent_sessions``; the returned ``saved_session`` and ``saved_totals`` are what
    the Manager saved last.
    """
    from loopx.db import agent_configs, agent_sessions, system_settings

    sessions: dict[str, dict[str, Any]] = {}
    if saved_session is not None:  # a session saved by an earlier run of the server
        sessions["manager"] = {"session_id": saved_session, "session_totals": dict(saved_totals or {})}

    async def _get_session(agent_id: str, **_: Any) -> dict[str, Any] | None:
        return sessions.get(agent_id)

    async def _save_session(agent_id: str, session_id: str | None, totals: Any, **_: Any) -> None:
        sessions[agent_id] = {"session_id": session_id, "session_totals": dict(totals)}

    monkeypatch.setattr(agent_sessions, "get_session", _get_session)
    monkeypatch.setattr(agent_sessions, "save_session", _save_session)

    spawned: list[tuple[list[str], Any]] = []
    launched: list[dict[str, Any]] = []
    real_exec = asyncio.create_subprocess_exec

    async def _exec(*args: Any, **kwargs: Any) -> Any:
        process = await real_exec(*args, **kwargs)
        spawned.append(([str(a) for a in args[1:]], process))
        launched.append(kwargs)
        return process

    current: dict[str, int] = {"message": 0}

    async def _get_config(agent_id: str) -> dict[str, Any] | None:
        if rows:
            return dict(rows[current["message"]])
        if not prompts:
            return None
        return {"system_prompt": prompts[current["message"]]}

    async def _get_settings(keys: Any) -> dict[str, str]:
        return dict(settings[current["message"]]) if settings else {}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(agent_configs, "get_config", _get_config)
    monkeypatch.setattr(system_settings, "get_settings", _get_settings)

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if saved_totals is not None:  # the fake's session has spent them
                (root / "session_cost").write_text(str(saved_totals.get("cost_usd", 0.0)))
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
            saved = sessions.get("manager") or {}
            return {"replies": replies, "spawned": spawned, "launched": launched, "running": running, "pids": pids,
                    "prompts": _prompts(root), "root": root, "files": sorted(p.name for p in root.rglob("*.json")),
                    "saved_session": saved.get("session_id"), "saved_totals": saved.get("session_totals")}

    return asyncio.run(_run())


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _assert_unrestricted(argv: list[str]) -> None:
    """The shared permission flags, once, and nothing that narrows the tools or the permissions again."""
    from loopx.agents.claude_cli import permission_args

    flags = permission_args()
    assert any(argv[i:i + len(flags)] == flags for i in range(len(argv)))
    assert argv.count("--dangerously-skip-permissions") == 1
    assert not {"--tools", "--allowedTools", "--disallowedTools", "--permission-mode"} & set(argv)


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

    def test_messages_go_on_stdin_not_in_argv(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        # The Manager's launch; what the shared flags hold is tested in test_claude_cli.
        chat = _converse(monkeypatch, _resident(), ["hello"])
        ((argv, _),) = chat["spawned"]
        assert _flag(argv, "--input-format") == "stream-json"
        assert _flag(argv, "--output-format") == "stream-json"
        # Claude echoes each message, so the Manager can tell its turns from the ones Claude runs on its own.
        assert {"--print", "--verbose", "--include-partial-messages", "--replay-user-messages"} <= set(argv)
        takes_value = {"--input-format", "--output-format", "--append-system-prompt-file", "--system-prompt-snapshot",
                       "--setting-sources", "--settings", "--resume", "--model", "--effort", "--allowedTools",
                       "--plugin-dir"}
        positional = [arg for i, arg in enumerate(argv) if not arg.startswith("--") and argv[i - 1] not in takes_value]
        assert positional == []
        assert _flag(argv, "--resume") is None  # no session yet
        # Isolated from the operator's configuration; the prompt file is read again on every start.
        isolation = isolation_args(chat["root"])
        at = argv.index(isolation[0])
        assert argv[at:at + len(isolation)] == isolation
        assert _flag(argv, "--system-prompt-snapshot") == "off"
        # Its tools and skills run without asking; without a library nothing is mounted or recorded.
        _assert_unrestricted(argv)
        assert "--plugin-dir" not in argv
        assert "library" not in agent_db.start_record.await_args.kwargs["metadata"]

    def test_claude_that_exited_is_restarted_with_its_session(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        chat = _converse(monkeypatch, _resident(exit_after=1), ["hello", "again"], between=_first_claude_exits)
        assert chat["replies"] == ["turn 1: hello", "turn 1: again"]
        (first, _), (second, _) = chat["spawned"]
        assert _flag(first, "--resume") is None
        assert _flag(second, "--resume") == "sess-1"
        assert len(chat["pids"]) == 2
        # The resumed process reports the session's total (0.5); the turn spent 0.25 of it.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [0.25, 0.25]

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
        # The restarted process resumes the session and reports its running total (0.5, then 0.75);
        # each record holds only its own turn's share.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [0.25, 0.25, 0.25]
        (first, _), (second, _) = chat["spawned"]
        first_prompt, second_prompt = chat["prompts"]
        assert first_prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nprompt A")
        assert second_prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nprompt B")
        # A new prompt is a new file named by its hash; each turn records the hash it ran with.
        assert Path(_flag(first, "--append-system-prompt-file") or "").name == f"{_sha(first_prompt)}.md"
        assert Path(_flag(second, "--append-system-prompt-file") or "").name == f"{_sha(second_prompt)}.md"
        starts = [call.kwargs["metadata"] for call in agent_db.start_record.await_args_list]
        assert [m["system_prompt_sha256"] for m in starts] == [_sha(first_prompt), _sha(second_prompt),
                                                              _sha(second_prompt)]
        assert [m["prompt_layers"]["role"]["recommended"] for m in starts] == [False, False, False]
        assert _flag(second, "--resume") == "sess-1"
        assert chat["running"] == [False, True]  # the old process was stopped and reaped

    def test_a_session_that_no_longer_exists_is_dropped_and_the_message_still_answered(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        chat = _converse(monkeypatch, _forgetting(), ["hello", "again"], saved_session="gone",
                         saved_totals={"cost_usd": 0.1})
        assert chat["replies"] == ["turn 1: hello", "turn 2: again"]
        (first, _), (second, _) = chat["spawned"]
        assert _flag(first, "--resume") == "gone"
        assert _flag(second, "--resume") is None  # a new session, which the next message keeps
        errors = [call.kwargs["error"] for call in agent_db.finish_record.await_args_list]
        assert "session gone could not be resumed" in (errors[0] or "")
        assert errors[1:] == [None, None]
        # The dropped session takes its totals with it: its 0.1 is not taken off the new session's turns.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [None, 0.25, 0.25]
        assert (chat["saved_session"], chat["saved_totals"]) == ("sess-1", {"cost_usd": 0.5})

    def test_a_new_session_that_also_says_nothing_is_not_retried_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat = _converse(monkeypatch, "sys.exit(1)", ["hello"], saved_session="gone")
        (reply,) = chat["replies"]
        assert "Manager" in reply  # the usual error reply, not a loop
        assert [_flag(argv, "--resume") for argv, _ in chat["spawned"]] == ["gone", None]

    def test_the_first_turn_after_a_server_restart_records_only_its_own_cost(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        # An earlier run of the server left the session at $0.50, saved beside its id.
        chat = _converse(monkeypatch, _resident(), ["hello", "again"], saved_session="sess-1",
                         saved_totals={"cost_usd": 0.5})
        ((argv, _),) = chat["spawned"]
        assert _flag(argv, "--resume") == "sess-1"
        # The resumed Claude reports 0.75 and then 1.0: the session's running total.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [0.25, 0.25]
        # The total is saved again for the next restart, in the database: no session file is written.
        assert (chat["saved_session"], chat["saved_totals"]) == ("sess-1", {"cost_usd": 1.0})
        assert "session.json" not in chat["files"]

    @pytest.mark.parametrize(("ignore_eof", "returncode"), [(False, 0), (True, -9)])
    def test_stop_ends_and_reaps_claude(
        self, monkeypatch: pytest.MonkeyPatch, ignore_eof: bool, returncode: int,
    ) -> None:
        from loopx.agents import claude_cli

        if ignore_eof:  # it is killed only once the grace period is over: keep that short
            monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.05)
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
            return SimpleNamespace(reply=reply, requests=requests, argv=argv, turn=turn, prompts=_prompts(root))

    return asyncio.run(_run())


def _project(name: str, work_dir: str = "", description: str = "", project_id: str = "proj-111111111111") -> dict[str, Any]:
    return {"id": project_id, "name": name, "description": description, "work_dir": work_dir}


TETRIS_DIR = os.path.join(os.path.expanduser("~"), "projects", "tetris")


@pytest.mark.usefixtures("agent_db")
class TestManagerTaskPlacement:
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

    # A chosen directory wins; without one the task takes the project's default directory.
    @pytest.mark.parametrize(("work_dir", "expected"), [("~/projects/tetris", TETRIS_DIR), ("", "/srv/tetris")])
    def test_existing_project_is_matched_by_name(self, agent_db: Any, work_dir: str, expected: str) -> None:
        agent_db.find_project_by_name.return_value = _project("Tetris", "/srv/tetris")
        result = _ask_for_task(_task_json(project="tetris", work_dir=work_dir))
        agent_db.find_project_by_name.assert_awaited_once_with("tetris")
        agent_db.create_project.assert_not_awaited()
        (request,) = result.requests
        assert (request.payload["project_id"], request.payload["work_dir"]) == ("proj-111111111111", expected)
        assert result.reply == "ok"

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


_HARNESS, _ENV = "agent.harness_prompt", "agent.env"


@pytest.mark.usefixtures("agent_db")
class TestManagerHarnessAndLaunch:
    """The Manager's harness is its own override, else the Settings default; its
    model is its own row's, else the Settings default (opus). Changing the
    harness, the model or the Settings environment restarts its Claude, which
    resumes the session."""

    def test_three_layers_with_the_managers_own_harness(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        row = {"system_prompt": "You manage.", "harness_prompt": "MANAGER HARNESS"}
        chat = _converse(monkeypatch, _resident(), ["hello"], rows=[row], settings=[{_HARNESS: "DEFAULT"}])
        (prompt,) = chat["prompts"]
        assert prompt == f"MANAGER HARNESS\n\nYou manage.\n\n{TASK_PLACEMENT_RULE}"
        layers = agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]
        assert layers["harness"] == {"source": "manager_override", "sha256": _sha("MANAGER HARNESS")}

    @pytest.mark.parametrize("change", ["harness", "override", "model"])
    def test_a_changed_harness_or_model_restarts_claude_resuming_its_session(
        self, monkeypatch: pytest.MonkeyPatch, change: str,
    ) -> None:
        before = {"system_prompt": "M.", "harness_prompt": "", "model": None}
        after = {**before, **({"harness_prompt": "OWN"} if change == "override" else {}),
                 **({"model": "sonnet"} if change == "model" else {})}
        settings = [{_HARNESS: "V1"}, {_HARNESS: "V2" if change == "harness" else "V1"}]
        chat = _converse(monkeypatch, _resident(), ["hello", "again"], rows=[before, after], settings=settings)
        assert chat["replies"] == ["turn 1: hello", "turn 1: again"]  # a new process for the second message
        (first, _), (second, _) = chat["spawned"]
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-1")
        assert chat["running"] == [False, True]
        if change == "model":  # the built-in default, then the Manager's own row
            assert (_flag(first, "--model"), _flag(second, "--model")) == ("opus", "sonnet")

    def test_a_changed_environment_restarts_claude_with_it(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        monkeypatch.setenv("LOOPX_SERVER_SIDE", "kept")
        env = [{_ENV: json.dumps([{"key": "LOOPX_TEST_FLAVOUR", "value": flavour}])} for flavour in ("mint", "mint",
                                                                                                    "lime")]
        chat = _converse(monkeypatch, _resident(), ["hello", "again", "third"], settings=env)
        assert chat["replies"] == ["turn 1: hello", "turn 2: again", "turn 1: third"]
        assert [(k["env"]["LOOPX_TEST_FLAVOUR"], k["env"]["LOOPX_SERVER_SIDE"]) for k in chat["launched"]] == [
            ("mint", "kept"), ("lime", "kept")]
        (first, _), (second, _) = chat["spawned"]
        assert first == [arg for arg in second if arg not in ("--resume", "sess-1")]  # only the env changed
        recorded = [call.kwargs["metadata"]["env"]["keys"] for call in agent_db.start_record.await_args_list]
        assert recorded == [{"LOOPX_TEST_FLAVOUR": _sha(f)} for f in ("mint", "mint", "lime")]


@pytest.mark.usefixtures("agent_db")
class TestManagerLibrary:
    """The Manager loads the plugins and the skills plugin installed for it
    globally; a change to that set restarts its Claude, which resumes the
    session."""

    def test_the_command_carries_the_enabled_plugins_and_the_skills_plugin(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, enabled_library: Any,
    ) -> None:
        demo, alpha = enabled_library(DEMO_PLUGIN, ALPHA_SKILL)
        chat = _converse(monkeypatch, _resident(), ["hello"])
        ((argv, _),) = chat["spawned"]
        plugin, skills = plugin_dirs(argv)
        assert plugin.endswith(f"/.local/library/{demo['sha256']}/demo") and skills.endswith("/personal")
        # Skills need no approval of their own: the Manager runs without permission prompts.
        _assert_unrestricted(argv)
        library = agent_db.start_record.await_args.kwargs["metadata"]["library"]
        assert library["items"] == [
            {"kind": "plugin", "name": "demo@market", "sha256": demo["sha256"], "plugin": "demo", "scope": "global"},
            {"kind": "skill", "name": "alpha", "sha256": alpha["sha256"], "plugin": "personal", "scope": "global"}]
        assert (library["role"], library["project_id"], len(library["sha256"])) == ("manager", None, 64)
        assert agent_db.list_installed.await_args.args == ("manager", None)

    def test_a_changed_library_restarts_claude_resuming_its_session(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, enabled_library: Any,
    ) -> None:
        enabled_library(DEMO_PLUGIN, ALPHA_SKILL)

        async def _disable_alpha(spawned: Any) -> None:
            enabled_library(DEMO_PLUGIN)

        chat = _converse(monkeypatch, _resident(), ["hello", "again", "third"], between=_disable_alpha)
        assert chat["replies"] == ["turn 1: hello", "turn 1: again", "turn 2: third"]
        (first, _), (second, _) = chat["spawned"]
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-1")
        assert len(plugin_dirs(first)) == 2 and len(plugin_dirs(second)) == 1
        recorded = [call.kwargs["metadata"]["library"] for call in agent_db.start_record.await_args_list]
        assert [[i["name"] for i in r["items"]] for r in recorded] == [
            ["demo@market", "alpha"], ["demo@market"], ["demo@market"]]
        assert recorded[0]["sha256"] != recorded[1]["sha256"] == recorded[2]["sha256"]


def _session_with(agent_db: Any, body: str, messages: list[str]) -> dict[str, Any]:
    """Send ``messages`` from one web chat session to a Manager backed by a fake claude.

    Returns the ``sent`` envelopes, every reply envelope in the order the
    channel got them, and each recorded run (its start and finish keywords,
    paired by record id).
    """
    ids = iter(range(1, 100))
    agent_db.start_record.side_effect = lambda **_: next(ids)

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(
                config=ManagerConfig(claude_bin=_fake_claude(root, body), work_dir=root), runtime_root=root,
            )
            manager._bus = bus
            sent = []
            for index, text in enumerate(messages):
                envelope = MessageEnvelope.create(
                    source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": text, "channel_context": {"session_id": "chat-1"}},
                    correlation_id=f"corr-{index}", metadata={"origin_channel_id": WEB.agent_id},
                )
                sent.append(envelope)
                await manager._handle(envelope)
            await manager.stop()
            queue = bus.get_queue(WEB.agent_id)
            replies = []
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    replies.append(envelope)
            await bus.shutdown()
            return {"sent": sent, "replies": replies, "state": agent_db.store.sessions["manager"]}

    chat = asyncio.run(_run())
    starts = [(call.kwargs, record_id) for call, record_id in
              zip(agent_db.start_record.await_args_list, range(1, 100))]
    finishes = {call.args[0]: call.kwargs for call in agent_db.finish_record.await_args_list}
    chat["runs"] = [{**start, **finishes[record_id]} for start, record_id in starts]
    return chat


@pytest.mark.usefixtures("agent_db")
class TestManagerAutonomousTurns:
    """Claude Code runs a turn of its own once background work ends (recorded, tests/claude_stream_replay.py)."""

    def test_the_next_message_gets_its_own_answer_and_the_autonomous_turn_its_own_reply_and_run(
        self, agent_db: Any,
    ) -> None:
        from claude_stream_replay import replay_body

        chat = _session_with(agent_db, replay_body("background_agent"), ["start a background agent", "7*6?"])
        first, second = chat["sent"]
        answers = {m.message_id: [r.payload["text"] for r in chat["replies"] if r.reply_to == m.message_id]
                   for m in chat["sent"]}
        assert answers == {first.message_id: ["LAUNCHED"], second.message_id: ["42"]}
        # What Claude said on its own is a reply of its own, to the chat of the message it last answered.
        (own,) = [r for r in chat["replies"] if r.reply_to is None]
        assert own.payload["text"] == "Background task completed with result: **SUB-DONE-4417**"
        assert own.payload["autonomous"] is True and own.payload["chat_session_id"] == "chat-1"
        assert (own.target, own.correlation_id, own.metadata["origin_channel_id"]) == (WEB, "corr-0", WEB.agent_id)
        assert chat["replies"].index(own) < [r.reply_to for r in chat["replies"]].index(second.message_id)
        # Three runs, each with its own input, output and cost.
        from loopx.agents.claude_cli import AUTONOMOUS_INPUT

        runs = {run["input_text"]: run for run in chat["runs"]}
        assert set(runs) == {"start a background agent", "7*6?", AUTONOMOUS_INPUT}
        autonomous = runs[AUTONOMOUS_INPUT]
        assert autonomous["metadata"]["autonomous"] is True and autonomous["agent_id"] == "manager"
        assert autonomous["output_text"] == own.payload["text"] and autonomous["status"] == "completed"
        assert runs["7*6?"]["output_text"] == "42"
        assert runs["start a background agent"]["cost_usd"] == pytest.approx(0.0130063)
        assert autonomous["cost_usd"] == pytest.approx(0.03359615 - 0.0130063)
        assert runs["7*6?"]["cost_usd"] == pytest.approx(0.03617515 - 0.03359615)
        # The saved running total covers all three, so a restart resumes from the right baseline.
        assert chat["state"]["session_totals"]["cost_usd"] == pytest.approx(0.03617515)
        # Every run is told apart; the user turns are not marked.
        assert [run["metadata"].get("autonomous") for run in chat["runs"]].count(True) == 1


@pytest.mark.usefixtures("agent_db")
class TestManagerProjectSkills:
    """The Manager spans every project, so it mounts and indexes nothing of a project's (knowledge or
    skills), only what is installed globally for it."""

    def test_the_manager_gets_no_project_skills_while_projects_have_some(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, enabled_library: Any, project_skills: Any,
        project_documents: Any,
    ) -> None:
        enabled_library(DEMO_PLUGIN)
        project_skills(("proj-1", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."))
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        chat = _converse(monkeypatch, _resident(), ["hello"])
        ((argv, _),) = chat["spawned"]
        (only,) = plugin_dirs(argv)  # the global plugin, nothing more
        assert only.endswith("/demo") and "/project" not in " ".join(plugin_dirs(argv))
        assert {call.args for call in agent_db.list_installed.await_args_list} == {("manager", None)}
        recorded = agent_db.start_record.await_args.kwargs["metadata"]["library"]
        assert [(i["name"], i["scope"]) for i in recorded["items"]] == [("demo@market", "global")]
        # Nor any project's knowledge: no mounted directory, no index, the documents never read.
        assert "--add-dir" not in argv
        assert "Project knowledge" not in chat["prompts"][0]
        agent_db.list_documents.assert_not_awaited()
        assert agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["project"] is None
