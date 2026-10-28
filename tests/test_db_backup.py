"""Verified backups precede genuine bridge upgrades and restore independently."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from evals.prompt_regression.runner import fresh_database, free_port
from looper import ops_config
from looper.agents import db_backup, operator_auth
from tests.db_support import DSN
from tests.bridge_schema_support import bridge_source as bridge_source, initialize_bridge, witness, run_bridge

REPO_ROOT = Path(__file__).resolve().parents[1]
needs_database = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_PG_DUMP = r'''#!@PYTHON@
import asyncio, json, os, sys
args = sys.argv[1:]
with open(@CALLS@, "a") as calls:
    calls.write(json.dumps({"argv": args, "PGPASSWORD": os.environ.get("PGPASSWORD")}) + "\n")
if args == ["--version"]:
    print("pg_dump (PostgreSQL) @VERSION@")
    sys.exit(0)
if @MODE@ == "exit":
    print("pg_dump: error: connection to server failed", file=sys.stderr)
    sys.exit(1)
if @MODE@ == "empty":
    sys.exit(0)
if @MODE@ == "offline":
    sys.stdout.buffer.write(b"PGDMP offline")
    sys.exit(0)
import asyncpg
async def bridge_state():
    conn = await asyncpg.connect(args[-1].split("=", 1)[1], password=os.environ.get("PGPASSWORD"))
    try:
        return {"version": await conn.fetchval("SELECT version FROM registry_schema_version"),
                "config_type": await conn.fetchval("SELECT jsonb_typeof(config) FROM tasks WHERE id='task-bridge'"),
                "old_key": await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conrelid = 'task_user_gate_posts'::regclass AND contype = 'u' "
                    "AND pg_get_constraintdef(oid) = 'UNIQUE (channel, chat, external_id)')")}
    finally:
        await conn.close()
sys.stdout.buffer.write(b"PGDMP" + json.dumps(asyncio.run(bridge_state())).encode())
'''

_PG_RESTORE = r'''#!/bin/sh
echo "$@" >> @CALLS@
if [ "@OK@" = yes ]; then
    echo '1; 1259 100 TABLE public role_configs test'
    echo '2; 0 100 TABLE DATA public role_configs test'
    exit 0
fi
echo "pg_restore: error: input file does not appear to be a valid archive" >&2
exit 1
'''


def _install(bin_dir: Path, *, version: str | None = "99.1", mode: str = "dump", restore_ok: bool | None = True) -> Path:
    """A stand-in pg_dump (none when ``version`` is None) and pg_restore beside it; the file both log calls to."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir.parent / "calls.jsonl"
    if version is not None:
        script = (_PG_DUMP.replace("@PYTHON@", sys.executable).replace("@CALLS@", repr(str(calls)))
                  .replace("@VERSION@", version).replace("@MODE@", repr(mode)))
        (bin_dir / "pg_dump").write_text(script)
        (bin_dir / "pg_dump").chmod(0o755)
    if restore_ok is not None:
        restore = _PG_RESTORE.replace("@CALLS@", str(bin_dir.parent / "restore-calls")).replace(
            "@OK@", "yes" if restore_ok else "no")
        (bin_dir / "pg_restore").write_text(restore)
        (bin_dir / "pg_restore").chmod(0o755)
    return calls


def _env(**extra: str) -> dict[str, str]:
    dropped = {"LOOPER_PG_DUMP", "LOOPER_BACKUP_CMD", "LOOPER_BACKUP_VERIFY_CMD", "LOOPER_RUNTIME_ROOT"}
    env = {key: value for key, value in os.environ.items()
           if key not in dropped and key.replace("LOOPX_", "LOOPER_", 1) not in dropped}
    return {**env, **extra}


