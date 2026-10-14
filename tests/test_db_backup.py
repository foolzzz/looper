"""``looper serve`` backs the database up with pg_dump before it changes the schema.

The serve cases run the real ``looper serve`` (``python -m looper.cli serve``)
with PATH holding only a stand-in ``pg_dump`` (and ``pg_restore``), against a
database created beside LOOPER_TEST_DATABASE_URL. The stand-in writes a
custom-format header followed by what it read from the database: whether the
column that a migration in schema.sql drops was still there.

A database "about to change" is one set up by ``ensure_schema`` whose recorded
fingerprint is then replaced, with that dropped column added back.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from evals.prompt_regression.runner import fresh_database, free_port
from looper.agents import db_backup
from looper.agents.self_check import mask_dsn
from looper.env import counterpart

REPO_ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
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
async def dropped_column():
    conn = await asyncpg.connect(args[-1].split("=", 1)[1], password=os.environ.get("PGPASSWORD"))
    try:
        return await conn.fetchval("SELECT count(*) FROM information_schema.columns "
                                   "WHERE table_name = 'role_configs' AND column_name = 'permission_mode'")
    finally:
        await conn.close()
sys.stdout.buffer.write(b"PGDMP" + json.dumps({"permission_mode": asyncio.run(dropped_column())}).encode())
'''

_PG_RESTORE = r'''#!/bin/sh
echo "$@" >> @CALLS@
[ "@OK@" = yes ] && exit 0
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
    dropped = {db_backup.PG_DUMP_ENV, db_backup.BACKUP_CMD_ENV, "LOOPER_RUNTIME_ROOT"}
    env = {key: value for key, value in os.environ.items() if key not in dropped and counterpart(key) not in dropped}
    return {**env, **extra}


def _serve(tmp_path: Path, database_url: str, *flags: str) -> tuple[int | None, str]:
    """Start ``looper serve`` with PATH = tmp_path/bin: its exit code, or None when it came up (then stopped); its log."""
    port = free_port()
    for name in ("runtime", "work", "home"):
        (tmp_path / name).mkdir(exist_ok=True)
    log_path = tmp_path / "serve.log"
    env = _env(LOOPER_DATABASE_URL=database_url, PATH=str(tmp_path / "bin"), HOME=str(tmp_path / "home"),
               PYTHONPATH=str(REPO_ROOT))
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
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2).read()
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


_OLD_SCHEMA = ("UPDATE schema_fingerprint SET sha256 = 'an older schema.sql'",
               "ALTER TABLE role_configs ADD COLUMN permission_mode TEXT")


def _state(url: str) -> dict[str, object]:
    """Whether the dropped column is there, and whether the fingerprint is the current schema.sql's."""
    async def read() -> dict[str, object]:
        import asyncpg

        from looper.db.connection import SCHEMA_PATH, schema_fingerprint

        conn = await asyncpg.connect(url)
        try:
            return {
                "permission_mode": await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'role_configs' AND column_name = 'permission_mode'"),
                "current": await conn.fetchval("SELECT sha256 FROM schema_fingerprint")
                == schema_fingerprint(SCHEMA_PATH.read_text(encoding="utf-8")),
            }
        finally:
            await conn.close()

    return asyncio.run(read())


MIGRATED = {"permission_mode": 0, "current": True}
UNTOUCHED = {"permission_mode": 1, "current": False}


def _backups(tmp_path: Path) -> list[Path]:
    backups = tmp_path / "runtime" / "backups"
    return sorted(backups.iterdir()) if backups.is_dir() else []


@needs_database
def test_a_schema_change_is_backed_up_before_it_is_applied(tmp_path: Path) -> None:
    _install(tmp_path / "bin")

    with fresh_database(str(DSN)) as (url, _):
        _db(url, *_OLD_SCHEMA)
        code, log = _serve(tmp_path, url)
        state = _state(url)

    assert code is None, log
    [backup] = _backups(tmp_path)
    # The dump was taken while the column the migration drops was still there.
    assert backup.read_bytes() == b"PGDMP" + json.dumps({"permission_mode": 1}).encode()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert state == MIGRATED
    listed = (tmp_path / "restore-calls").read_text().split()  # before it took its final name
    assert listed[0] == "--list" and listed[1].startswith(str(backup))
    assert f"pg_restore --no-owner --dbname '{mask_dsn(url)}' '{backup}'" in log


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
def test_without_a_usable_pg_dump_serve_refuses(tmp_path: Path, version: str | None, reason: str) -> None:
    _install(tmp_path / "bin", version=version)

    with fresh_database(str(DSN)) as (url, _):
        _db(url, *_OLD_SCHEMA)
        code, log = _serve(tmp_path, url)
        state = _state(url)

    assert code == 1, log
    assert state == UNTOUCHED
    fail = next(line for line in log.splitlines() if line.startswith("fail  pg_dump"))
    assert reason in fail
    install = "brew install libpq" if sys.platform == "darwin" else "apt install postgresql-client-"
    assert install in log and "LOOPER_BACKUP_CMD='docker exec" in log and "looper serve --skip-backup" in log
    assert _backups(tmp_path) == []


