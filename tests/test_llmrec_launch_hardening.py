"""Recording must preserve launch networking and the controller's pinned endpoint."""

import asyncio
from dataclasses import replace
import os
import sys
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.llmrec import LlmrecRoute, STATE_FILE, boot_argv
from looper.agents.profile_registry import builtin_resolved_profile


@pytest.mark.parametrize('lower,upper,expected', [
    (None, 'internal.example,.corp.example', 'internal.example,.corp.example,127.0.0.1,localhost,::1'),
    ('', 'internal.example', '127.0.0.1,localhost,::1'),
    ('lower.example', 'upper.example', 'lower.example,127.0.0.1,localhost,::1'),
    (None, None, '127.0.0.1,localhost,::1'),
    ('', '*', '127.0.0.1,localhost,::1'),
    (None, '*', '*'),
    ('*', 'upper.example', '*'),
    ('localhost,127.0.0.1,localhost,::1', '', 'localhost,127.0.0.1,::1'),
])
def test_recording_preserves_effective_proxy_bypass(monkeypatch, lower, upper, expected):
    for name, value in [('no_proxy', lower), ('NO_PROXY', upper)]:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    launch = Launch(AgentSettings(profile=builtin_resolved_profile()))
    original = launch.environment()
    assert original.get('no_proxy') == lower and original.get('NO_PROXY') == upper
    recorded = replace(launch, llmrec=LlmrecRoute('a' * 22, 'http://127.0.0.1:1/r/' + 'a' * 22,
                                               'https://api.anthropic.com'))
    env = recorded.environment()
    assert env['no_proxy'] == env['NO_PROXY'] == expected
    assert launch.environment() == original


def test_busy_pinned_port_exits_without_replacing_state(tmp_path):
    async def body():
        listener = await asyncio.start_server(lambda reader, writer: writer.close(), '127.0.0.1', 0)
        port = listener.sockets[0].getsockname()[1]
        state = tmp_path / STATE_FILE
        state.parent.mkdir(parents=True)
        original = '{"port": %d, "instance": "existing"}\n' % port
        state.write_text(original)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *boot_argv(sys.executable, os.getcwd(), tmp_path, port),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                await asyncio.wait_for(process.communicate(b'\n'), 3)
            except asyncio.TimeoutError:
                pytest.fail('busy pinned port must fail immediately, rather than serving another port')
            assert process.returncode != 0
            assert state.read_text() == original
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)
            listener.close()
            await listener.wait_closed()

    asyncio.run(asyncio.wait_for(body(), 10))


def test_relative_trust_paths_follow_symlinked_worker_cwd(tmp_path, monkeypatch):
    from looper.agents.llmrec import ROUTES_DIR, RouteFile
    from looper.agents.llmrec.control import ProxyControl

    worker = tmp_path / 'real' / 'worker'
    worker.mkdir(parents=True)
    alias = tmp_path / 'worker-alias'
    alias.symlink_to(worker, target_is_directory=True)
    cert = worker.parent / 'ca.pem'
    cert.write_text('worker trust file')
    cert_dir = worker.parent / 'certs'
    cert_dir.mkdir()
    for name, value in [('NODE_EXTRA_CA_CERTS', '../ca.pem'), ('SSL_CERT_FILE', '../ca.pem'),
                        ('SSL_CERT_DIR', '../certs')]:
        monkeypatch.setenv(name, value)
    owner = ProxyControl(tmp_path / 'runtime', '', enabled=True, include_oauth=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=True))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234})
    launch = Launch(AgentSettings(profile=builtin_resolved_profile()))
    attached = asyncio.run(owner.attach(launch, agent_id='worker', task_id=None, cwd=str(alias)))
    route = RouteFile.read(owner.runtime_root / ROUTES_DIR / f'{attached.llmrec.id}.json')
    for name in ('NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE'):
        assert os.path.samefile(route.tls_env[name], cert)
    assert os.path.samefile(route.tls_env['SSL_CERT_DIR'], cert_dir)


@pytest.mark.parametrize('enabled', [None, 'true'])
def test_standalone_doctor_reads_recording_settings_from_ops_dsn(tmp_path, monkeypatch, enabled):
    import asyncpg
    from looper.agents import self_check
    from looper.agents.llmrec import control
    from looper.db import connection, system_settings
    from looper.ops_config import OpsConfig
    from tests.db_support import DSN, run_db

    async def body(pool):
        if enabled is not None:
            await system_settings.put_settings({'llmrec.enabled': enabled}, pool=pool)
        schema = await pool.fetchval('SELECT current_schema()')
        connect = asyncpg.connect

        async def isolated_connect(dsn, **kwargs):
            assert dsn == DSN
            return await connect(dsn, **kwargs, server_settings={'search_path': schema})

        monkeypatch.setattr(asyncpg, 'connect', isolated_connect)
        monkeypatch.setattr(connection, '_pool', None)
        monkeypatch.setattr(control, '_control', None)
        monkeypatch.setattr(control.ProxyControl, 'status', AsyncMock(return_value={
            'enabled': True, 'proxy': {'state': 'running'}}))
        monkeypatch.setattr(self_check, '_database_facts', AsyncMock(return_value=self_check._DatabaseFacts()))
        monkeypatch.setattr(self_check, 'check_claude', lambda *_: [])
        monkeypatch.setattr(self_check, 'check_codex_login', lambda *_: None)
        monkeypatch.setattr(self_check, 'check_grok_login', lambda *_: None)
        monkeypatch.setattr(self_check, 'check_cursor_login', lambda *_: None)
        monkeypatch.setattr(self_check, 'check_ttadk', AsyncMock(return_value=None))
        monkeypatch.setattr(self_check, 'check_pg_dump', lambda *_: (None, None))
        ops = OpsConfig(runtime_root=tmp_path, path=tmp_path / 'server.toml', database_url=DSN)
        checks = await self_check.run_checks(ops)
        check = next(check for check in checks if check is not None and check.id == 'llmrec')
        assert (check.status, check.detail) == (self_check.OK, 'proxy running; recorded requests use 0.0 MiB of 2048 MiB cap')
        await system_settings.put_settings({'llmrec.enabled': 'false'}, pool=pool)
        checks = await self_check.run_checks(ops)
        check = next(check for check in checks if check is not None and check.id == 'llmrec')
        assert (check.status, check.detail) == (self_check.SKIP, 'off; recorded requests use 0.0 MiB of 2048 MiB cap')

    run_db(body, timeout=15)