def _serve(tmp_path: Path, database_url: str, *flags: str, backup: dict | None = None, **extra_env: str) -> tuple[int | None, str]:
    """Start ``looper serve`` with PATH = tmp_path/bin: its exit code, or None when it came up (then stopped); its log."""
    port = free_port()
    for name in ("runtime", "work", "home"):
        (tmp_path / name).mkdir(exist_ok=True)
    log_path = tmp_path / "serve.log"
    body = f"[database]\nurl = {json.dumps(database_url)}\n[backup]\n"
    body += "\n".join(f"{key} = {json.dumps(value)}" for key, value in (backup or {}).items())
    ops_config.write_private(tmp_path / "runtime" / ops_config.FILE, body)
    env = _env(PATH=str(tmp_path / "bin"), HOME=str(tmp_path / "home"),
               PYTHONPATH=str(REPO_ROOT), **extra_env)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "looper.cli", "serve", "--port", str(port),
             "--runtime-root", str(tmp_path / "runtime"), *flags],
            cwd=tmp_path / "work", env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 90
            while process.poll() is None:
                try:
                    token = operator_auth.read_token(tmp_path / "runtime")
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/system/status", data=b"{}",
                                                     headers={"Content-Type": "application/json",
                                                              **({"Authorization": f"Bearer {token}"} if token else {})})
                    urllib.request.urlopen(request, timeout=2).read()
                    break
                except OSError:
                    assert time.monotonic() < deadline, "looper serve did not answer within 90 s"
                    time.sleep(0.25)
            code = process.poll()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return code, log_path.read_text()


def _db(url: str, *statements: str) -> None:
    async def run() -> None:
        import asyncpg

        from looper.db.connection import ensure_schema

        conn = await asyncpg.connect(url)
        try:
            await ensure_schema(conn)
            for statement in statements:
                await conn.execute(statement)
        finally:
            await conn.close()

    asyncio.run(run())


def _bridge(url: str, source: Path) -> dict:
    async def prepare():
        import asyncpg
        conn = await asyncpg.connect(url)
        try:
            await initialize_bridge(conn, source, url=url)
            return await witness(conn)
        finally:
            await conn.close()
    return asyncio.run(prepare())


def _state(url: str, *, rows: bool = False) -> dict[str, object]:
    async def read() -> dict[str, object]:
        import asyncpg
        from looper.db.connection import SCHEMA_PATH, schema_fingerprint
        conn = await asyncpg.connect(url)
        try:
            if rows:
                return await witness(conn)
            return {
                "version": await conn.fetchval("SELECT version FROM registry_schema_version"),
                "old_key": await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conrelid = 'task_user_gate_posts'::regclass AND contype = 'u' "
                    "AND pg_get_constraintdef(oid) = 'UNIQUE (channel, chat, external_id)')"),
                "current": await conn.fetchval("SELECT sha256 FROM schema_fingerprint")
                == schema_fingerprint(SCHEMA_PATH.read_text(encoding="utf-8")),
            }
        finally:
            await conn.close()
    return asyncio.run(read())


MIGRATED = {"version": 4, "old_key": False, "current": True}
UNTOUCHED = {"version": 3, "old_key": True, "current": False}


def _backups(tmp_path: Path) -> list[Path]:
    backups = tmp_path / "runtime" / "backups"
    return sorted(backups.iterdir()) if backups.is_dir() else []


@needs_database
def test_a_schema_change_is_backed_up_before_it_is_applied(tmp_path: Path, bridge_source: Path) -> None:
    _install(tmp_path / "bin")

    with fresh_database(str(DSN)) as (url, _):
        _bridge(url, bridge_source)
        code, log = _serve(tmp_path, url)
        state = _state(url)

    assert code is None, log
    [backup] = _backups(tmp_path)
    assert backup.read_bytes() == b"PGDMP" + json.dumps({"version": 3, "config_type": "string", "old_key": True}).encode()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert state == MIGRATED
    listed = (tmp_path / "restore-calls").read_text().split()  # before it took its final name
    assert listed[0] == "--list" and listed[1].startswith(str(backup))
    assert "version=3 target_version=4 phase=backup" in log
    assert "phase=backup_verified" in log
    assert f"pg_restore --no-owner --dbname '<isolated-database-url>' '{backup}'" in log
    assert "preserving the migrated database and new writes" in log


