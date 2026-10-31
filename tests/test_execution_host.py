"""Listener admission and identity at the persistent execution boundary."""
import asyncio
from dataclasses import replace
import json
import os
import socket
from types import SimpleNamespace

import pytest

from looper import ops_config
from looper.agents import execution_host, proc_tree, serve
from looper.agents.http_server import AgentHTTPServer


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
