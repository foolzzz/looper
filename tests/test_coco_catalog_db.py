"""Coco catalog validation belongs to saves and launches, never task reads."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import agent_settings, profile_registry as registry
from looper.agents.bus import MessageBus
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.runtimes import adapter, coco_login
from looper.agents.serve import seed_role_configs
from looper.db import agent_setting_layers, profile_registry as db, projects, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    executable = fake_claude(tmp_path / "coco", "print('[{\"name\":\"GPT-5.5\"}]')\n")
    monkeypatch.setattr(coco_login, "login_file", lambda: tmp_path / "login.json")
    monkeypatch.setattr(registry, "_login_modules", lambda: {})
    monkeypatch.setattr(coco_login, "launch_problem", AsyncMock(return_value=None))
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(coco_login, "time", SimpleNamespace(monotonic=lambda: clock.now))
    coco_login.forget_models()
    yield executable, clock
    coco_login.forget_models()


async def _models(executable):
    await db.insert_builtin_runtime("rt-coco", {
        "name": "coco", "type": "coco", "executable": executable, "version": "stand-in",
        "capabilities": sorted(c.value for c in adapter("coco").capabilities), "probe_error": None})
    result = {}
    for name in ("GPT-5.5", "GPT-9"):
        answer = await registry.create_model({"provider_id": "prov-coco-login", "name": name,
                                               "protocol": "openai-responses"})
        result[name] = answer["model"]["model_id"]
    return result


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("cache", ["cold", "expired", "unavailable"])
def test_profile_save_refreshes_catalog_off_the_loop(catalog, tmp_path, monkeypatch, action, cache):
    executable, clock = catalog

    async def test(pool):
        models = await _models(executable)
        fields = {"name": "Coco", "runtime_id": "rt-coco", "model_id": models["GPT-5.5"]}
        profile_id = (await registry.create_profile(fields))["profile"]["profile_id"]
        if cache == "expired":
            await asyncio.to_thread(coco_login.available_models, executable)
            clock.now += coco_login._MODELS_TTL_SEC + 1
        else:
            coco_login.forget_models()
        assert coco_login.cached_models(executable) is None
        if cache == "unavailable":
            fake_claude(tmp_path / "coco", "raise SystemExit(1)\n")
        available = coco_login.available_models
        loop_thread = threading.get_ident()
        threads = []

        def refresh(command):
            threads.append(threading.get_ident())
            assert threads[-1] != loop_thread
            return available(command)

        monkeypatch.setattr(coco_login, "available_models", refresh)
        async with v1_api(tmp_path, pool) as call:
            body = {**fields, "name": "Another Coco", "model_id": models["GPT-9"]}
            if action == "update":
                body = {"profile_id": profile_id, "fields": {"model_id": models["GPT-9"]}}
            status, answer = await call(f"profiles.{action}", body)
            if cache == "unavailable":
                assert status == 200, answer
                assert coco_login.cached_models(executable) is None
            else:
                assert status == 400, answer
                assert answer["error"]["field"] == "model"
                assert "coco has no model 'GPT-9'" in answer["error"]["message"]
                assert (await db.get_profile(profile_id))["model_id"] == models["GPT-5.5"]
            assert threads

    run_db(test, monkeypatch, timeout=30)


def test_missing_catalog_model_keeps_task_and_settings_readable(catalog, tmp_path, monkeypatch):
    executable, _ = catalog

    async def test(pool):
        models = await _models(executable)
        # A previously saved profile can outlive an upstream model's availability.
        await seed_role_configs(pool)
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "VALUES ('coco-profile', 'Coco', 'rt-coco', $1)", models["GPT-9"])
        await agent_setting_layers.put_layer("system", None, None, {"profile_id": "coco-profile"})
        project = await projects.create_project(name="Catalog", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="catalog-task", requirement="Read the task", status="ready",
                                project_id=project["id"], work_dir=str(tmp_path), pool=pool)
        async with pool.acquire() as conn:
            await agent_setting_layers.set_profile_overrides(conn, "catalog-task", {"worker": "coco-profile"})
        await asyncio.to_thread(coco_login.available_models, executable)
        assert coco_login.cached_models(executable) == {"gpt-5.5": "GPT-5.5"}
        async with v1_api(tmp_path, pool) as call:
            for action, body in (
                ("/api/v1/ops/tasks/get", {"task_id": "catalog-task"}),
                ("tasks.detail", {"task_id": "catalog-task"}),
                ("agent_settings.get", {}),
                ("roles.get", {"role": "worker"}),
            ):
                status, answer = await call(action, body)
                assert status == 200, (action, answer)
                assert answer["status"] == "ok", answer
        manager = ManagerOps(Platform(bus=MessageBus(runtime_root=tmp_path),
            task_router=SimpleNamespace(has_planner=lambda task_id: False)), url="http://127.0.0.1:9", token="test")
        turn = Turn(channel="web", chat="catalog", message_id="read")
        manager.begin_turn(turn, binding="catalog-read")
        try:
            answer = await manager.call("looper_tasks", {"action": "get", "task_id": "catalog-task"},
                                        binding="catalog-read")
            assert answer["status"] == "ok", answer
        finally:
            manager.end_turn(turn)
        row = await db.get_profile("coco-profile")
        profile = registry.ResolvedAgentProfile.from_row(row)
        assert profile.provider.catalog[0].name == "GPT-9"
        problem = await agent_settings.profile_launch_problem(profile, agent_settings.AgentType.WORKER)
        assert "coco has no model 'GPT-9'" in problem

    run_db(test, monkeypatch, timeout=30)