@needs_database
def test_a_real_bridge_dump_restores_all_rows_for_the_bridge_binary(tmp_path, bridge_source):
    dump, restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    if not dump or not restore:
        pytest.skip("actual pg_dump and pg_restore are required")
    with fresh_database(str(DSN)) as (url, _), fresh_database(str(DSN)) as (restored, _):
        before = _bridge(url, bridge_source)
        archive, _ = db_backup.back_up(db_backup.Dumper((dump,), False, "test"),
                                       url, tmp_path / "backups")
        _db(url)
        assert _state(url) == MIGRATED
        bare, password, _, _ = db_backup._connection(restored)
        subprocess.run([restore, "--exit-on-error", "--no-owner", "--dbname", bare, str(archive)],
                       env={**os.environ, **({"PGPASSWORD": password} if password else {})},
                       capture_output=True, check=True, timeout=45)
        assert _state(restored, rows=True) == before
        asyncio.run(run_bridge(bridge_source, restored))
        assert _state(restored)["version"] == 3


@needs_database
@pytest.mark.parametrize("old", [False, True], ids=["new-database", "same-schema"])
def test_no_backup_without_a_schema_change(tmp_path: Path, old: bool) -> None:
    calls = _install(tmp_path / "bin")

    with fresh_database(str(DSN)) as (url, _):
        if old:
            _db(url)
        code, log = _serve(tmp_path, url)

    assert code is None, log
    assert not calls.exists() and _backups(tmp_path) == []


@needs_database
@pytest.mark.parametrize(
    ("version", "reason"),
    [(None, "`pg_dump` is not on PATH"), ("9.6.24", "pg_dump (PostgreSQL) 9.6.24")],
    ids=["missing", "older-than-the-server"],
)
def test_without_a_usable_pg_dump_serve_refuses(tmp_path: Path, bridge_source: Path, version: str | None, reason: str) -> None:
    _install(tmp_path / "bin", version=version)

    with fresh_database(str(DSN)) as (url, _):
        before = _bridge(url, bridge_source)
        code, log = _serve(tmp_path, url)
        state = _state(url)
        assert _state(url, rows=True) == before

    assert code == 1, log
    assert state == UNTOUCHED
    fail = next(line for line in log.splitlines() if line.startswith("fail  pg_dump"))
    assert reason in fail
    install = "brew install libpq" if sys.platform == "darwin" else "apt install postgresql-client-"
    assert install in log and 'command = ["docker", "exec"' in log and "looper serve --skip-backup" in log
    assert _backups(tmp_path) == []


@needs_database
def test_unsupported_database_is_refused_before_backup(tmp_path, bridge_source):
    calls = _install(tmp_path / "bin")
    with fresh_database(str(DSN)) as (url, _):
        _bridge(url, bridge_source)
        async def invalidate():
            import asyncpg
            conn = await asyncpg.connect(url)
            try:
                await conn.execute("UPDATE schema_fingerprint SET sha256 = 'unsupported'")
            finally:
                await conn.close()
        asyncio.run(invalidate())
        before = _state(url, rows=True)
        code, log = _serve(tmp_path, url)
        assert _state(url, rows=True) == before
    assert code == 1 and "v2.13.165" in log
    assert not calls.exists() and _backups(tmp_path) == []


@needs_database
def test_skip_backup_cannot_bypass_the_bridge_upgrade(tmp_path: Path, bridge_source: Path) -> None:
    _install(tmp_path / "bin", version=None)
    with fresh_database(str(DSN)) as (url, _):
        _bridge(url, bridge_source)
        code, log = _serve(tmp_path, url, "--skip-backup")
        assert _state(url) == UNTOUCHED
    assert code == 1 and "--skip-backup is not allowed" in log
    assert _backups(tmp_path) == []


@needs_database
def test_skip_backup_for_a_current_schema_change_warns(tmp_path, monkeypatch):
    from looper.agents.serve import _back_up_before_schema_change
    from looper.db import connection
    from tests.db_support import fresh_schema

    async def pending(conn):
        return True

    async def run():
        async with fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "schema_change_pending", pending)
            await _back_up_before_schema_change(pool, _ops(tmp_path), tmp_path, skip_backup=True)

    with patch('looper.agents.serve.logger.warning') as warning:
        asyncio.run(run())
    assert "BACKUP SKIPPED (--skip-backup)" in warning.call_args.args[0]