@needs_database
def test_skip_backup_changes_the_schema_with_a_warning(tmp_path: Path) -> None:
    _install(tmp_path / "bin", version=None)

    with fresh_database(str(DSN)) as (url, _):
        _db(url, *_OLD_SCHEMA)
        code, log = _serve(tmp_path, url, "--skip-backup")
        state = _state(url)

    assert code is None, log
    assert state == MIGRATED
    assert "WARNING BACKUP SKIPPED (--skip-backup)" in log
    assert _backups(tmp_path) == []


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
def test_a_failed_backup_stops_serve_before_any_ddl(tmp_path: Path, mode: str, restore_ok: bool, reason: str) -> None:
    _install(tmp_path / "bin", mode=mode, restore_ok=restore_ok)

    with fresh_database(str(DSN)) as (url, _):
        _db(url, *_OLD_SCHEMA)
        code, log = _serve(tmp_path, url)
        state = _state(url)

    assert code == 1, log
    assert state == UNTOUCHED
    assert reason in next(line for line in log.splitlines() if line.startswith("fail  pg_dump backup"))
    assert _backups(tmp_path) == []  # nor a partial file


def test_the_newest_five_backups_of_the_database_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path / "bin", mode="offline", restore_ok=None)
    backups = tmp_path / "backups"
    backups.mkdir()
    old = [backups / f"looper-loopx-2026100{day}T120000.000000Z.dump" for day in range(1, 7)]
    # Another database's backups, and those named before the rename (loopx-<database>-...), are left alone.
    others = [backups / "looper-loopx_other-20261001T120000.000000Z.dump",
              backups / "loopx-loopx-20261001T120000.000000Z.dump", backups / "notes.txt"]
    for path in old + others:
        path.write_bytes(b"PGDMP")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    for name in (db_backup.PG_DUMP_ENV, db_backup.BACKUP_CMD_ENV):
        monkeypatch.delenv(name, raising=False)

    new, _ = db_backup.back_up(db_backup.find_dumper(16), "postgresql://u@127.0.0.1:1/loopx", backups)

    assert sorted(backups.iterdir()) == sorted([*old[-4:], new, *others])


def test_the_password_reaches_pg_dump_only_through_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(tmp_path / "bin", mode="offline", restore_ok=None)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    for name in (db_backup.PG_DUMP_ENV, db_backup.BACKUP_CMD_ENV):
        monkeypatch.delenv(name, raising=False)

    db_backup.back_up(db_backup.find_dumper(16), "postgresql://u:p%40ss@127.0.0.1:1/loopx", tmp_path / "backups")

    dump = json.loads(calls.read_text().splitlines()[-1])
    assert dump == {"argv": ["--format=custom", "--no-password", "--dbname=postgresql://u@127.0.0.1:1/loopx"],
                    "PGPASSWORD": "p@ss"}


def test_a_backup_command_runs_as_given_with_the_database_filled_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LOOPER_BACKUP_CMD`` wins over any pg_dump: its version is not asked, its stdout is the backup."""
    calls = _install(tmp_path / "bin", version="9.6.24", mode="offline", restore_ok=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv(db_backup.BACKUP_CMD_ENV, f"{tmp_path / 'bin' / 'pg_dump'} --format=custom -U {{user}} {{dbname}}")

    dumper = db_backup.find_dumper(16)
    path, checked = db_backup.back_up(dumper, "postgresql://alice:pw@127.0.0.1:1/loopx", tmp_path / "backups")

    assert [json.loads(line)["argv"] for line in calls.read_text().splitlines()] == [
        ["--format=custom", "-U", "alice", "loopx"]]
    assert path.read_bytes() == b"PGDMP offline" and "header" in checked
