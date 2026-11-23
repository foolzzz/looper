"""What a user turn waited behind in its own session (``turn_metrics.queued_behind``).

Metrics on, the user-turn record says ``none``, ``user`` or ``background``. Metrics off, the same
arrival writes no ``turn_metrics`` and leaves both end times unset. The metrics-off prompt hash
and command line are pinned to this scenario on main. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings, operator_identity, session_routing, turn_metrics
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db, until
from tests.lark_cli_stand_in import OPEN_ID
from tests.test_completion_turn_db import THREAD, _Hung, _due, _goal_status, _task
from tests.test_manager_mid_turn_input import run_case, start_turn
from tests.test_manager_session_routing_db import GROUP_A, LARK, _Resident, _Rig, _lark, _run, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_PREVIOUS = {"v", "origin", "chat", "received_at", "ack", "ack_ms", "first_reply_ms", "tool_calls",
             "delivery", "replayed"}

# origin/main (d375a17bc) for this scenario with metrics off. The prompt and the command are built by
# code this branch does not change. ``--settings`` embeds parent directories and the home directory, so
# that argument is ``<settings>``; the executable is ``<claude>``. The prompt file keeps its content hash.
# The completion turn and the user turn that follows it share this command.
_MAIN_SYSTEM_PROMPT_SHA256 = "91ead4013184f8884ce8380726099e8adc82e5584e54ddac2d73ac0cf44b5808"
_MAIN_COMMAND = [
    "<claude>",
    "--print",
    "--verbose",
    "--output-format",
    "stream-json",
    "--input-format",
    "stream-json",
    "--include-partial-messages",
    "--replay-user-messages",
    "--setting-sources",
    "project",
    "--strict-mcp-config",
    "--settings",
    "<settings>",
    "--system-prompt-file",
    "<root>/.local/prompts/91ead4013184f8884ce8380726099e8adc82e5584e54ddac2d73ac0cf44b5808.md",
    "--system-prompt-snapshot",
    "off",
    "--dangerously-skip-permissions",
    "--disallowedTools",
    "ListAgents",
    "--model",
    "opus",
    "--teammate-mode",
    "in-process",
]


def _command_line(command: list[str], root: Path) -> list[str]:
    """The command line with this run's paths removed, so it can be pinned."""
    root_text = str(root)
    line = [part.replace(str(root.resolve()), "<root>").replace(root_text, "<root>") for part in command]
    line[0] = "<claude>"
    line[line.index("--settings") + 1] = "<settings>"
    return [re.sub(r"/[0-9a-f]{64}\.(json)", r"/<sha>.\1", part) for part in line]


async def _rows(pool: Any) -> list[tuple[str, dict[str, Any]]]:
    fetched = await pool.fetch(
        "SELECT input_text, metadata::text AS metadata FROM llm_records WHERE agent_id = 'manager' ORDER BY id")
    return [(row["input_text"] or "", json.loads(row["metadata"] or "{}")) for row in fetched]


def _row(rows: list[tuple[str, dict[str, Any]]], text: str) -> dict[str, Any]:
    matches = [metadata for body, metadata in rows if text in body]
    assert len(matches) == 1
    return matches[0]