@needs_database
@pytest.mark.parametrize(
    ("mode", "restore_ok", "reason"),
    [
        ("exit", True, "connection to server failed"),
        ("empty", True, "the dump is empty"),
        ("dump", False, "does not appear to be a valid archive"),
    ],
    ids=["dump-fails", "empty-dump", "unreadable-dump"],
)
def test_a_failed_backup_stops_serve_before_any_ddl(tmp_path: Path, bridge_source: Path, mode: str, restore_ok: bool, reason: str) -> None:
    _install(tmp_path / "bin", mode=mode, restore_ok=restore_ok)

    with fresh_database(str(DSN)) as (url, _):
        before = _bridge(url, bridge_source)
        code, log = _serve(tmp_path, url)
        state = _state(url)
        assert _state(url, rows=True) == before

    assert code == 1, log
    assert state == UNTOUCHED
    assert "backup verification failed" in log and reason in log
    assert _backups(tmp_path) == []  # nor a partial file


@pytest.mark.parametrize("command", [False, True], ids=["pg-dump", "backup-command"])
@pytest.mark.parametrize("restore_ok", [None, False], ids=["missing-verifier", "unreadable-archive"])
def test_header_alone_cannot_authorize_a_schema_change(tmp_path, monkeypatch, command, restore_ok):
    _install(tmp_path / "bin", mode="offline", restore_ok=restore_ok)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    dumper = db_backup.Dumper((str(tmp_path / "bin" / "pg_dump"),), command, "test")
    with pytest.raises(db_backup.BackupFailed):
        db_backup.back_up(dumper, "postgresql://u@127.0.0.1:1/loopx", tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []


@pytest.mark.parametrize("toc", ["", "1; 1259 100 TABLE public t test", "2; 0 100 TABLE DATA public t test"],
                         ids=["empty-archive", "schema-only", "data-only"])
def test_a_full_backup_requires_both_table_definitions_and_data(tmp_path, toc):
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"PGDMP")
    restore = tmp_path / "pg_restore"
    restore.write_text(f"#!{sys.executable}\nprint({toc!r})\n")
    restore.chmod(0o755)
    with pytest.raises(db_backup.BackupFailed, match="full database backup"):
        db_backup._verify(dump, str(restore), password=None)


def test_the_newest_five_backups_of_the_database_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path / "bin", mode="offline")
    backups = tmp_path / "backups"
    backups.mkdir()
    old = [backups / f"looper-loopx-2026100{day}T120000.000000Z.dump" for day in range(1, 7)]
    # Another database's backups, and those named before the rename (loopx-<database>-...), are left alone.
    others = [backups / "looper-loopx_other-20261001T120000.000000Z.dump",
              backups / "loopx-loopx-20261001T120000.000000Z.dump", backups / "notes.txt"]
    for path in old + others:
        path.write_bytes(b"PGDMP")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    new, _ = db_backup.back_up(db_backup.find_dumper(16), "postgresql://u@127.0.0.1:1/loopx", backups)

    assert sorted(backups.iterdir()) == sorted([*old[-4:], new, *others])


def test_the_password_reaches_pg_dump_only_through_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(tmp_path / "bin", mode="offline")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    db_backup.back_up(db_backup.find_dumper(16), "postgresql://u:p%40ss@127.0.0.1:1/loopx", tmp_path / "backups")

    dump = json.loads(calls.read_text().splitlines()[-1])
    assert dump == {"argv": ["--format=custom", "--no-password", "--dbname=postgresql://u@127.0.0.1:1/loopx"],
                    "PGPASSWORD": "p@ss"}


