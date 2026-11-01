"""A keeper's database lease refuses competitors and fences a control plane that loses it."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from db_support import DSN, fresh_schema, until
from looper.agents import control_plane, proc_tree, serve
from looper.db import connection
from looper.ops_config import KeeperMode, OpsConfig

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture
def tmp_path():
    # Real keeper sockets must fit on platforms with a 103-byte Unix socket limit.
    with tempfile.TemporaryDirectory(prefix="lk.lock.", dir="/tmp") as directory:
        yield Path(directory)


def _schema_dsn(schema: str) -> str:
    parts = urlsplit(DSN)
    query = urlencode([*parse_qsl(parts.query), ("search_path", schema)])
    return urlunsplit(parts._replace(query=query))


@contextlib.asynccontextmanager
async def _running(pool: Any, monkeypatch: pytest.MonkeyPatch, root: Path, *, keeper: KeeperMode) -> AsyncIterator[Any]:
    """Real startup and HTTP, with external CLI/login probes kept inside the test boundary."""
    from looper import db
    from looper.agents import managers_api, operator_notes, profile_registry
    from looper.agents.runtimes import codex_login, grok_login

    schema = await pool.fetchval("SELECT current_schema()")
    dsn = _schema_dsn(schema)
    signals: dict[int, Any] = {}
    servers: list[Any] = []
    fences: list[Any] = []
    exits: list[int] = []
    before = asyncio.all_tasks()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: signals.__setitem__(sig, callback))
    monkeypatch.setattr(connection, "_pool", pool)
    monkeypatch.setattr(connection, "_fenced", False)
    monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(operator_notes, "_root", str(root))
    monkeypatch.setattr(managers_api, "_listener", None)
    for name, value in (("_records", None), ("_server", None), ("_recorded", {}), ("_ending", set()),
                        ("_forgetting", set()), ("_spawns_fenced", False), ("_spawning", set()), ("_owned", set())):
        monkeypatch.setattr(proc_tree, name, value)
    monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
    for module in (codex_login, grok_login):
        monkeypatch.setattr(module, "inspect_at_start", AsyncMock(return_value=None))
    check = serve.self_check.Check("test", "Test", serve.self_check.OK, "isolated test")
    for name in ("codex_login_check", "grok_login_check", "ttadk_check"):
        monkeypatch.setattr(serve.self_check, name, AsyncMock(return_value=check))
    monkeypatch.setattr(serve.self_check, "check_claude", lambda _: [])
    monkeypatch.setattr(serve.self_check, "check_lark_cli", lambda _: check)
    monkeypatch.setattr(serve.self_check, "check_git", lambda: check)
    real_start, real_fence = serve.AgentHTTPServer.start, serve.Fence

    async def start(server: Any) -> None:
        await real_start(server)
        servers.append(server)

    def fence(*args: Any, **kwargs: Any) -> Any:
        instance = real_fence(*args, **kwargs)
        fences.append(instance)
        return instance

    monkeypatch.setattr(serve.AgentHTTPServer, "start", start)
    monkeypatch.setattr(serve, "Fence", fence)
    ops = OpsConfig(database_url=dsn, host="127.0.0.1", port=0, keeper_mode=keeper,
                    runtime_root=root, path=root / "ops.toml", operator_notes_root=str(root))
    system = asyncio.create_task(serve._run_agent_system_owned(
        ops=ops, runtime_root=root, work_dir=root, skip_backup=True, exit_now=exits.append))

    def ready() -> bool:
        if system.done():
            system.result()
            raise AssertionError("serve stopped before readiness")
        return bool(servers and signal.SIGTERM in signals)

    try:
        await until(ready, "the full control plane startup", timeout=15)
        yield SimpleNamespace(exits=exits, server=servers[0], system=system, dsn=dsn, signals=signals)
    finally:
        # A real os._exit needs no orderly shutdown; an injected exit leaves tasks to reap here.
        if not exits and not system.done() and signal.SIGTERM in signals:
            signals[signal.SIGTERM]()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(system), 5)
        system.cancel()
        await asyncio.gather(system, return_exceptions=True)
        for instance in fences:
            if instance._backstop is not None:
                instance._backstop.cancel()
            if instance._ending is not None:
                await asyncio.gather(instance._ending, return_exceptions=True)
        for server in servers:
            await server.stop()
        leftovers = asyncio.all_tasks() - before - {asyncio.current_task()}
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)


def test_a_second_control_plane_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import operator_notes
    monkeypatch.setattr(operator_notes, "_root", str(tmp_path))

    async def test() -> None:
        first = await control_plane.ControlPlaneLease.acquire(DSN, on_lost=lambda _: None)
        try:
            pid = first._conn.get_server_pid()
            name = await first._conn.fetchval("SHOW application_name")
            with pytest.raises(control_plane.LockHeld) as refused:
                await control_plane.ControlPlaneLease.acquire(DSN, on_lost=lambda _: None)
            assert refused.value.holder.pid == pid
            assert refused.value.holder.application_name == name
            assert str(pid) in refused.value.holder.describe()
            ops = OpsConfig(database_url=DSN, keeper_mode=KeeperMode.ON, runtime_root=tmp_path,
                            path=tmp_path / "ops.toml", operator_notes_root=str(tmp_path))
            with pytest.raises(serve.StartupCheckFailed, match="another looper serve") as failed:
                await serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path, skip_backup=True)
            assert name in str(failed.value) and str(pid) in str(failed.value)
        finally:
            await first.release()
        second = await control_plane.ControlPlaneLease.acquire(DSN, on_lost=lambda _: None)
        await second.release()
        assert second.state is control_plane.LeaseState.RELEASED

    asyncio.run(asyncio.wait_for(test(), 20))


def test_the_epoch_increments_per_control_plane() -> None:
    async def test() -> None:
        async with fresh_schema() as pool:
            await connection.ensure_schema(pool)
            schema = await pool.fetchval("SELECT current_schema()")
            for expected in (1, 2, 3):
                lease = await control_plane.ControlPlaneLease.acquire(DSN, on_lost=lambda _: None)
                try:
                    await lease._conn.execute(f'SET search_path TO "{schema}"')
                    assert await lease.bump_epoch() == expected
                    assert lease.epoch == expected
                finally:
                    await lease.release()
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", control_plane.EPOCH_KEY) == "3"

    asyncio.run(asyncio.wait_for(test(), 20))


def test_keeper_off_takes_no_lock_and_writes_no_epoch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test() -> None:
        async with fresh_schema() as pool:
            async with _running(pool, monkeypatch, tmp_path, keeper=KeeperMode.OFF) as system:
                assert not await pool.fetchval("""SELECT EXISTS (SELECT 1 FROM pg_locks
                    WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                    AND classid=$1::oid AND objid=$2::oid AND objsubid=2 AND granted)""", *control_plane.LOCK_KEY)
                assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", control_plane.EPOCH_KEY) is None
                assert system.server._server.is_serving()
                assert system.exits == []

    asyncio.run(asyncio.wait_for(test(), 25))


@pytest.mark.parametrize("write_path", ["execute", "acquire", "transaction"])
def test_lock_loss_during_pool_growth_refuses_writes(monkeypatch: pytest.MonkeyPatch, write_path: str) -> None:
    async def test() -> None:
        async with fresh_schema() as driver:
            await driver.execute("CREATE TABLE fence_writes (marker text)")
            schema = await driver.fetchval("SELECT current_schema()")
            monkeypatch.setattr(connection, "_fenced", False)
            monkeypatch.setattr(connection, "_pool", None)
            lease = await control_plane.ControlPlaneLease.acquire(DSN, on_lost=lambda _: connection.fence())
            real_connect = asyncpg.connection.connect
            connecting, resume = asyncio.Event(), asyncio.Event()
            connections = []

            async def connect(*args: Any, **kwargs: Any) -> asyncpg.Connection:
                conn = await real_connect(*args, **kwargs)
                connections.append(conn)
                if len(connections) > 1:
                    # The holder has not received this connection yet, so older
                    # asyncpg versions cannot reach it from Pool.terminate().
                    connecting.set()
                    await resume.wait()
                return conn

            monkeypatch.setattr(asyncpg.connection, "connect", connect)
            writer = None
            pending = None
            try:
                writer = await connection.init_pool(_schema_dsn(schema), min_size=1, max_size=2)
                await writer.execute("INSERT INTO fence_writes VALUES ('before')")
                assert await driver.fetchval("SELECT count(*) FROM fence_writes") == 1

                async def write() -> None:
                    if write_path == "execute":
                        await writer.execute("INSERT INTO fence_writes VALUES ('after')")
                    elif write_path == "acquire":
                        async with writer.acquire() as conn, conn.transaction():
                            await conn.execute("INSERT INTO fence_writes VALUES ('after')")
                    else:
                        async with connection.transaction() as conn:
                            await conn.execute("INSERT INTO fence_writes VALUES ('after')")

                async with writer.acquire():
                    pending = asyncio.create_task(write())
                    await asyncio.wait_for(connecting.wait(), 5)
                    assert await driver.fetchval("SELECT pg_terminate_backend($1)", lease._conn.get_server_pid())
                    await until(lambda: connection._fenced, "the lost-lock fence", timeout=5)
                    assert lease.state is control_plane.LeaseState.LOST
                    resume.set()
                    outcome, = await asyncio.gather(pending, return_exceptions=True)
                    assert await driver.fetchval("SELECT count(*) FROM fence_writes WHERE marker='after'") == 0
                    assert isinstance(outcome, (RuntimeError, asyncpg.InterfaceError)), repr(outcome)
                    assert connections[-1].is_closed(), "a connection completed after fencing must be closed"
            finally:
                resume.set()
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                for conn in connections:
                    conn.terminate()
                if writer is not None:
                    await writer.close()
                await lease.release()

    asyncio.run(asyncio.wait_for(test(), 20))


async def _lose_running_system(pool: Any, monkeypatch: pytest.MonkeyPatch, root: Path, *, restart: bool) -> None:
    # Identify the fixture's schema owner: it drops the schema afterwards and must survive the restart.
    schema = await pool.fetchval("SELECT current_schema()")
    driver_pids = {row["pid"] for row in await pool.fetch("""SELECT pid FROM pg_stat_activity
        WHERE datname=current_database() AND pid<>pg_backend_pid() AND application_name=$1""",
        f"looper test schema {schema}")}
    assert len(driver_pids) == 1, "fresh_schema must have exactly one schema administrator"
    probe = await asyncpg.connect(DSN)
    agent = None
    child_pid = None
    spawning = None
    inflight = []
    created, release = asyncio.Event(), asyncio.Event()
    try:
        # The system's pool already holds a connection: a restart ends it, like every connection the system opens
        # later. Preserve the rest, including the fixture's owner that drops the schema afterwards.
        assert pool.get_size() == 1, "the system's pool must hold a single connection before startup"
        system_pids = {await pool.fetchval("SELECT pg_backend_pid()")}
        before = {row["pid"] for row in await probe.fetch("""SELECT pid FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()""")} - system_pids
        assert driver_pids <= before, "the schema administrator must be preserved during the restart"
        async with _running(pool, monkeypatch, root, keeper=KeeperMode.ON) as system:
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", control_plane.EPOCH_KEY) == "1"
            child_code = ("import os, signal, sys, time\n"
                          "def stop(*_):\n    time.sleep(5)\n    sys.exit(0)\n"
                          "signal.signal(signal.SIGTERM, stop)\n"
                          "open('child.pid', 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
            agent = await proc_tree.spawn(
                sys.executable, "-c", f"import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\ntime.sleep(60)\n", cwd=root)
            await until(lambda: (root / "child.pid").exists() and (root / "child.pid").read_text(),
                        "the slow child's SIGTERM handler")
            child_pid = int((root / "child.pid").read_text())
            assert child_pid in proc_tree._snapshot()
            assert agent.pid in proc_tree._recorded
            real_create = asyncio.create_subprocess_exec

            async def create(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
                process = await real_create(*args, **kwargs)
                if args == ("/bin/sleep", "60"):
                    inflight.append(process)
                    created.set()
                    await release.wait()  # Alive when the lease is lost, not yet returned to spawn.
                return process

            monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
            spawning = asyncio.create_task(proc_tree.spawn("/bin/sleep", "60"))
            await asyncio.wait_for(created.wait(), 5)
            assert inflight[0].pid in proc_tree._snapshot()
            assert inflight[0].pid not in proc_tree._recorded
            pid = await probe.fetchval("""SELECT pid FROM pg_stat_activity
                WHERE datname=current_database() AND application_name LIKE 'looper serve %'""")
            assert pid is not None
            if restart:
                ended = {row["pid"] for row in await probe.fetch("""SELECT pid, pg_terminate_backend(pid) AS ended
                    FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
                    AND NOT (pid=ANY($1::int[]))""", list(before)) if row["ended"]}
                assert system_pids | {pid} <= ended, "a restart must end every connection of the system"
            else:
                assert await probe.fetchval("SELECT pg_terminate_backend($1)", pid)
            await until(lambda: proc_tree._spawns_fenced, "the lost-lock spawn fence", timeout=5)
            assert not spawning.done()
            assert not system.exits
            release.set()
            assert await spawning is inflight[0]
            await until(lambda: bool(system.exits), "the lost-lock exit", timeout=8)
            assert system.exits == [control_plane.EXIT_LOCK_LOST]
            await asyncio.wait_for(agent.wait(), 1)
            assert agent.returncode == -signal.SIGTERM
            await asyncio.wait_for(inflight[0].wait(), 1)
            assert inflight[0].returncode == -signal.SIGTERM
            assert inflight[0].pid not in proc_tree._snapshot()
            with pytest.raises(RuntimeError, match="the control plane lost its lock; no new agents"):
                await proc_tree.spawn("/bin/sleep", "60")
            assert child_pid not in proc_tree._snapshot()
            assert not system.server._server.is_serving()
            with pytest.raises(RuntimeError, match="fenced"):
                connection.get_pool()
            with pytest.raises(asyncpg.InterfaceError, match="closed"):
                async with pool.acquire():
                    pytest.fail("a fenced pool was reused")
            # PostgreSQL accepts fresh connections again, but this control plane stays fenced.
            replacement = await control_plane.ControlPlaneLease.acquire(system.dsn, on_lost=lambda _: None)
            await replacement.release()
            with pytest.raises(RuntimeError, match="fenced"):
                connection.get_pool()
    finally:
        release.set()
        if spawning is not None:
            await asyncio.gather(spawning, return_exceptions=True)
        for process in inflight:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if agent is not None:
            if agent.returncode is None:
                agent.kill()
            await agent.wait()
        if child_pid is None and (root / "child.pid").exists():
            child_pid = int((root / "child.pid").read_text())
        if child_pid is not None and child_pid in proc_tree._snapshot():
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
            await until(lambda: child_pid not in proc_tree._snapshot(), "the test child's cleanup")
        await probe.close()


def test_losing_the_lock_fences_and_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test() -> None:
        async with fresh_schema(max_size=3) as pool:
            await _lose_running_system(pool, monkeypatch, tmp_path, restart=False)
    asyncio.run(asyncio.wait_for(test(), 30))


@pytest.mark.parametrize("extra_idle_connection", [False, True])
def test_a_database_restart_is_a_lost_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_idle_connection: bool,
) -> None:
    async def test() -> None:
        async with fresh_schema(max_size=3) as pool:
            extra = await asyncpg.connect(DSN) if extra_idle_connection else None
            try:
                await _lose_running_system(pool, monkeypatch, tmp_path, restart=True)
                if extra is not None:
                    assert await extra.fetchval("SELECT 1") == 1
            finally:
                if extra is not None:
                    await extra.close()
    asyncio.run(asyncio.wait_for(test(), 30))