async def _wait_metric(pool: Any, text: str) -> dict[str, Any]:
    """The reply is published before the turn's metrics annotate is scheduled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while True:
        pending = list(turn_metrics._pending)
        if pending:
            await asyncio.gather(*pending)
        matches = [metadata for body, metadata in await _rows(pool)
                   if text in body and "turn_metrics" in metadata]
        if len(matches) == 1:
            return matches[0]
        assert loop.time() < deadline, f"timed out waiting for {text}"
        await asyncio.sleep(0.02)


async def _behind_a_completion(pool: Any, tmp_path: Path) -> tuple[dict[str, Any], str, ManagerAgent]:
    """A user message arrives while a completion turn holds the session, then runs after it."""
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    await _task(pool, tmp_path, origin_thread=THREAD)
    bus = MessageBus(runtime_root=tmp_path)
    hung = _Hung()
    manager = ManagerAgent(config=ManagerConfig(claude_bin=str(claude), work_dir=tmp_path), runtime_root=tmp_path)
    manager._bus, manager._claude = bus, hung
    running: asyncio.Task[Any] | None = None
    try:
        await manager._handle(_goal_status())
        (due,) = await _due(bus)
        running = asyncio.create_task(manager._handle(due))
        await until(lambda: bool(hung.texts))
        user = MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "queued-user", "channel_context": {"session_id": THREAD}}, correlation_id="c2",
            metadata={"origin_channel_id": "channel-web"})
        hung.release.set()
        await asyncio.wait_for(running, 20)
        await manager._handle(user)
        await asyncio.gather(*turn_metrics._pending)
        prompts = [text for text in hung.texts if text.endswith("queued-user")]
        assert len(prompts) == 1
        return _row(await _rows(pool), "queued-user"), prompts[0], manager
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await bus.shutdown()


def test_a_user_message_that_arrived_during_a_completion_turn_waited_behind_background(
        monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def run(pool: Any) -> None:
        metadata, _prompt, manager = await _behind_a_completion(pool, tmp_path)
        metrics = metadata["turn_metrics"]
        assert metrics["queued_behind"] == "background"
        assert set(metrics) == _PREVIOUS | {"queued_behind"}
        assert metrics["v"] == 1 and metrics["origin"] == "user" and metrics["delivery"] == "reply"
        assert metrics["replayed"] is False and metrics["chat"] == "dashboard"
        completion = _row(await _rows(pool), "[COMPLETION]")
        assert completion["turn_metrics"] == {"origin": "completion", "tool_calls": 0, "delivery": "reply"}
        session = manager._sessions["main"]
        assert session.last_background_turn_ended_at is not None
        assert session.last_user_turn_ended_at is not None
        assert session.last_background_turn_ended_at <= session.last_user_turn_ended_at

    run_db(run, monkeypatch, timeout=90)


def test_metrics_off_matches_the_same_arrival_with_metrics_on_except_the_new_key(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)

    async def run(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
        off_metadata, off_prompt, off_manager = await _behind_a_completion(pool, tmp_path)
        assert "turn_metrics" not in off_metadata
        session = off_manager._sessions["main"]
        assert session.last_user_turn_ended_at is None and session.last_background_turn_ended_at is None
        rows = await _rows(pool)
        user, completion = _row(rows, "queued-user"), _row(rows, "[COMPLETION]")
        assert hashlib.sha256(user["system_prompt"].encode()).hexdigest() == user["system_prompt_sha256"]
        assert hashlib.sha256(completion["system_prompt"].encode()).hexdigest() == completion["system_prompt_sha256"]
        observed = {
            "system_prompt_sha256": user["system_prompt_sha256"],
            "completion_system_prompt_sha256": completion["system_prompt_sha256"],
            "completion_command": _command_line(completion["command"], tmp_path),
            "user_command": _command_line(user["command"], tmp_path),
        }
        assert observed == {
            "system_prompt_sha256": _MAIN_SYSTEM_PROMPT_SHA256,
            "completion_system_prompt_sha256": _MAIN_SYSTEM_PROMPT_SHA256,
            "completion_command": _MAIN_COMMAND,
            "user_command": _MAIN_COMMAND,
        }
        await pool.execute(
            "TRUNCATE llm_records, manager_events, tasks, projects, agent_sessions, manager_sessions, "
            "chat_messages CASCADE")
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        on_metadata, on_prompt, _on_manager = await _behind_a_completion(pool, tmp_path)
        assert off_prompt == on_prompt
        assert {key: value for key, value in on_metadata.items() if key != "turn_metrics"} == off_metadata
        metrics = on_metadata["turn_metrics"]
        assert metrics["queued_behind"] == "background"
        assert set(metrics) - {"queued_behind"} == _PREVIOUS
        assert {key: metrics[key] for key in ("v", "origin", "delivery", "replayed")} == {
            "v": 1, "origin": "user", "delivery": "reply", "replayed": False}

    run_db(run, monkeypatch, timeout=120)


def test_a_second_user_message_waited_behind_the_user_and_a_lone_one_behind_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    monkeypatch.setattr(chat_settings, "_mid_turn_input_enabled", False, raising=False)

    async def test(rig: _Rig, pool: Any) -> None:
        lone = _web("lone-msg")
        await rig.bus.publish(lone)
        await rig.reply(lone)
        session = rig.manager._sessions["main"]
        await until(lambda: session.last_user_turn_ended_at is not None)
        rig.main.hold = True
        first = _web("first-msg")
        await rig.bus.publish(first)
        await asyncio.wait_for(rig.main.running.wait(), 20)
        second = _web("second-msg")
        await rig.bus.publish(second)
        rig.main.release.set()
        await rig.reply(second)
        assert (await _wait_metric(pool, "lone-msg"))["turn_metrics"]["queued_behind"] == "none"
        assert (await _wait_metric(pool, "first-msg"))["turn_metrics"]["queued_behind"] == "none"
        assert (await _wait_metric(pool, "second-msg"))["turn_metrics"]["queued_behind"] == "user"

    _run(test, monkeypatch, tmp_path)


def test_an_operator_direct_message_does_not_wait_behind_another_sessions_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(session_routing, "_max_resident", 2)

    async def operator(channel_id: str) -> str | None:
        return OPEN_ID if channel_id == LARK.agent_id else None

    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)

    async def test(rig: _Rig, pool: Any) -> None:
        group = rig.group(GROUP_A, _Resident("sess-group", hold=True))
        asked = _lark("group-msg", GROUP_A, OPEN_ID, group=True)
        await rig.bus.publish(asked)
        await asyncio.wait_for(group.running.wait(), 20)
        group_session = rig.session(GROUP_A)
        assert group_session.last_user_turn_ended_at is None and group_session.last_background_turn_ended_at is None
        direct = _lark("dm-msg", "oc_synthetic_operator", OPEN_ID, group=False)
        await rig.bus.publish(direct)
        await rig.reply(direct)
        assert (await _wait_metric(pool, "dm-msg"))["turn_metrics"]["queued_behind"] == "none"
        assert group_session.last_user_turn_ended_at is None and group_session.last_background_turn_ended_at is None
        group.release.set()
        await rig.reply(asked)

    _run(test, monkeypatch, tmp_path)


def test_a_merged_message_records_that_it_waited_behind_the_user_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    monkeypatch.setattr(chat_settings, "_mid_turn_input_enabled", True, raising=False)

    async def test(rig: _Rig, pool: Any) -> None:
        first = await start_turn(rig)
        await rig.bus.publish(_web("second-msg"))
        await until(lambda: bool(rig.main.inputs))
        rig.main.release.set()
        await rig.reply(first)
        metadata = await _wait_metric(pool, "first")
        assert metadata["turn_metrics"]["queued_behind"] == "none"
        assert metadata["merged"][0]["turn_metrics"]["queued_behind"] == "user"

    run_case(test, monkeypatch, tmp_path)