def test_a_backup_command_runs_as_given_with_the_database_filled_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``backup.command`` wins over any pg_dump: its version is not asked, its stdout is the backup."""
    calls = _install(tmp_path / "bin", version="9.6.24", mode="offline")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    ops = _ops(tmp_path, command=[str(tmp_path / "bin" / "pg_dump"), "--format=custom", "-U", "{user}", "{dbname}"])

    dumper = db_backup.find_dumper(16, ops)
    path, checked = db_backup.back_up(dumper, "postgresql://alice:pw@127.0.0.1:1/loopx", tmp_path / "backups")

    assert [json.loads(line)["argv"] for line in calls.read_text().splitlines()] == [
        ["--format=custom", "-U", "alice", "loopx"]]
    assert path.read_bytes() == b"PGDMP offline" and "listed" in checked


def test_a_backup_command_is_shown_by_its_program_only(tmp_path: Path) -> None:
    """The command's arguments can hold a password: the detail the startup log and ``looper doctor`` show names the
    source and the program, never the arguments."""
    from looper.agents import self_check

    ops = _ops(tmp_path, command=["docker", "exec", "-e", "PGPASSWORD=Sekr1t-pw", "pg", "pg_dump",
                                  "--format=custom", "{dbname}"])

    dumper = db_backup.find_dumper(16, ops)
    check, _ = self_check.check_pg_dump(160004, True, ops)

    assert dumper.detail == f"backup.command in {ops.path}: docker … (version not checked)"
    assert "Sekr1t-pw" not in self_check.format_checks([check]) and "PGPASSWORD" not in check.detail


# --- Docker: the dump is made, and listed, inside the container -----------------------

_DOCKER = r'''#!@PYTHON@
import json, sys
args = sys.argv[1:]
record = {"argv": args}
if "pg_dump" in args:
    sys.stdout.buffer.write(b"PGDMP docker")
elif "pg_restore" in args:
    record["stdin"] = sys.stdin.buffer.read().decode("latin-1")
    mode = @MODE@
    if mode == "ok":
        print("1; 1259 100 TABLE public t test")
        print("2; 0 100 TABLE DATA public t test")
    elif mode == "fail":
        print("pg_restore: error: input file is too short", file=sys.stderr)
        sys.exit(1)
else:
    sys.exit(2)
with open(@CALLS@, "a") as log:
    log.write(json.dumps(record) + "\n")
'''

DOCKER_DUMP = ["docker", "exec", "pgc", "pg_dump", "--format=custom", "-U", "{user}", "{dbname}"]
DOCKER_VERIFY = ("docker", "exec", "-i", "pgc", "pg_restore", "--list")


def _install_docker(bin_dir: Path, *, listing: str = "ok") -> Path:
    """A stand-in ``docker`` (never the real one): ``exec ... pg_dump`` writes an archive header, ``exec ... pg_restore``
    lists what it reads on stdin (``listing``: ok, fail or empty). The file it logs each call to."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir.parent / "docker-calls.jsonl"
    script = (_DOCKER.replace("@PYTHON@", sys.executable).replace("@MODE@", repr(listing))
              .replace("@CALLS@", repr(str(calls))))
    (bin_dir / "docker").write_text(script)
    (bin_dir / "docker").chmod(0o755)
    return calls


def _docker_calls(calls: Path) -> list[dict]:
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def _ops(tmp_path: Path, **backup) -> ops_config.OpsConfig:
    return _ops_file(tmp_path, backup)


def _ops_file(tmp_path: Path, backup: dict) -> ops_config.OpsConfig:
    path = tmp_path / "runtime" / ops_config.FILE
    body = "\n".join(f"{key} = {json.dumps(value)}" for key, value in backup.items())
    ops_config.write_private(path, f'[database]\nurl = "postgresql://u@127.0.0.1:1/loopx"\n[backup]\n{body}\n')
    return ops_config.load(ops_config.Flags(runtime_root=tmp_path / "runtime"))


