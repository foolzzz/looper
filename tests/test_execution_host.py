"""Listener admission and identity at the persistent execution boundary."""
import asyncio
from dataclasses import replace
import json
import os
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper import ops_config
from looper.agents import execution_host, proc_tree, serve
from looper.agents.http_server import AgentHTTPServer


@pytest.mark.parametrize("config_kind", ["absent-default", "default", "custom"])
def test_host_child_loads_optional_default_config(tmp_path, monkeypatch, config_kind):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "runtime"
    path = root / ("custom.toml" if config_kind == "custom" else ops_config.FILE)
    database_url = "postgresql://postgres:postgres@127.0.0.1:54329/looper_test_host_config"
    monkeypatch.setenv("LOOPER_DATABASE_URL", database_url)
    if config_kind != "absent-default":
        ops_config.write_private(path, ops_config.render(ops_config.OpsConfig(
            runtime_root=root, path=path, database_url=database_url)))
    ops = ops_config.load(ops_config.Flags(runtime_root=root,
        config=path if config_kind == "custom" else None, port=18834))
    # Run the actual child argument parser and loader in another interpreter;
    # only replace its database/agent workload, which is outside this regression.
    program = """
import json
from looper.agents import execution_host
async def capture(**kwargs):
    ops = kwargs['ops']
    print(json.dumps({'database_url': ops.database_url, 'port': ops.port, 'path': str(ops.path)}))
execution_host.run_host = capture
execution_host.main()
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    result = subprocess.run([sys.executable, "-c", program,
        *execution_host._host_command(ops, tmp_path, "claude", True)[3:]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"database_url": database_url, "port": 18834, "path": str(path)}


@pytest.mark.parametrize("channel_type", ["telegram", "lark", "lark_bot"])
def test_listener_reattach_revokes_senders_without_stopping_execution(tmp_path, monkeypatch, channel_type):
    from looper.agents.bus import MessageBus
    from looper.agents.channels import lark, lark_bot_settings, lark_cli, registry as registry_module, telegram
    from looper.agents.channels.chat_routing import RoutingSnapshot
    from looper.agents.channels.registry import ChannelRegistry
    from looper.db import channel_configs
    from tests.test_agent_channels import _lark_event, _tg_update

    channel_id = f"channel-{channel_type}-2"
    removed, retained = (("7", "8") if channel_type == "telegram" else
                         ("ou_removed000001", "ou_retained00001"))
    field = "allowed_user_ids" if channel_type == "telegram" else "allowed_open_ids"
    row = {"channel_id": channel_id, "channel_type": channel_type, "enabled": True,
           "manager_id": "manager", "config": {field: [removed, retained], "app_id": "cli_test"}}
    monkeypatch.setattr(channel_configs, "list_configs", AsyncMock(side_effect=lambda: [row]))
    monkeypatch.setattr(channel_configs, "get_bot_token", AsyncMock(return_value="123:synthetic-token"))
    monkeypatch.setattr(channel_configs, "record_rejected", AsyncMock())
    monkeypatch.setattr(registry_module, "load_snapshot", AsyncMock(
        return_value=RoutingSnapshot(channel_id, "manager", False, {})))
    monkeypatch.setattr(lark_cli, "check", AsyncMock(return_value=(None, SimpleNamespace(
        profile="", app_id="cli_test", bot_name="Test", bot_open_id="ou_bot000000001"))))
    monkeypatch.setattr(lark_cli, "identity", AsyncMock(return_value=SimpleNamespace(app_id="cli_test")))
    monkeypatch.setattr(lark_bot_settings, "check", AsyncMock(return_value=(None, SimpleNamespace(
        app_name="Test", open_id="ou_bot000000001"))))
    # External polling is the stand-in; construction, admission, routing, bus
    # delivery and the authenticated listener handshake are the production code.
    async def start(adapter, bus):
        adapter._bus = bus
        adapter._running = True
        adapter._reactions.add = Mock()

    for module, cls in ((telegram, telegram.TelegramChannelAdapter), (lark, lark.LarkChannelAdapter)):
        monkeypatch.setattr(cls, "start", start)
        monkeypatch.setattr(module, "observe_chat", AsyncMock())

    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        registry = ChannelRegistry(bus)
        web = SimpleNamespace(channel_id="channel-web", channel_type="web", healthcheck=lambda: {})
        registry._channels[web.channel_id] = web
        fleet = SimpleNamespace(is_running=lambda _: True, stop_chat=AsyncMock())
        execution = asyncio.create_task(asyncio.Event().wait())
        await serve._register_bots(registry, await serve._enabled_bot_rows(), tmp_path,
                                   fleet.is_running, (channel_type,), stop_chat=fleet.stop_chat)
        original = registry.get(channel_id)

        async def deliver(sender):
            adapter = registry.get(channel_id)
            if channel_type == "telegram":
                await adapter._on_message(_tg_update(int(sender)), None)
            else:
                await adapter._handle_lark_event(_lark_event(sender))

        server = AgentHTTPServer(port=0, runtime_root=tmp_path, web_adapter=web,
            task_router=SimpleNamespace(), supervisor=SimpleNamespace(), managers=fleet,
            channels=registry, proxy_secret="f" * 64)
        await server.start()
        state = {"port": server._port, "secret": "f" * 64}
        frontend = None
        handlers = {}
        try:
            queue = bus.get_queue("manager")
            await deliver(removed)
            assert queue.get_nowait().payload["text"] == "hello"  # Positive control before revocation.
            row["config"] = {**row["config"], field: [retained]}
            with pytest.raises(ConnectionError):
                await execution_host._connect({**state, "secret": "0" * 64}, "attach")
            assert registry.get(channel_id) is original  # An unauthenticated peer cannot reload channels.
            _, control = await execution_host._connect(state)
            control.close()
            await control.wait_closed()
            assert registry.get(channel_id) is original  # Initial startup must not start pollers twice.
            # Run the replacement frontend's real adoption path against the
            # same live execution owner, without launching another agent system.
            ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE, port=0)
            execution_host._private_directory(tmp_path)
            ops_config.write_private(tmp_path / execution_host.STATE, json.dumps({**state,
                "pid": os.getpid(), "started": proc_tree._start_time(os.getpid()),
                "identity": execution_host._identity(ops, tmp_path, "claude")}))
            attached = asyncio.Event()
            connect = execution_host._connect

            async def track_attach(*args):
                streams = await connect(*args)
                attached.set()
                return streams

            monkeypatch.setattr(execution_host, "_connect", track_attach)
            monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler",
                                lambda sig, callback: handlers.setdefault(sig, callback))
            frontend = asyncio.create_task(execution_host.serve_frontend(
                ops=ops, work_dir=tmp_path, claude_bin="claude", skip_backup=True))
            await attached.wait()
            await deliver(removed)
            assert queue.empty(), "revoked sender still reaches the Manager after listener reattachment"
            await deliver(retained)
            assert queue.get_nowait().payload["text"] == "hello"
            assert not original._running
            assert registry.get("channel-web") is web and not execution.done()

            # A storage failure must refuse admission and leave old ingress stopped.
            with monkeypatch.context() as patch:
                patch.setattr(channel_configs, "list_configs", AsyncMock(side_effect=RuntimeError("unavailable")))
                with pytest.raises(ConnectionError, match="rejected"):
                    await execution_host._connect(state, "attach")
            assert registry.get(channel_id) is None
            assert not execution.done()

            # Disabling a channel is configuration too: a retained host must stop it.
            _, control = await execution_host._connect(state, "attach")
            control.close()
            await control.wait_closed()
            assert registry.get(channel_id) is not None
            row["enabled"] = False
            _, control = await execution_host._connect(state, "attach")
            control.close()
            await control.wait_closed()
            assert registry.get(channel_id) is None
            assert not execution.done()
        finally:
            if frontend is not None:
                handlers[signal.SIGTERM]()
                await frontend
            await server.stop()
            await registry.unregister(channel_id)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize(("platform", "pi", "dsh", "persistent"), [
    ("linux", "runner", "cli", True),
    ("linux", "cli", "runner", True),
    ("linux", "cli", "cli", False),
    ("darwin", "runner", "runner", False),
])
def test_default_runner_paths_select_the_persistent_owner(tmp_path, monkeypatch, platform, pi, dsh, persistent):
    monkeypatch.setattr(serve, "sys", SimpleNamespace(platform=platform))
    ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE,
                               pi_runtime=pi, dsh_runtime=dsh)
    assert serve._persistent_runners(ops) is persistent


def test_changed_execution_settings_cannot_run_recovery_beside_a_live_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "sys", SimpleNamespace(platform="linux"))
    ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE)
    (tmp_path / execution_host.STATE.parent).mkdir(mode=0o700)
    state = {"pid": os.getpid(), "started": proc_tree._start_time(os.getpid()), "port": 12345,
             "secret": "f" * 64, "identity": execution_host._identity(ops, tmp_path, "claude")}
    ops_config.write_private(tmp_path / execution_host.STATE, json.dumps(state))
    changed = replace(ops, pi_runtime="cli", dsh_runtime="cli", port=0)
    assert serve._persistent_runners(changed)
    with pytest.raises(serve.StartupCheckFailed, match="different settings"):
        asyncio.run(asyncio.wait_for(execution_host.serve_frontend(ops=changed,
            work_dir=tmp_path, claude_bin="claude", skip_backup=True), 2))
    assert execution_host.read_host(tmp_path)["pid"] == os.getpid()
    # A reused PID or dead owner cannot reserve its endpoint for adoption.
    state["started"] = "different-start-time"
    ops_config.write_private(tmp_path / execution_host.STATE, json.dumps(state))
    assert execution_host.read_host(tmp_path) is None


@pytest.mark.parametrize("peer", ["127.0.0.1", "198.51.100.7"])
def test_authenticated_relay_preserves_peer_and_public_cookie_port(tmp_path, peer):
    async def run():
        (tmp_path / "index.html").write_text("<html>dashboard</html>")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            port = listener.getsockname()[1]
            server = AgentHTTPServer(port=18834, static_dir=tmp_path, runtime_root=tmp_path,
                operator_token="operator-token", web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
                supervisor=SimpleNamespace(), listen_socket=listener, proxy_secret="f" * 64)
            await server.start()
            try:
                reader, writer = await execution_host._connect({"port": port, "secret": "f" * 64}, peer)
                writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:18834\r\n"
                    b"Sec-Fetch-Mode: navigate\r\nSec-Fetch-Dest: document\r\n\r\n")
                await writer.drain()
                reply = await asyncio.wait_for(reader.read(), 2)
                assert (b"looper_operator_18834=operator-token" in reply) is (peer == "127.0.0.1")
                assert b"dashboard" in reply if peer == "127.0.0.1" else b"operator-token" not in reply
                writer.close()
                await writer.wait_closed()
                with pytest.raises(ConnectionError, match="rejected"):
                    await execution_host._connect({"port": port, "secret": "0" * 64}, "127.0.0.1")
                # A host exit closes its watch stream so systemd can restart a
                # failed listener, rather than leaving a healthy-looking proxy.
                watch, control = await execution_host._connect({"port": port, "secret": "f" * 64})
                await server.stop()
                assert await asyncio.wait_for(watch.read(), 1) == b""
                control.close()
                await control.wait_closed()
            finally:
                await server.stop()
    asyncio.run(asyncio.wait_for(run(), 8))
