"""Each Manager's per-turn task and gate lists, its recent conversations and its /gates commands cover only what
it owns (RFC D11), on a real PostgreSQL. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import gate_commands
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.test_managers_schema_db import _manager
from tests.test_ops_tasks_db import _project
from tests.test_usage_api_db import DSN, _run
from tests.test_user_gates_db import _gate, _gate_step

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

REVIEW = "manager-review"
WEB = channel_address("web", "channel-web")


def _agent(tmp: Path, definition: ManagerDefinition | None = None) -> ManagerAgent:
    return ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=tmp), runtime_root=tmp,
                        definition=definition)


async def _definition(pool: Any) -> ManagerDefinition:
    from looper.db import managers

    row = await managers.get_manager(REVIEW, pool=pool)
    assert row is not None
    return ManagerDefinition.from_row(row)


async def _task(pool: Any, task_id: str, manager_id: str, project: dict[str, Any], *, gate: bool = False,
                status: str | None = None) -> None:
    from looper.db import plans, tasks

    await tasks.create_task(task_id=task_id, requirement=f"requirement of {task_id}", title=f"title of {task_id}",
                            project_id=project["id"], work_dir="/w", status=status or tasks.READY, manager_id=manager_id, pool=pool)
    if gate:
        step_id = f"step-{task_id}"
        await plans.save(task_id, plan={"plan_id": f"plan-{task_id}", "status": "executing"},
                         steps=[{**_gate_step(step_id), "status": "waiting_user"}],
                         gates=[_gate(step_id, state="open", opened_at=datetime.now(timezone.utc))], pool=pool)


def _web(text: str = "hi", session: str = "s-1") -> MessageEnvelope:
    return MessageEnvelope.create(
        source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE, payload={
            "text": text, "channel_context": {"session_id": session}})


async def _handle(envelope: MessageEnvelope, text: str, manager_id: str | None) -> str | None:
    async def posted(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("posted")

    return await gate_commands.handle(envelope, text, has_planner=lambda task_id: True, bus=None,
                                      source=MANAGER_ADDRESS, manager_id=manager_id)


def test_ready_tasks_and_gates_are_the_managers_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _manager(pool, "review", task_scope="all")
        project = await _project(pool)
        await _task(pool, "t-default", "manager", project, gate=True, status="running")
        await _task(pool, "t-own", REVIEW, project, gate=True, status="running")
        await _task(pool, "r-default", "manager", project)
        await _task(pool, "r-own", REVIEW, project)
        default, review = _agent(tmp_path), _agent(tmp_path, await _definition(pool))
        text = "\n".join(await default._ready_tasks_section())
        assert "r-default" in text and "r-own" not in text
        text = "\n".join(await review._ready_tasks_section())
        assert "r-own" in text and "r-default" not in text
        text = "\n".join(await default._open_gates_section(_web()))
        assert "t-default" in text and "t-own" not in text
        text = "\n".join(await review._open_gates_section(_web()))
        assert "t-own" in text and "t-default" not in text
        # /gates ignores task_scope too
        out = await _handle(_web(), "/gates", REVIEW)
        assert out and "title of t-own" in out and "title of t-default" not in out
        out = await _handle(_web(), "/gates", "manager")
        assert out and "title of t-default" in out and "title of t-own" not in out
        out = await _handle(_web(), "/gates", None)
        assert out and "title of t-default" in out and "title of t-own" in out

    _run(test, monkeypatch)


def test_no_tasks_no_sections(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        agent = _agent(tmp_path)
        assert await agent._ready_tasks_section() == []
        assert await agent._open_gates_section(_web()) == []

    _run(test, monkeypatch)


def test_default_sections_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool, "calc")
        await _task(pool, "t-1", "manager", project)
        await _task(pool, "t-2", "manager", project, gate=True, status="running")
        agent = _agent(tmp_path)
        assert await agent._ready_tasks_section() == [
            "Ready tasks (created, not started; newest first):",
            "  - t-1 (project calc): requirement of t-1", ""]
        lines = await agent._open_gates_section(_web())
        assert lines[0] == "Open gates (waiting for the user's answer; newest first):"
        assert lines[1].startswith("  - task t-2 (project calc), gate g1 \"storage\": open for ")
        assert lines[1].endswith("waiting for the user; chat: the task page only; open questions: "
                                 + lines[1].rsplit("open questions: ", 1)[1]) and lines[2] == ""

    _run(test, monkeypatch)


def test_recent_conversations_and_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import chat, managers

        await _manager(pool, "review")
        for sid, owner in (("s-own", REVIEW), ("s-del", REVIEW), ("lark:oc_x", None), ("s-old", None)):
            await chat.save_message(session_id=sid, message_id=f"m-{sid}", role="user", content=sid, pool=pool)
            if owner:
                await chat.claim_thread(sid, owner, pool=pool)
        got = await chat.recent_conversations(sessions=10, messages=5, manager_id=REVIEW, pool=pool)
        assert {c["session_id"] for c in got} == {"s-own", "s-del"}
        got = await chat.recent_conversations(sessions=10, messages=5, pool=pool)
        assert {c["session_id"] for c in got} == {"lark:oc_x", "s-old"}
        assert await managers.delete_manager(REVIEW) == "deleted"
        got = await chat.recent_conversations(sessions=10, messages=5, manager_id="manager", pool=pool)
        assert "s-del" not in {c["session_id"] for c in got}

    _run(test, monkeypatch)


def test_gate_snapshot_keys_and_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _manager(pool, "review")
        project = await _project(pool)
        await _task(pool, "t-default", "manager", project, gate=True, status="running")
        await _task(pool, "t-own", REVIEW, project, gate=True, status="running")
        gate_commands._snapshots.clear()
        gate_commands._pending.clear()
        posts: list[Any] = []

        async def post_message(*args: Any, **kwargs: Any) -> Any:
            posts.append(args)
            return {"delivered": "queued"}

        monkeypatch.setattr(gate_commands, "post_message", post_message)
        envelope = _web()
        for manager_id in ("manager", REVIEW):
            await _handle(envelope, "/gates", manager_id)
            gate_commands.settle(envelope.message_id, True)
        assert await _handle(envelope, "/gate 1 ok", "manager") is not None
        assert await _handle(envelope, "/gate 1 ok", REVIEW) is not None
        assert [p[0] for p in posts] == ["t-default", "t-own"]
        # the scope is rechecked at submit time
        posts.clear()
        await _handle(envelope, "/gates", REVIEW)
        gate_commands.settle(envelope.message_id, True)
        await pool.execute("UPDATE tasks SET manager_id = 'manager' WHERE id = 't-own'")
        out = await _handle(envelope, "/gate 1 ok", REVIEW)
        assert not posts and out and "不在这个管家的范围里" in out
        # another Manager's chat has no snapshot
        gate_commands._snapshots.clear()
        out = await _handle(envelope, "/gate 1 ok", REVIEW)
        assert out and "请先用 /gates" in out

    _run(test, monkeypatch)