@pytest.mark.parametrize("command", [
    DOCKER_DUMP,
    ["docker", "exec", "-e", "PGPASSWORD=pw", "-u", "postgres", "pgc", "pg_dump", "--format=custom"],
    ["/usr/local/bin/docker", "exec", "--env", "A=b", "--env=C=d", "-eE=f", "--user=postgres", "-w", "/tmp", "pgc",
     "pg_dump"],
    ["docker", "exec", "-it", "-i", "--privileged", "pgc", "/usr/bin/pg_dump"],
    ["docker", "exec", "-t=false", "-i=false", "-it=true", "pgc", "pg_dump"],
], ids=["plain", "short-options", "long-options", "flags", "short-booleans-with-values"])
def test_the_verifier_is_derived_from_a_docker_exec_pg_dump_in_the_ops_file(tmp_path, command):
    ops = _ops_file(tmp_path, {"command": command})

    dumper = db_backup.find_dumper(16, ops)

    assert dumper.verify == (command[0], "exec", "-i", "pgc", "pg_restore", "--list")
    assert dumper.verifier == "docker exec -i pgc pg_restore --list"


def test_the_derived_verifier_does_not_retain_dump_credentials(tmp_path):
    ops = _ops(tmp_path, command=["docker", "exec", "-e", "PGPASSWORD=Sekr1t-pw", "--env", "A=b", "pgc",
                                  "pg_dump", "--format=custom", "-U", "{user}", "{dbname}"])

    dumper = db_backup.find_dumper(16, ops)

    assert dumper.verify == DOCKER_VERIFY
    assert "Sekr1t-pw" not in dumper.detail + dumper.verifier


@pytest.mark.parametrize("command", [
    ["pg_dump", "--format=custom", "{dbname}"],
    ["docker", "run", "--rm", "pgc", "pg_dump"],
    ["docker", "exec", "pgc", "psql", "-c", "select 1"],
    ["docker", "exec", "--no-such-option", "pgc", "pg_dump"],
    ["docker", "-H", "tcp://h", "exec", "pgc", "pg_dump"],
    ["docker", "exec", "pgc"],
    ["docker", "exec", "-e"],
    ["sudo", "docker", "exec", "pgc", "pg_dump"],
], ids=["local", "run", "other-program", "unknown-option", "global-option", "no-program", "dangling", "sudo"])
def test_only_the_exact_docker_exec_form_is_derived(tmp_path, command):
    assert db_backup.find_dumper(16, _ops_file(tmp_path, {"command": command})).verify == ()


def test_a_docker_backup_is_dumped_and_listed_inside_the_container(tmp_path, monkeypatch):
    calls = _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    dumper = db_backup.find_dumper(16, _ops_file(tmp_path, {"command": DOCKER_DUMP}))

    path, checked = db_backup.back_up(dumper, "postgresql://alice@127.0.0.1:1/loopx", tmp_path / "backups")

    assert checked == "listed with docker exec -i pgc pg_restore --list"
    dump, listing = _docker_calls(calls)
    assert dump["argv"] == ["exec", "pgc", "pg_dump", "--format=custom", "-U", "alice", "loopx"]
    assert listing == {"argv": ["exec", "-i", "pgc", "pg_restore", "--list"], "stdin": "PGDMP docker"}
    assert path.read_bytes() == b"PGDMP docker"


def test_the_derived_verifier_expands_placeholders_in_the_container(tmp_path, monkeypatch):
    calls = _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    command = ["docker", "exec", "{dbname}", "pg_dump", "--format=custom", "-U", "{user}", "{dbname}"]
    dumper = db_backup.find_dumper(16, _ops_file(tmp_path, {"command": command}))

    _, checked = db_backup.back_up(dumper, "postgresql://alice@127.0.0.1:1/loopx", tmp_path / "backups")

    assert checked == "listed with docker exec -i loopx pg_restore --list"
    _, listing = _docker_calls(calls)
    assert listing["argv"] == ["exec", "-i", "loopx", "pg_restore", "--list"]


def test_an_explicit_verify_command_wins_over_the_derived_one(tmp_path, monkeypatch):
    calls = _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    verify = ["docker", "exec", "-i", "other", "pg_restore", "--list"]
    ops = _ops_file(tmp_path, {"command": DOCKER_DUMP, "verify_command": verify})
    dumper = db_backup.find_dumper(16, ops)

    _, checked = db_backup.back_up(dumper, "postgresql://alice@127.0.0.1:1/loopx", tmp_path / "backups")

    assert dumper.verify == tuple(verify)
    assert checked == "listed with docker … (backup.verify_command)"
    assert _docker_calls(calls)[-1]["argv"] == verify[1:]
    assert _docker_calls(calls)[-1]["stdin"] == "PGDMP docker"


