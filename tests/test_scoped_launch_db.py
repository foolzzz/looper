"""Scoped launch authority on the Manager bus, MCP bridge and completion path."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import httpx

from looper.agents import chat_scopes, session_routing, session_visibility
from looper.agents.agent_settings import Access
from looper.agents.claude_cli import ResidentClaude
from looper.agents.http_server import AgentHTTPServer
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.message import MessageType
from looper.agents.ops.service import ManagerOps, Platform, scope_for
from looper.agents.runtimes import ADAPTERS, CLAUDE_CODE
from looper.agents.tiers import manager as manager_module
from looper.db import manager_events
from tests.api_manager_support import _profile
from tests.db_support import DSN, run_db, until
from tests.test_scoped_routing_db import GROUP_A, LARK, MANAGER, OPERATOR, OTHER, _message, _send, _setup

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
START_REFUSAL = "这个群的能力范围在当前 Agent Profile 上跑不了，请操作者换一个支持的 Agent Profile。"


@asynccontextmanager
async def _endpoint(rig: Any, root: Path):
    router = SimpleNamespace(has_planner=lambda _: False)
    ops = ManagerOps(Platform(bus=rig.bus, task_router=router, manager=rig.manager),
                     url="http://127.0.0.1:9", scope=scope_for(rig.manager._definition),
                     turn_file=root / ".local" / "scoped-turn.json")
    server = AgentHTTPServer(host="127.0.0.1", port=0, manager_ops=ops, manager=rig.manager,
                             runtime_root=root, web_adapter=SimpleNamespace(),
                             supervisor=SimpleNamespace(bus=rig.bus), task_router=None)
    await server.start()
    assert server._port not in (0, 8767)
    ops._url = f"http://127.0.0.1:{server._port}"
    ops._working_at = ops._clock()
    rig.manager.ops = ops
    try:
        yield ops
    finally:
        await server.stop()


@pytest.mark.parametrize("scope,runtime", [("chat_only", "claude-code"), ("chat_only", "api"),
                                            ("full", "claude-code"), ("legacy", "claude-code")])
def test_launch_frozen_per_scope_and_runtime(scope, runtime, monkeypatch, tmp_path):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch,
                                      default="legacy" if scope == "legacy" else None)
        installed = AgentLibrary(items=(LoadedItem(ItemKind.PLUGIN, "synthetic", "a" * 64),),
                                 plugin_dirs=(str(tmp_path / "synthetic-plugin"),))
        monkeypatch.setattr(manager_module, "load_installed", AsyncMock(return_value=installed))
        if runtime == "api":
            await _profile(pool, "http://127.0.0.1:9")
            await pool.execute("UPDATE managers SET profile_id='agent-api-test' WHERE id=$1", MANAGER)
            monkeypatch.setattr(ADAPTERS["api"], "resident_session", CLAUDE_CODE.resident_session)
        async with _endpoint(rig, tmp_path) as ops:
            await rig.start()
            try:
                await _send(rig, "scope launch", OPERATOR if scope == "full" else OTHER)
                session = next(s for s in rig.manager._sessions.values() if s.scope is not None)
                process = session.process
                assert process is not None
                assert process.signature[-2:] == (session.scope.fingerprint, session.scope.mode.value)
                assert "scope" in process.signature_components
                rig.manager._current.set(session)
                root = rig.manager._session_root()
                cwd = rig.manager._session_work_dir()
                environment = rig.manager._session_environment(process.launch)
                command = list(process.signature[0])
                if scope == "chat_only":
                    assert process.launch.access_level is Access.PLATFORM_TOOLS
                    assert all(item.kind is ItemKind.MCP and item.name == "looper" for item in process.launch.library.items)
                    assert not process.launch.library.plugin_dirs
                    assert cwd.is_relative_to(root) and cwd != rig.manager._config.work_dir
                    assert await ops.current_listing(session_key=session.key) == []
                    if runtime == "claude-code":
                        assert command[command.index("--tools") + 1] == ""
                        assert command[command.index("--allowedTools") + 1] == "mcp__looper__*"
                        assert "--dangerously-skip-permissions" not in command
                    else:
                        assert process.launch.runtime.in_process
                else:
                    assert process.launch.access is None
                    assert installed.items[0] in process.launch.library.items
                    assert cwd == rig.manager._config.work_dir
                    assert environment == process.launch.environment()
                    own_listing = await ops.current_listing(session_key=session.key)
                    rig.manager._current.set(rig.manager._sessions["main"])
                    assert root != rig.manager._session_root()
                    main_command = rig.manager._claude_command(process.system_prompt.write_file(tmp_path), process.launch,
                                                               resume=False)
                    # These paths remain session-local even though full authority uses main's cwd and environment.
                    def normalized(args):
                        args = list(args)
                        for flag in ("--append-system-prompt-file", "--system-prompt-file", "--mcp-config", "--settings"):
                            if flag in args:
                                args[args.index(flag) + 1] = "<session-path>"
                        return args
                    assert normalized(command) == normalized(main_command)
                    assert own_listing == await ops.current_listing(session_key="main")
                    assert own_listing
                assert len(residents) >= 1 and residents[-1].starts
            finally:
                await rig.close()
    run_db(body, monkeypatch, timeout=90)


def _unsupported(monkeypatch):
    adapter = copy.copy(CLAUDE_CODE)
    adapter.launch_modes = frozenset({chat_scopes.LaunchMode.FULL})
    monkeypatch.setitem(ADAPTERS, "claude-code", adapter)
    return adapter


def test_unsupported_runtime_refuses_chat_only(monkeypatch, tmp_path, caplog):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        _unsupported(monkeypatch)
        await rig.start()
        try:
            message = await _send(rig, "unsupported")
            assert START_REFUSAL in await rig.reply(message)
            assert not any(r.starts or r.turns for r in residents)
            assert any(getattr(record, "mode", None) == "platform_tools" for record in caplog.records)
            await pool.execute("UPDATE manager_chat_bindings SET default_scope='legacy' WHERE chat_id=$1", GROUP_A)
            message = await _send(rig, "legacy supported")
            assert START_REFUSAL not in await rig.reply(message)
            assert any(r.starts and r.turns for r in residents)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_profile_switch_rechecks_a_live_process(monkeypatch, tmp_path):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        await rig.start()
        try:
            await _send(rig, "supported")
            resident = residents[0]
            assert resident.started and len(resident.turns) == 1
            _unsupported(monkeypatch)
            message = await _send(rig, "unsupported after profile switch")
            assert START_REFUSAL in await rig.reply(message)
            assert len(resident.turns) == 1
            assert not any(r.turns for r in residents[1:])
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_other_fingerprint_process_never_reused(monkeypatch, tmp_path):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        await rig.start()
        try:
            await _send(rig, "member")
            await _send(rig, "operator", OPERATOR)
            member, operator = [s for s in rig.manager._sessions.values() if s.scope is not None]
            assert member.claude is not operator.claude
            assert member.process.signature[-2:] != operator.process.signature[-2:]
            # Exercise the production resident comparison without spawning a model process.
            resident = ResidentClaude(label="synthetic-stale")
            resident._process = SimpleNamespace(returncode=None, stdin=None)
            resident._signature = member.process.signature
            assert not resident.stale(member.process.signature)
            assert resident.stale((*member.process.signature[:-2], "another-fingerprint", member.scope.mode.value))
            assert member.process.signature_components["scope"] != operator.process.signature_components["scope"]

            rig.manager._current.set(member)
            original = rig.manager._prepare_turn
            prepared_seen = []
            async def changed(*args, **kwargs):
                prepared = await original(*args, **kwargs)
                library = AgentLibrary(items=(LoadedItem(ItemKind.SKILL, "synthetic-refresh", "b" * 64),))
                prepared = replace(prepared, launch=replace(prepared.launch, library=library), switch=None,
                                   signature=(*prepared.signature[:-2], "different-fingerprint", prepared.signature[-1]))
                prepared_seen.append(prepared)
                return prepared
            monkeypatch.setattr(rig.manager, "_prepare_turn", changed)
            member.claude.pid = 12345
            monkeypatch.setattr(member.claude, "try_stop_if_idle", AsyncMock(return_value=False))
            errors = []
            run = rig.manager._run_prepared
            async def observed(*args, **kwargs):
                try:
                    return await run(*args, **kwargs)
                except chat_scopes.ScopeInvariantViolation as exc:
                    errors.append(exc)
                    raise
            monkeypatch.setattr(rig.manager, "_run_prepared", observed)
            message = await _send(rig, "refresh mismatch")
            assert prepared_seen and errors
            assert rig.replies[message.message_id][-1].payload["outcome"] == "failed"
            assert len(member.claude.turns) == 1
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("name,capability", [("looper_task_create", "tasks.create"),
                                              ("looper_nonexistent_synthetic", "unknown")],
                         ids=["known", "unknown"])
def test_guessed_tool_is_out_of_scope(name, capability, monkeypatch, tmp_path, caplog):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        raw = {"project": "synthetic-project-secret", "requirement": "synthetic-requirement-secret"}
        async with _endpoint(rig, tmp_path) as ops:
            message = _message(rig, "guess a tool")
            session = await rig.manager._router.target(message)
            answers = []
            async def calls(on_event):
                config = ops.server_config(session_key=session.key)
                requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "synthetic"}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": raw}}]
                process = await asyncio.create_subprocess_exec(config["command"], *config["args"],
                    env={**os.environ, **config["env"]}, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(
                    "".join(json.dumps(request) + "\n" for request in requests).encode()), 20)
                answers.append((process.returncode, stdout.decode(), stderr.decode()))
            session.claude.on_turn = calls
            await rig.start()
            try:
                before = await pool.fetchval("SELECT count(*) FROM tasks")
                await rig.bus.publish(message)
                await rig.reply(message)
                await until(lambda: not rig.manager._router.pending())
                assert len(answers) == 1 and answers[0][0] == 0, answers
                results = {row["id"]: row for row in map(json.loads, answers[0][1].splitlines())}
                assert results[2]["result"]["tools"] == []
                answer = json.loads(results[3]["result"]["content"][0]["text"])
                assert answer == {"status": "error", "error": {"code": "forbidden", "reason": "out_of_scope",
                    "capability": capability, "message": f"this chat's scope does not include {capability}: it did not run and nothing waits; tell the speaker that the operator can change this group's scope"}}
                rows = await pool.fetch("SELECT * FROM manager_actions WHERE tool=$1", name)
                assert len(rows) == 1
                row = rows[0]
                assert json.loads(row["arguments"]) == dict.fromkeys(raw)
                assert (row["status"], row["error_code"]) == ("error", "forbidden")
                assert row["risk"] == ("write" if capability == "tasks.create" else "read")
                assert json.loads(row["result"]) == {"reason": "out_of_scope", "capability": capability,
                    "scope": "chat_only@1", "principal_kind": "person"}
                assert await pool.fetchval("SELECT count(*) FROM tasks") == before
                assert not ops._pending and not ops.asks_in(message.message_id)
                assert "looper_tasks" not in answer["error"]["message"]
                refusals = [record for record in caplog.records if getattr(record, "reason", None) == "out_of_scope"]
                assert len(refusals) == 1
                assert refusals[0].manager_id == MANAGER
                assert refusals[0].chat_digest == session_routing.key_digest(session.key)
                assert refusals[0].capability == capability and refusals[0].principal_kind == "person"
                logs = "\n".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
                assert all(value not in logs for value in raw.values())
                assert OTHER not in logs
            finally:
                await rig.close()
    with caplog.at_level(logging.INFO):
        run_db(body, monkeypatch, timeout=90)


def test_invariant_refuses_injected_skip_permissions(monkeypatch, tmp_path, caplog):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        original = rig.manager._claude_command
        monkeypatch.setattr(rig.manager, "_claude_command", lambda *a, **kw:
                            [*original(*a, **kw), "--dangerously-skip-permissions"])
        await rig.start()
        try:
            message = await _send(rig, "injected permission flag")
            reply = rig.replies[message.message_id][-1]
            assert reply.payload["error"] == "scope_invariant"
            assert reply.payload["outcome"] == "failed"
            assert not any(resident.starts or resident.turns for resident in residents)
            assert any(record.levelno == logging.CRITICAL for record in caplog.records)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_chat_only_completion_is_a_code_notice(monkeypatch, tmp_path):
    async def body(pool):
        rig, residents = await _setup(pool, tmp_path, monkeypatch)
        session = await rig.manager._router.target(_message(rig, "completion target"))
        assert session_visibility.for_session(session.key, manager_id=MANAGER) is not None
        await rig.bus.start()
        rig.manager._bus = rig.bus
        rig.manager._current.set(session)
        notice = {"channel": "lark", "channel_id": LARK.agent_id, "text": "Synthetic completion notice",
                  "target": {"kind": "chat", "id": GROUP_A}}
        await pool.execute("INSERT INTO projects(id, name, work_dir) VALUES('scoped-project', 'Synthetic', $1)", str(tmp_path))
        await pool.execute("INSERT INTO tasks(id, requirement, status, project_id) "
                           "VALUES('scoped-completion', 'Synthetic', 'completed', 'scoped-project')")
        await manager_events.record(manager_id=MANAGER, task_id="scoped-completion", origin="chat", target_kind="dm",
            channel_id=LARK.agent_id, chat=GROUP_A, dedupe_key="synthetic-completion",
            payload={"chat_id": GROUP_A}, fallback=[{"via": "notice", "notice": notice}], state="pending", reason=None)
        batch = uuid.uuid4()
        rows = await manager_events.claim(MANAGER, LARK.agent_id, GROUP_A, batch)
        try:
            await rig.manager._tell_completion(rows, batch)
            row = await pool.fetchrow("SELECT state, reason FROM manager_events WHERE task_id='scoped-completion'")
            assert row["state"] == "fallback" and row["reason"] == "group_chat"
            queue = rig.bus.get_queue(LARK.agent_id)
            sent = [queue.get_nowait() for _ in range(queue.qsize())]
            assert any(envelope.message_type is MessageType.CHAT_NOTICE and envelope.payload == notice for envelope in sent)
            assert not any(resident.starts or resident.turns for resident in residents)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_retirement_revokes_credential_before_forgetting_scope(monkeypatch, tmp_path):
    async def body(pool):
        rig, _ = await _setup(pool, tmp_path, monkeypatch)
        async with _endpoint(rig, tmp_path) as ops:
            await rig.start()
            try:
                await _send(rig, "old scope")
                session = next(s for s in rig.manager._sessions.values() if s.scope is not None)
                credential = ops.server_config(session_key=session.key)["env"]["LOOPER_AGENT_TOKEN"]
                assert session_visibility.scoped_for(MANAGER, session.key) is not None
                assert ops.session_for(f"Bearer {credential}") == session.key
                await pool.execute("UPDATE manager_chat_bindings SET default_scope='legacy' WHERE chat_id=$1", GROUP_A)
                await _send(rig, "new scope")
                assert session.key not in rig.manager._sessions
                assert session_visibility.scoped_for(MANAGER, session.key) is None
                async with httpx.AsyncClient(base_url=ops._url, trust_env=False) as client:
                    response = await client.get("/api/agent/v1/tools", headers={"Authorization": f"Bearer {credential}"})
                assert response.status_code == 401, response.text
            finally:
                await rig.close()
    run_db(body, monkeypatch, timeout=90)
