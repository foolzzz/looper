"""Task residents on a disposable database and a scripted stream-json process."""
from __future__ import annotations

import json
from dataclasses import replace
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import operator_mcp, outbound_hook, task_conversations_settings as settings, task_manager_safety as safety
from looper.agents import task_manager_sandbox, task_manager_settings
from looper.agents.agent_settings import AgentSettings
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.tiers import task_manager as tm
from looper.db import projects, tasks
from tests.db_support import run_db
from tests.fake_claude import fake_claude


@pytest.fixture
def setup_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_enabled", True)
    monkeypatch.setattr(settings, "_manager_answers", True)
    monkeypatch.setattr(outbound_hook, "known_enabled", lambda: False)
    monkeypatch.setattr(task_manager_settings, "load_task_manager_settings", AsyncMock(return_value=AgentSettings(profile=builtin_resolved_profile())))
    monkeypatch.setattr(safety, "gate", AsyncMock(return_value=None))
    monkeypatch.setattr(task_manager_sandbox, "SUPPORTED_PLATFORM", sys.platform)  # a scripted CLI on any platform
    executable = fake_claude(tmp_path / "claude", '''
import sys, json, os, time
if '--version' in sys.argv:
    print('2.1.332 (Claude Code)'); sys.exit()
session = sys.argv[sys.argv.index('--resume')+1] if '--resume' in sys.argv else 'task-session'
for line in sys.stdin:
    data = json.loads(line)
    if data.get('type') == 'control_request':
        print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':data['request_id']}}), flush=True)
        print(json.dumps({'type':'result','subtype':'success','result':'stopped','session_id':session,'total_cost_usd':0.1}), flush=True)
        continue
    text = data.get('message',{}).get('content','')
    if text == 'fail-resume' and '--resume' in sys.argv:
        sys.exit(2)
    print(json.dumps({'type':'system','subtype':'init','session_id':session}), flush=True)
    if text == 'wait':
        continue
    if text == 'write':
        open('changed.txt','w').write('changed')
    print(json.dumps({'type':'stream_event','event':{'delta':{'type':'text_delta','text':'one'}}}), flush=True)
    print(json.dumps({'type':'stream_event','event':{'delta':{'type':'text_delta','text':'two'}}}), flush=True)
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'call','name':'Read','input':{'file_path':'sample'}}]}}), flush=True)
    print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'call','content':'contents'}]}}), flush=True)
    print(json.dumps({'type':'result','subtype':'success','result':'onetwo','session_id':session,'total_cost_usd':0.1}), flush=True)
''')

    async def create(pool, task_id="task-one", status="draft", work_dir=None, workspace_mode="in_place"):
        project = await projects.create_project(name=f"Project {task_id}", work_dir=str(work_dir or tmp_path), pool=pool)
        await tasks.create_task(task_id=task_id, requirement="Implement a feature", project_id=project["id"],
                                status=status, work_dir=str(work_dir or tmp_path), workspace_mode=workspace_mode,
                                workspace_dir=str(work_dir or tmp_path), pool=pool)
        return task_id

    return executable, create


def test_pool_constructs_without_a_process(monkeypatch, tmp_path, setup_manager):
    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        pool = tm.TaskManagerPool(tmp_path / 'runtime', executable, max_resident=lambda: 2)
        try:
            settings.set_enabled(False)
            agent = pool.agent(task_id)
            with pytest.raises(tm.Refused) as refused:
                await agent.prepare()
            assert refused.value.reason is tm.RefuseReason.WORK_MODE_OFF
            assert not agent.resident.started
            assert tm.current_pool() is pool
        finally:
            await pool.shutdown()
        assert tm.current_pool() is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("failure", ["hook", "gate", "spawn"])