def test_an_explicit_verify_command_serves_a_local_pg_dump_without_pg_restore(tmp_path, monkeypatch):
    _install(tmp_path / "bin", mode="offline", restore_ok=None)
    calls = _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    ops = _ops(tmp_path, verify_command=list(DOCKER_VERIFY))

    dumper = db_backup.find_dumper(16, ops)
    _, checked = db_backup.back_up(dumper, "postgresql://u@127.0.0.1:1/loopx", tmp_path / "backups")

    assert checked == "listed with docker … (backup.verify_command)"
    assert _docker_calls(calls)[-1]["argv"] == ["exec", "-i", "pgc", "pg_restore", "--list"]
    assert not ops.warnings


def test_a_verify_command_timeout_does_not_leak_its_argv(tmp_path, monkeypatch):
    _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    secret = "Sekr1t-pw"
    verify = ["docker", "exec", "-e", f"PGPASSWORD={secret}", "-i", "pgc", "pg_restore", "--list"]
    dumper = db_backup.find_dumper(16, _ops_file(tmp_path, {"command": DOCKER_DUMP, "verify_command": verify}))
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if secret in " ".join(argv):
            raise subprocess.TimeoutExpired(argv, 5)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(db_backup.subprocess, "run", run)

    with pytest.raises(db_backup.BackupFailed, match="did not finish: timed out after 5s") as failure:
        db_backup.back_up(dumper, "postgresql://alice@127.0.0.1:1/loopx", tmp_path / "backups")

    assert secret not in str(failure.value)
    assert "backup.verify_command" in str(failure.value)


@pytest.mark.parametrize(("listing", "reason"), [
    ("fail", "cannot read the dump: pg_restore: error: input file is too short"),
    ("empty", "not a full database backup"),
], ids=["exits-non-zero", "lists-nothing"])
def test_a_verifier_that_fails_or_lists_nothing_fails_the_backup(tmp_path, monkeypatch, listing, reason):
    _install_docker(tmp_path / "bin", listing=listing)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    dumper = db_backup.find_dumper(16, _ops_file(tmp_path, {"command": DOCKER_DUMP}))

    with pytest.raises(db_backup.BackupFailed, match=reason):
        db_backup.back_up(dumper, "postgresql://alice@127.0.0.1:1/loopx", tmp_path / "backups")

    assert list((tmp_path / "backups").iterdir()) == []


def test_without_any_verifier_the_backup_fails_naming_verify_command(tmp_path, monkeypatch):
    _install(tmp_path / "bin", mode="offline", restore_ok=None)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    dumper = db_backup.find_dumper(16, _ops(tmp_path, command=[str(tmp_path / "bin" / "pg_dump"), "{dbname}"]))

    with pytest.raises(db_backup.BackupFailed, match="backup.verify_command") as failure:
        db_backup.back_up(dumper, "postgresql://u@127.0.0.1:1/loopx", tmp_path / "backups")

    assert '["docker", "exec", "-i", "<container>", "pg_restore", "--list"]' in str(failure.value)
    assert list((tmp_path / "backups").iterdir()) == []


def test_the_self_check_reports_the_verifier_and_fails_without_one(tmp_path, monkeypatch):
    from looper.agents import self_check

    _install(tmp_path / "bin", mode="offline", restore_ok=None)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    check, dumper = self_check.check_pg_dump(160004, True, _ops_file(tmp_path, {"command": DOCKER_DUMP}))
    assert dumper is not None and check.status == self_check.OK
    assert check.detail.endswith("backup verified by docker exec -i pgc pg_restore --list")

    check, dumper = self_check.check_pg_dump(160004, True, _ops(tmp_path, pg_dump=str(tmp_path / "bin" / "pg_dump")))
    assert dumper is None and check.status == self_check.FAIL
    assert "backup.verify_command" in check.detail
