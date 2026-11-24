"""Routed completions use code notices before any operator-scoped model input is read."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from looper.agents import completion_events
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers import background_turn
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.manager import Ask, _CompletionNotTaken
from looper.agents.ops import Turn, TurnOrigin


@pytest.mark.parametrize("kind", [SessionKind.GROUP, SessionKind.DM])
def test_non_main_completion_falls_back_before_building_model_input(monkeypatch, tmp_path, kind):
    async def check():
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = object()
        session = manager._router.session(SessionKey(kind, "channel-lark", "oc_synthetic_background"))
        manager._current.set(session)
        rows = [{"id": 1, "task_id": "synthetic-task", "payload": {}, "fallback": [],
                 "target_kind": "user", "chat": "oc_synthetic_background", "channel_id": "channel-lark"}]
        batch = uuid.uuid4()
        fallback = AsyncMock()
        build = AsyncMock(side_effect=AssertionError("non-main must not read task facts"))
        run = AsyncMock(side_effect=AssertionError("non-main must not start a model completion"))
        monkeypatch.setattr(manager, "_fall_back_completion", fallback)
        monkeypatch.setattr(manager, "_completion_turn", run)
        monkeypatch.setattr(completion_events, "task_facts", build)
        monkeypatch.setattr(completion_events, "completion_message", build)
        await manager._tell_completion(rows, batch)
        fallback.assert_awaited_once_with(batch, "session_allowlist", {"rows": rows})
        run.assert_not_called()
        build.assert_not_called()

    asyncio.run(check())


def test_main_completion_still_runs_the_model_path_and_its_existing_fallback(monkeypatch, tmp_path):
    async def check():
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = object()
        row = {"id": 1, "task_id": "synthetic-task", "payload": {}, "fallback": [],
               "target_kind": "web", "chat": "synthetic-web", "channel_id": "channel-web"}
        rows, batch = [row], uuid.uuid4()
        fallback, model = AsyncMock(), AsyncMock(return_value="")

        async def run(*args, work, **kwargs):
            task = asyncio.create_task(work)
            await task
            return background_turn.Result(timed_out=False, task=task)

        monkeypatch.setattr(manager, "_fall_back_completion", fallback)
        monkeypatch.setattr(manager, "_completion_turn", model)
        monkeypatch.setattr(background_turn, "run", run)
        await manager._tell_completion(rows, batch)
        model.assert_awaited_once()
        assert model.call_args.args[:2] == (rows, batch)
        fallback.assert_awaited_once_with(batch, "empty_reply", {"rows": rows}, rejected=False)

    asyncio.run(check())


def test_unlisted_background_input_cannot_bypass_the_completion_entry_point(tmp_path):
    async def check():
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        session = manager._router.session(SessionKey(SessionKind.DM, "channel-lark", "oc_synthetic_background"))
        manager._current.set(session)
        with pytest.raises(_CompletionNotTaken):
            await manager._prepare_turn(None, None, ask=Ask(
                "PRIVATE_BACKGROUND_INPUT", turn=Turn(None, None, origin=TurnOrigin.COMPLETION)))

    asyncio.run(check())