def test_failed_tool_launch_revokes_credential(monkeypatch, tmp_path, setup_manager, failure):
    from types import SimpleNamespace
    from looper.agents.ops.service import Platform
    from looper.agents.ops.task_manager import TaskManagerOps

    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        ops = TaskManagerOps(Platform(bus=AsyncMock(), task_router=SimpleNamespace(has_planner=lambda _: False)),
                             url="http://127.0.0.1:18915")
        pool = tm.TaskManagerPool(tmp_path / "runtime", executable, ops=ops, max_resident=lambda: 1)
        try:
            if failure == "hook":
                # Only the read-only orchestrated session carries the review hook; a direct one has none.
                await tasks.update_task_status(task_id, "pending", pool=db)
                monkeypatch.setattr(outbound_hook, "known_enabled", lambda: True)
                monkeypatch.setattr(ops, "tool_hook", lambda *args: None)
            elif failure == "gate":
                monkeypatch.setattr(safety, "gate", AsyncMock(return_value=SimpleNamespace(
                    reason=tm.RefuseReason.CLI_UNVERIFIED, notice="Unverified synthetic version")))
            else:
                monkeypatch.setattr(tm.ResidentClaude, "start", AsyncMock(side_effect=OSError("Synthetic spawn failure")))
            with pytest.raises((tm.Refused, OSError)):
                await pool.agent(task_id).prepare()
            assert not ops._grants
            assert not pool.agent(task_id).resident.started
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_prepare_operator_and_read_only_commands(monkeypatch, tmp_path, setup_manager):
    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        pool = tm.TaskManagerPool(tmp_path / 'runtime', executable, max_resident=lambda: 2)
        try:
            agent = pool.agent(task_id)
            _, _, launch, _ = await agent.prepare()
            direct = agent.resident.command
            # The sandboxed direct session reads the user's settings only, and has the built-in tool allowlist.
            assert direct[direct.index('--setting-sources') + 1] == 'user'
            assert direct[direct.index('--tools') + 1] == ','.join(task_manager_sandbox.BUILTIN_TOOLS)
            assert '--append-system-prompt-file' in direct
            await tasks.update_task_status(task_id, 'pending', pool=db)
            agent.session_id = 'task-session'
            await agent.prepare()
            readonly = agent.resident.command
            assert '--setting-sources' in readonly
            assert 'Bash,Edit,Write,NotebookEdit,EnterWorktree' in readonly
            assert readonly[-2:] == ['--resume', 'task-session']
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_direct_session_loads_only_looper_and_allowed_mcp_servers(monkeypatch, tmp_path, setup_manager):
    from types import SimpleNamespace

    from looper.agents.ops.service import Platform
    from looper.agents.ops.task_manager import TaskManagerOps

    home, work = tmp_path / "home", tmp_path / "work"
    work.mkdir()
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"alpha": {"type": "stdio", "command": "/opt/mcp/alpha"},
                                                                  "beta": {"type": "stdio", "command": "/opt/mcp/beta"},
                                                                  "unlisted": {"type": "stdio", "command": "/opt/mcp/other"}}}))
    # The task directory's own file defines an allowed name differently: it is ignored.
    (work / ".mcp.json").write_text(json.dumps({"mcpServers": {"beta": {"type": "stdio", "command": "/opt/mcp/hostile"}}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def servers(command):
        return sorted(json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())["mcpServers"])

    async def check(db):
        executable, create = setup_manager
        task_id = await create(db, work_dir=work)
        ops = TaskManagerOps(Platform(bus=AsyncMock(), task_router=SimpleNamespace(has_planner=lambda _: False)),
                             url="http://127.0.0.1:18915")
        pool = tm.TaskManagerPool(tmp_path / "runtime", executable, ops=ops, max_resident=lambda: 2)
        try:
            agent = pool.agent(task_id)
            await agent.prepare()
            command = agent.resident.command
            assert command.count("--strict-mcp-config") == 1 and command.count("--mcp-config") == 1
            assert servers(command) == ["looper"]  # default: only Looper's own server
            monkeypatch.setattr(settings, "_manager_mcp", ("alpha",))
            await agent.prepare()
            first = Path(agent.resident.command[agent.resident.command.index("--mcp-config") + 1])
            assert first.is_file() and servers(agent.resident.command) == ["alpha", "looper"]
            leftover = pool.runtime_root / "task-managers" / task_id / "mcp-cwd" / "left-by-a-server"
            leftover.write_text("x")
            monkeypatch.setattr(settings, "_manager_mcp", ("alpha", "beta"))
            await agent.prepare()
            assert not leftover.exists()  # a restart starts the servers in an empty directory  # a changed allowlist restarts the resident with the new servers
            assert servers(agent.resident.command) == ["alpha", "beta", "looper"]
            written = json.loads(Path(agent.resident.command[agent.resident.command.index("--mcp-config") + 1]).read_text())
            # Every stdio server, Looper's own included, starts through the wrapper in a private directory; the
            # fingerprints are those of the user-scope definitions, before wrapping.
            assert all(operator_mcp.is_wrapped(entry) for entry in written["mcpServers"].values())
            assert operator_mcp.unwrap(written["mcpServers"]["beta"])["command"] == "/opt/mcp/beta"
            assert agent.mcp_fingerprints == {
                "alpha": operator_mcp.fingerprint({"type": "stdio", "command": "/opt/mcp/alpha"}),
                "beta": operator_mcp.fingerprint({"type": "stdio", "command": "/opt/mcp/beta"})}
            second = Path(agent.resident.command[agent.resident.command.index("--mcp-config") + 1])
            assert not first.exists() and second.is_file()  # the replaced config, which can hold secrets, is gone
            await agent.resident.stop()
            assert not second.exists()  # and so is the one of an ended session
            await agent.prepare()
            assert Path(agent.resident.command[agent.resident.command.index("--mcp-config") + 1]).is_file()
            assert agent.resident.command.count("--strict-mcp-config") == 1
            await tasks.update_task_status(task_id, "pending", pool=db)
            await agent.prepare()  # the read-only orchestrated session keeps its own isolation, unaffected
            readonly = agent.resident.command
            assert "--setting-sources" in readonly and servers(readonly) == ["looper"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.parametrize("review_on", [False, True])
def test_task_tools_follow_process_and_turn_lifetime(monkeypatch, tmp_path, setup_manager, review_on):
    from types import SimpleNamespace
    from looper.agents.ops.service import Platform
    from looper.agents.ops.task_manager import TaskManagerOps
    monkeypatch.setattr(outbound_hook, "known_enabled", lambda: review_on)

    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        ops = TaskManagerOps(Platform(bus=AsyncMock(), task_router=SimpleNamespace(has_planner=lambda _: False)),
                             url="http://127.0.0.1:18915")
        pool = tm.TaskManagerPool(tmp_path / "runtime", executable, ops=ops, max_resident=lambda: 1,
                                  clock=lambda: 2000)

        async def token_for(agent):
            _, _, launch, _ = await agent.prepare()
            config = json.loads(Path(launch.library.mcp_config).read_text())
            token = config["mcpServers"]["looper"]["env"]["LOOPER_AGENT_TOKEN"]
            # A direct session carries no outbound-review hook, whatever the switch says.
            hook = pool.runtime_root / ".local" / "outbound-hook" / f"task-manager-{agent.task_id}.json"
            assert not hook.exists()
            return token

        async def ignore(event):
            pass

        try:
            agent = pool.agent(task_id)
            token = await token_for(agent)
            header = "Bearer " + token
            assert ops.authorized(header)
            assert await token_for(agent) == token
            replies = []

            async def on_event(event):
                if event.get("type") == "system":
                    replies.append(await ops.call(header, "task_requirement_set", {"requirement": "Updated"}))

            await agent.resident.run_turn("read", on_event)
            assert replies[0]["status"] == "ok"
            assert (await ops.call(header, "task_requirement_set", {"requirement": "Outside"}))["error"]["code"] == "forbidden"
            await pool.close(task_id)
            assert not ops.authorized(header)
            assert not (pool.runtime_root / ".local" / "outbound-hook" / f"task-manager-{task_id}.json").exists()
            token = await token_for(agent)
            assert token != header.removeprefix("Bearer ")
            await agent.resident.run_turn("read", ignore)
            agent.last_used = 0
            await pool.reclaim()
            assert not ops.authorized("Bearer " + token)
            token = await token_for(agent)
            await agent.resident.run_turn("read", ignore)
            other_id = await create(db, "task-two")
            assert await pool._slot(pool.agent(other_id))
            assert not ops.authorized("Bearer " + token)
            token = await token_for(agent)
            await pool.forget(task_id)
            assert not ops.authorized("Bearer " + token)
            token = await token_for(pool.agent(other_id))
        finally:
            await pool.shutdown()
        assert not ops.authorized("Bearer " + token)
    run_db(check, monkeypatch)


def test_a_direct_session_drops_proxy_variables_and_the_ssh_agent_an_orchestrated_one_keeps_them(
        monkeypatch, tmp_path, setup_manager):
    from looper.agents import task_manager_sandbox

    for name in task_manager_sandbox.DIRECT_DROPPED_ENV:
        if name not in ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
            monkeypatch.setenv(name, "http://127.0.0.1:9" if "proxy" in name.lower() else "x")
    seen = []

    async def start(self, command, **kwargs):
        seen.append(dict(kwargs["env"]))

    monkeypatch.setattr(tm.ResidentClaude, "start", start)
    # A secret an Agent Profile adds through its own env (GH_TOKEN) must not reach the sandboxed Bash either.
    profile = AgentSettings(profile=builtin_resolved_profile())
    monkeypatch.setattr(task_manager_settings, "load_task_manager_settings", AsyncMock(
        return_value=replace(profile, env=(("GH_TOKEN", "gh-secret"), ("ANTHROPIC_MODEL", "x")))))

    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", executable, max_resident=lambda: 2)
        try:
            await pool.agent(task_id).prepare()
            await tasks.update_task_status(task_id, "pending", pool=db)
            await pool.agent(task_id).prepare()
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)
    direct, orchestrated = seen
    assert not set(task_manager_sandbox.DIRECT_DROPPED_ENV) & set(direct)
    assert "GH_TOKEN" not in direct and direct.get("ANTHROPIC_MODEL") == "x"  # the CLI's own profile variables stay
    # The positive control: the same variables reach an orchestrated session, so the drop is the direct one's own.
    assert {"HTTPS_PROXY", "https_proxy", "SSH_AUTH_SOCK", "GH_TOKEN"} <= set(orchestrated)


@pytest.mark.parametrize("with_key", [True, False])
def test_a_direct_launch_warns_about_a_provider_credential_without_logging_it(monkeypatch, tmp_path, setup_manager,
                                                                               caplog, with_key):
    secret = "sk-ant-never-in-a-log"
    if with_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def start(self, command, **kwargs):
        pass

    monkeypatch.setattr(tm.ResidentClaude, "start", start)

    async def check(db):
        executable, create = setup_manager
        task_id = await create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", executable, max_resident=lambda: 1)
        try:
            with caplog.at_level("WARNING"):
                await pool.agent(task_id).prepare()
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)
    warned = [r.getMessage() for r in caplog.records if "sandboxed Bash can read" in r.getMessage()]
    assert bool(warned) is with_key and secret not in caplog.text
    if with_key:
        assert "ANTHROPIC_API_KEY" in warned[0]
