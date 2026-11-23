"""Capture the observable off-path contract against a synthetic local model."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from looper.agents import chat_settings, completion_events, lanes_settings, session_routing, turn_metrics
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.db import projects, tasks
from tests.api_manager_support import USAGE, _manager
from tests.db_support import until
from tests.fake_model_api import ResponseScript, text_events


def _normalize(value: Any, root: Path, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item, root, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, root, replacements) for item in value]
    if not isinstance(value, str):
        return value
    value = value.replace(str(root), "<runtime-root>")
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(?:Z|[+-][0-9:]+)", "<timestamp>", value)
    return value


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def capture(pool, root, monkeypatch, *, routing: bool, legacy_router=None):
    """Compare actual resident inputs, complete record columns, replies and acknowledgement order."""
    monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
    monkeypatch.setattr(session_routing, "routes", lambda _: routing)
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
    gate = asyncio.Event()
    first = ResponseScript(events=text_events("completion answer", usage=USAGE), gate=gate)
    second = ResponseScript(events=text_events("user answer", usage=USAGE))
    async with _manager(pool, root, monkeypatch, [first, second]) as chat:
        if legacy_router is not None:
            chat.manager._router = legacy_router(chat.manager)
        project = await projects.create_project(name="compat-project", work_dir=str(root), pool=pool)
        await tasks.create_task(task_id="compat-task", requirement="compat task", project_id=project["id"],
                                work_dir=str(root), status="completed", title="compat task", pool=pool,
                                origin_thread="compat-chat")
        await chat.manager._handle(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
            payload={"task_id": "compat-task", "goal_id": "compat-goal", "status": "completed", "summary": "Done"},
            metadata={"task_title": "compat task", "origin_thread": "compat-chat", "origin_channel_id": "channel-web"}))
        due = completion_events.wake(MANAGER_ADDRESS, completion_events.Target("manager", "channel-web", "compat-chat"))
        user = MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "hello", "channel_context": {"session_id": "compat-chat"}},
            metadata={"origin_channel_id": "channel-web"})
        replacements = {due.message_id: "<completion-message>", user.message_id: "<user-message>",
                        project["id"]: "<project-id>", chat.model.base_url: "<model-api>"}
        settled = []
        original = chat.bus.settle_handled

        def settle(envelope):
            settled.append(envelope.message_id)
            original(envelope)

        monkeypatch.setattr(chat.bus, "settle_handled", settle)
        await chat.bus.start()
        await chat.manager.start(chat.bus)
        await chat.bus.publish(due)
        await asyncio.wait_for(first.gate_reached.wait(), 15)
        await chat.bus.publish(user)
        if routing:
            await until(lambda: chat.manager._router.queued_count(chat.manager._sessions["main"]) == 1)
        gate.set()
        await until(lambda: user.message_id in settled, "legacy completion then user")
        await until(lambda: not turn_metrics._pending, "all record annotations")
        event = await pool.fetchrow("SELECT batch FROM manager_events")
        replacements[str(event["batch"])] = "<completion-batch>"
        saved = await pool.fetchval("SELECT session_id FROM agent_sessions WHERE agent_id = 'manager'")
        if saved:
            replacements[saved] = "<resident-session>"
        records = [dict(row) for row in await pool.fetch("SELECT * FROM llm_records ORDER BY id")]
        for record in records:
            record["metadata"] = json.loads(record["metadata"])
            # Built-in MCP bundles include the ephemeral loopback URL and credential. Their hashes vary even
            # when all tool schemas and launch behavior are identical; retain the bundle shape and names.
            library = record["metadata"].get("library", {})
            if any(item.get("name") == "looper" and item.get("scope") == "builtin"
                   for item in library.get("items", [])):
                library["sha256"] = "<mcp-bundle>"
                for item in library["items"]:
                    if item.get("name") == "looper" and item.get("scope") == "builtin":
                        item["sha256"] = "<mcp-entry>"
            # Wall-clock execution duration and timestamps are not part of the behavioral contract.
            for key in ("duration_ms", "created_at", "finished_at", "last_output_at"):
                record[key] = "<clock>" if record[key] is not None else None
        queue = chat.bus.get_queue("channel-web")
        replies = [item.payload for item in (queue.get_nowait() for _ in range(queue.qsize()))
                   if item.message_type is MessageType.AGENT_REPLY]
        raw = _normalize({"inputs": [request.body["input"] for request in chat.model.requests],
                          "records": records, "replies": replies, "settles": settled}, root, replacements)
        return {"inputs": [_sha(item) for item in raw["inputs"]],
                "records": [{key: _sha(value) for key, value in row.items()} for row in raw["records"]],
                "replies": raw["replies"], "settles": raw["settles"]}, raw
