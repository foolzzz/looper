"""Minimal Worker preparation fixtures shared by runtime and injection tests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers import worker
from tests.runtime_model_helpers import builtin_launch


def _item(root, *, review=False):
    return MessageEnvelope.create(
        source=planner_address("task-subagents"), target=worker_address("worker-subagents"),
        message_type=MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM,
        payload={"task_id": "step-subagents", "parent_task_id": "task-subagents", "step_key": "inspect",
                 "instruction": "Inspect source", "context": {"work_dir": str(root)}},
    )


def _configured_pool(root, monkeypatch, project):
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=builtin_launch().settings))
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0, 0, "WORKER")))
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(worker.WorkerPool, "_task_project", AsyncMock(return_value=project))
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=root / "runtime")
    room = SimpleNamespace(presence_changed=AsyncMock(), first_turn=AsyncMock(return_value="room intro"),
                           delivery=AsyncMock(return_value=None), confirm=AsyncMock())

    async def same_library(library, *args, **kwargs):
        return library

    pool.room_ops = SimpleNamespace(
        room=room, issue=Mock(return_value="run.synthetic"),
        library_with_server=AsyncMock(side_effect=same_library), bind_subagents=Mock(),
        end_subagents=AsyncMock(), revoke_run=Mock(), export_run=Mock(return_value=None),
    )
    return pool
