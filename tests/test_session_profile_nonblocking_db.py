"""Session reads and selection never wait for runtime authentication probes."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from looper.agents.agent_settings import AgentType, Launch, load_agent_settings
from looper.agents import profile_registry
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.profile_registry import RegistryError
from looper.agents.runtimes import adapter, coco_login, codex_login, cursor_login, ttadk
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import agent_sessions, profile_registry as db_registry
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires a disposable database")


@pytest.mark.parametrize("runtime,launcher", [("codex", "ttadk"), ("cursor-agent", "native"), ("coco", "native")])
def test_session_reads_and_selection_do_not_wait_for_probes(runtime, launcher, monkeypatch, tmp_path):
    module = {"codex": ttadk, "cursor-agent": cursor_login, "coco": coco_login}[runtime]
    module.forget_probe()
    completed = threading.Event()
    calls = []

    def slow_probe(*args):
        calls.append(args)
        time.sleep(5)
        completed.set()
        if module is ttadk:
            return ttadk.TtadkProblem.NOT_SIGNED_IN
        return module.LoginStatus(module.LoginState.NOT_LOGGED_IN, "not signed in")

    monkeypatch.setattr(module, "_sign_in_problem" if module is ttadk else "probe_login", slow_probe)
    monkeypatch.setattr(codex_login, "launch_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter(runtime).__class__, "ttadk_problem", lambda self: None)
    monkeypatch.setattr(coco_login, "available_models", lambda *args: pytest.fail("view started a models probe"))
    # Guard against accidental calls into the operator's installed tools or homes.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})

    async def body(pool):
        capabilities = [c.value for c in adapter(runtime).capabilities]
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, launcher, capabilities) "
                           "VALUES ('rt-view', 'View runtime', $1, $2, $3)", runtime, launcher, capabilities)
        provider = {"codex": "prov-codex-login", "cursor-agent": "prov-cursor-login", "coco": "prov-coco-login"}[runtime]
        await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                           "VALUES ('model-view', $1, 'view-model', 'openai-responses')", provider)
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "SELECT 'profile-view', 'View profile', 'rt-view', id FROM llm_models "
                           "WHERE provider_id=$1 ORDER BY id LIMIT 1", provider)
        await agent_sessions.select_profile("manager", "profile-view")
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._session_loaded = True
        async def no_executor(*args, **kwargs):
            pytest.fail("a session view or selection used the default executor")

        to_thread = asyncio.to_thread
        recordings = []

        async def recording_only(function, *args, **kwargs):
            # Recording may materialize a runtime catalog off-loop; authentication must use its own probe.
            assert isinstance(getattr(function, "__self__", None), Launch)
            assert function.__func__ is Launch.record and not args and not kwargs
            recordings.append(function)
            return await to_thread(function)

        with monkeypatch.context() as read_patch:
            read_patch.setattr(asyncio, "to_thread", no_executor)
            started = time.monotonic()
            views = await asyncio.wait_for(asyncio.gather(manager.session_view(), manager.session_view()), 1)
            elapsed = time.monotonic() - started
            # Saving and recording a new choice must not re-enter a blocking launch check.
            await agent_sessions.select_profile("manager", None)
            read_patch.setattr(asyncio, "to_thread", recording_only)
            started_select = time.monotonic()
            await asyncio.wait_for(manager.select_session_profile("profile-view", origin={}, via="test"), 1)
            assert time.monotonic() - started_select < 1
        assert len(recordings) == 1
        assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE agent_id='manager' "
                                   "AND status='completed' AND metadata->'session_profile'->>'to'='profile-view'") == 1
        for view in views:
            choice = next(c for c in view["choices"] if c["profile_id"] == "profile-view")
            assert choice["problem"] is None and choice["checking"] is True
            assert view["problem"] is None
        assert (await agent_sessions.get_session("manager"))["selected_profile_id"] == "profile-view"

        # Launches still wait, and reject the signed-out profile once the probe completes.
        launch = asyncio.create_task(load_agent_settings(AgentType.MANAGER))
        await asyncio.sleep(0.05)
        assert not launch.done()
        settings = await asyncio.wait_for(launch, 7)
        assert completed.is_set() and settings.profile_error
        view = await manager.session_view()
        choice = next(c for c in view["choices"] if c["profile_id"] == "profile-view")
        assert choice["problem"] == settings.profile_error and not choice["checking"]
        with pytest.raises(RegistryError):
            await manager.select_session_profile("profile-view", origin={}, via="test")
        assert len(calls) == 1
        print(f"{runtime}: concurrent session views {elapsed * 1000:.1f} ms")

    try:
        # Schema initialization has its own budget; the calls above retain their one-second deadlines.
        run_db(body, monkeypatch, timeout=120)
    finally:
        # Join the background refresh before monkeypatch teardown or leaving the test.
        if module is coco_login:
            for lock in list(coco_login._probe_locks.values()):
                with lock:
                    pass
        module.forget_probe()


def test_choices_read_profiles_once(monkeypatch, tmp_path):
    from looper.agents import session_profile

    async def body(pool):
        original = db_registry.list_profiles
        listed = AsyncMock(wraps=original)
        monkeypatch.setattr(db_registry, "list_profiles", listed)
        settings = await load_agent_settings(AgentType.MANAGER, wait=False)
        monkeypatch.setattr(db_registry, "get_profile", AsyncMock(side_effect=AssertionError("N+1 read")))
        assert await session_profile.choices(None, settings)
        assert listed.await_count == 1

    run_db(body, monkeypatch, timeout=120)


def test_session_profile_http_readback(monkeypatch, tmp_path):
    from looper.agents import api_v1

    async def body(pool):
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        await manager.session_view()
        server = AgentHTTPServer(host="127.0.0.1", port=0, manager=manager, web_adapter=SimpleNamespace(),
                                 task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 static_dir=tmp_path, runtime_root=tmp_path, operator_token="test-token",
                                 client_ops=ClientOps(Platform(bus=None, task_router=None, runtime_root=tmp_path)))
        operations = api_v1.operations

        def register_operations(*args, **kwargs):
            assert server._server is None, "API registration must finish before accepting requests"
            return operations(*args, **kwargs)

        # Exercise a cold registry even when an earlier test already routed an API call.
        api_v1._index.cache_clear()
        monkeypatch.setattr(api_v1, "operations", register_operations)
        try:
            await server.start()
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server._port}", timeout=1,
                                         trust_env=False, headers={"Authorization": "Bearer test-token"}) as client:
                started = time.monotonic()
                response = await client.post("/api/v1/manager_session/get", json={})
                read_elapsed = time.monotonic() - started
                assert response.status_code == 200, response.text
                choice = response.json()["result"]["choices"][0]
                assert choice["checking"] is False
                response = await client.post("/api/v1/manager_session/select_profile",
                                             json={"profile_id": choice["profile_id"]})
                assert response.status_code == 200, response.text
                assert response.json()["result"]["selected"]["profile_id"] == choice["profile_id"]
                assert time.monotonic() - started < 1
                print(f"HTTP session read {read_elapsed * 1000:.1f} ms; "
                      f"selection/readback {(time.monotonic() - started - read_elapsed) * 1000:.1f} ms")
        finally:
            await server.stop()

    # Server and database startup are outside the API latency assertion.
    run_db(body, monkeypatch, timeout=120)
