"""Portable config uses the dashboard's export/import implementation and client confirmations."""

import json

import pytest

from looper.agents.bus import MessageBus
from looper.agents import operator_auth
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_config_round_trip_matches_dashboard_and_requires_confirmation(tmp_path, monkeypatch, clients):
    async def run(pool):
        document = {"format": "looper-config/v3", "projects": [{"name": "Portable", "background": "context"}]}
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                                   task_router=server._task_router, runtime_root=tmp_path))
            async def request(path, data=None, *, client=True, session="one", method="POST"):
                return await _send(server, f"{method} {path}",
                                   {**_bearer(CLIENT_TOKEN if client else TOKEN), "X-Looper-Session": session},
                                   "" if data is None else json.dumps(data))

            status, _, old = await request("/api/config/imports", document, client=False)
            assert status == 201
            status, _, preview = await request("/api/v1/config/import_preview", {"document": document})
            assert status == 200 and preview["result"]["plan"] == old["plan"]
            assert await pool.fetchval("SELECT count(*) FROM projects WHERE name='Portable'") == 0
            import_id = preview["result"]["import_id"]
            status, _, refused = await request("/api/v1/config/import_apply", {"import_id": old["import_id"]})
            assert status == 404  # a client's import ids cannot name an operator's staged file
            status, _, asked = await request("/api/v1/config/import_apply", {"import_id": import_id})
            assert status == 202
            assert await pool.fetchval("SELECT count(*) FROM projects WHERE name='Portable'") == 0
            status, _, _ = await request("/api/v1/confirm", {"confirmation_id": asked["confirmation_id"]}, session="two")
            assert status == 404
            status, _, applied = await request("/api/v1/confirm", {"confirmation_id": asked["confirmation_id"]})
            assert status == 200 and applied["result"]["plan"]["applied"]
            assert await pool.fetchval("SELECT background FROM projects WHERE name='Portable'") == "context"
            status, _, old_applied = await request(f"/api/config/imports/{old['import_id']}/apply", client=False)
            assert status == 200 and old_applied["plan"]["applied"]
            status, _, old_export = await request("/api/config/export?projects=1", client=False, method="GET")
            assert status == 200
            status, _, exported = await request("/api/v1/config/export", {"include_projects": True})
            assert status == 200
            new_export = exported["result"]["document"]
            for value in (old_export, new_export):
                value.pop("exported_at")
            assert new_export == old_export
            status, _, again = await request("/api/v1/config/import_preview", {"document": new_export})
            assert status == 200 and again["result"]["plan"]["changes"] == []
            server._config_imports.clear()
    run_db(run, monkeypatch, timeout=30)


def test_cli_commands_against_disposable_serve(tmp_path, monkeypatch, capsys):
    """The real serve wiring supplies live self-check settings and shared imports to loopercli."""
    import io
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path
    from looper import loopercli

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = tmp_path / "runtime"
    home = tmp_path / "home"
    home.mkdir()
    root.mkdir(mode=0o700)
    config = tmp_path / "server.toml"
    config.write_text(f'[database]\nurl = "{DSN}"\n[server]\nhost = "127.0.0.1"\nport = {port}\n')
    config.chmod(0o600)
    # No inherited credentials or runtime CLIs: this path makes no model calls and reads no personal homes.
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "XDG_CONFIG_HOME": str(home / "config")}
    log = tmp_path / "serve.log"
    with log.open("w+") as handle:
        process = subprocess.Popen([sys.executable, "-m", "looper.cli", "serve", "--runtime-root", str(root),
                                    "--config", str(config)], cwd=tmp_path, env=env, stdout=handle, stderr=handle,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 25
            while True:
                assert process.poll() is None, log.read_text()
                if (root / operator_auth.TOKEN_FILE).exists():
                    token = (root / operator_auth.TOKEN_FILE).read_text().strip()
                    api = loopercli.Api(f"http://127.0.0.1:{port}", token)
                    try:
                        if api.call("GET", "/api/v1/whoami")[0] == 200:
                            break
                    except loopercli.Unreachable:
                        pass
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.1)
            status, created = api.call("POST", "/api/clients", {"name": "smoke"})
            assert status == 201
            client = tmp_path / "client.json"
            loopercli.write_client(client, api.url, created["token"])
            base = ["--client-file", str(client), "--json"]
            assert loopercli.main([*base, "doctor"]) == 1  # missing Claude CLI is reported as a failure
            checks = json.loads(capsys.readouterr().out)["result"]["checks"]
            assert any(check["id"] == "pg_dump" for check in checks)
            exported = tmp_path / "exported.json"
            assert loopercli.main([*base, "config", "export", str(exported)]) == 0
            capsys.readouterr()
            assert loopercli.main([*base, "config", "import", str(exported)]) == 0
            staged = json.loads(capsys.readouterr().out)["result"]["import_id"]
            for _ in range(3):
                assert loopercli.main([*base, "config", "import", str(exported)]) == 0
                unused = json.loads(capsys.readouterr().out)["result"]["import_id"]
            assert loopercli.main([*base, "config", "import", str(exported)]) == 1
            assert json.loads(capsys.readouterr().out)["error"]["code"] == "conflict"
            assert loopercli.main([*base, "config", "import", "--cancel", unused]) == 0
            assert json.loads(capsys.readouterr().out)["result"] == {"deleted": unused}
            assert loopercli.main([*base, "config", "import", str(exported)]) == 0
            capsys.readouterr()
            assert loopercli.main([*base, "config", "import", "--apply", staged]) == 3
            confirmation = json.loads(capsys.readouterr().out)["confirmation_id"]
            assert loopercli.main([*base, "confirm", confirmation]) == 0
            capsys.readouterr()
            monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
            monkeypatch.setattr(loopercli.getpass, "getpass", lambda prompt: "smoke-password")
            assert loopercli.main(["auth", "set-password", "--url", api.url]) == 0
            assert loopercli.main(["auth", "clear-password", "--url", api.url,
                                   "--operator-token-file", str(root / operator_auth.TOKEN_FILE)]) == 0
            assert not (root / operator_auth.PASSWORD_FILE).exists()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            # Reap any child left after a startup failure as well.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
