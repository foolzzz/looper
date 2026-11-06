"""Advisory checks cannot delay the real HTTP listener or stop it on failure."""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from looper import db, ops_config
from looper.agents import api_v1, operator_notes, profile_registry, serve
from tests.db_support import DSN, run_db, until


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("check_fails", [False, True], ids=["warning", "exception"])
def test_http_answers_before_slow_advisory_check_finishes(monkeypatch, tmp_path, caplog, check_fails):
    class Checked(Exception):
        pass

    async def run(pool):
        started, finished = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        servers = []
        ok = serve.self_check.Check("test", "Test", "ok", "ready")

        def slow_claude(_):
            loop.call_soon_threadsafe(started.set)
            try:
                time.sleep(3)
                if check_fails:
                    raise RuntimeError("advisory probe crashed")
                return [serve.self_check.Check("claude_cli", "Claude CLI", "warn", "slow probe problem")]
            finally:
                loop.call_soon_threadsafe(finished.set)

        http_start = serve.AgentHTTPServer.start

        async def start_http(server):
            servers.append(server)
            await http_start(server)
            # Warm the readiness route before scheduling the slow probe: cold imports in
            # status reporting are outside the startup-check ordering under test.
            port = server._server.sockets[0].getsockname()[1]
            client.base_url = f"http://127.0.0.1:{port}"
            client.headers["Authorization"] = f"Bearer {server._operator_token}"
            response = await client.post("/api/v1/system/status", json={})
            assert response.status_code == 200
            result = response.json()["result"]
            assert {c["id"] for c in result["checks"] if c["status"] == "pending"} == {
                "claude_cli", "claude_login", "codex_login", "grok_login", "cursor_login", "ttadk",
                "llmrec", "gateway", "lark_cli", "git", "projects_root",
            }
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/system/status").result, result)

        async def check_readiness(_):
            await asyncio.wait_for(started.wait(), 1)
            # The prompt eval uses this readiness route; it must work while the check is sleeping.
            response = await client.post("/api/v1/system/status", json={})
            assert response.status_code == 200
            assert not finished.is_set()
            assert next(c for c in response.json()["result"]["checks"] if c["id"] == "claude_cli")["status"] == "pending"
            assert any(c["id"] == "test" and c["status"] == "ok" for c in response.json()["result"]["checks"])
            assert "slow probe problem" not in caplog.text
            await asyncio.wait_for(finished.wait(), 4)
            message = "advisory probe crashed" if check_fails else "slow probe problem"
            await until(lambda: message in caplog.text, f"logged {message}", timeout=1)
            response = await client.post("/api/v1/system/status", json={})
            assert response.status_code == 200  # A failed check never stops the listener.
            checks = response.json()["result"]["checks"]
            assert not any(c["status"] == "pending" for c in checks)
            if not check_fails:
                assert any(c["id"] == "claude_cli" and c["detail"] == message for c in checks)
            raise Checked

        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_enabled_bot_rows", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve.AgentHTTPServer, "start", start_http)
        monkeypatch.setattr(serve.AgentSupervisor, "start_all", check_readiness)
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=
                            serve.self_check.Check("database", "Database", "ok", "ready")))
        monkeypatch.setattr(serve.self_check, "check_claude", slow_claude)
        for name in ("codex_login_check", "grok_login_check", "cursor_login_check", "ttadk_check", "llmrec_check", "gateway_check"):
            monkeypatch.setattr(serve.self_check, name, AsyncMock(return_value=ok))
        monkeypatch.setattr(serve.self_check, "check_git", lambda: ok)
        monkeypatch.setattr(serve.self_check, "check_lark_cli", lambda _: ok)
        monkeypatch.setattr(serve.self_check, "check_projects_root", lambda _: ok)
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN,
                                   port=0, operator_notes_root=str(tmp_path))
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            with pytest.raises(Checked):
                await serve._run_agent_system(ops=ops, runtime_root=tmp_path, work_dir=tmp_path, skip_backup=True,
                                               runner_supervisor=SimpleNamespace(stop=AsyncMock()))
        assert "s since process start)" in caplog.text
        assert all(not task.get_name().startswith("advisory-startup") for task in asyncio.all_tasks()
                   if task is not asyncio.current_task())

    with caplog.at_level(logging.INFO):
        run_db(run, monkeypatch, timeout=60)


def test_advisory_checks_overlap_and_log_in_original_order(monkeypatch, caplog):
    async def run():
        started = []
        gate = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def check(name):
            started.append(name)
            if len(started) == 10:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)
            return serve.self_check.Check(name, name, "warn", name)

        def sync_check(name):
            return asyncio.run_coroutine_threadsafe(check(name), loop).result(timeout=2)

        monkeypatch.setattr(serve.self_check, "check_claude", lambda _: [sync_check("claude")])
        for name in ("codex", "grok", "cursor", "ttadk", "llmrec", "gateway"):
            monkeypatch.setattr(serve.self_check, name + "_check" if name in ("ttadk", "llmrec", "gateway") else name + "_login_check",
                                lambda name=name: check(name))
        monkeypatch.setattr(serve.self_check, "check_lark_cli", lambda _: sync_check("lark"))
        monkeypatch.setattr(serve.self_check, "check_git", lambda: sync_check("git"))
        monkeypatch.setattr(serve.self_check, "check_projects_root", lambda _: sync_check("projects"))
        snapshot = []
        await serve._advisory_startup_checks(snapshot, serve.self_check.Check("database", "Database", "ok", ""),
                                            claude_bin="unused", lark_enabled=False, projects_root="")
        expected = ["claude", "codex", "grok", "cursor", "ttadk", "llmrec", "gateway", "lark", "git", "projects"]
        assert [c["id"] for c in snapshot] == ["database", *expected]
        assert [r.message.split(":")[0].split()[-1] for r in caplog.records if "SELF-CHECK" in r.message] == expected

    asyncio.run(run())
