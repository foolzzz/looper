"""Schema-change dumps omit message bodies; a daily backup of those rows is off until asked for."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from looper import ops_config
from looper.agents import db_backup
from looper.agents.scheduler import Scheduler
from looper.ops_config import OpsConfigError

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
EXCLUDE = "--exclude-table-data=source_events"
TABLE = "--table=source_events"
DSN = "postgresql://u:p%40ss@127.0.0.1:1/loopx"


@pytest.fixture(autouse=True)
def _isolated_ops_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_config, "DOTENV", tmp_path / "no.env")
    for name in ("LOOPER_DATABASE_URL", "LOOPX_DATABASE_URL", "LOOPER_PG_DUMP", "LOOPX_PG_DUMP",
                 "LOOPER_BACKUP_CMD", "LOOPX_BACKUP_CMD", "LOOPER_BACKUP_VERIFY_CMD",
                 "LOOPER_RUNTIME_ROOT", "LOOPX_RUNTIME_ROOT", "LOOPER_BACKUP_MESSAGES", "LOOPX_BACKUP_MESSAGES"):
        monkeypatch.delenv(name, raising=False)


def _install(bin_dir: Path, *, program: str = "pg_dump") -> Path:
    """A stand-in dumper (and a pg_restore beside it) that records its argv and writes an archive header."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir.parent / f"{program}-calls.jsonl"
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(calls)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': args, 'PGPASSWORD': os.environ.get('PGPASSWORD')}) + '\\n')\n"
        "if args == ['--version']:\n"
        "    print('pg_dump (PostgreSQL) 99.1')\n"
        "    raise SystemExit(0)\n"
        "sys.stdout.buffer.write(b'PGDMP')\n"
    )
    path = bin_dir / program
    path.write_text(script)
    path.chmod(0o755)
    restore = bin_dir / "pg_restore"
    if not restore.exists():
        restore.write_text(
            f"#!{sys.executable}\n"
            "print('1; 1259 100 TABLE public source_events source_events')\n"
            "print('2; 0 100 TABLE DATA public source_events source_events')\n"
        )
        restore.chmod(0o755)
    return calls


def _install_docker(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir.parent / "docker-calls.jsonl"
    script = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "record = {'argv': args}\n"
        "if 'pg_dump' in args:\n"
        "    sys.stdout.buffer.write(b'PGDMP')\n"
        "elif 'pg_restore' in args:\n"
        "    sys.stdin.buffer.read()\n"
        "    print('1; 1259 100 TABLE public source_events source_events')\n"
        "    print('2; 0 100 TABLE DATA public source_events source_events')\n"
        "else:\n"
        "    raise SystemExit(2)\n"
        f"with open({str(calls)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
    )
    path = bin_dir / "docker"
    path.write_text(script)
    path.chmod(0o755)
    return calls


def _ops(tmp_path: Path, backup: str = "") -> ops_config.OpsConfig:
    path = tmp_path / "runtime" / ops_config.FILE
    body = '[database]\nurl = "postgresql://u@127.0.0.1:1/loopx"\n'
    if backup:
        body += "[backup]\n" + backup
    ops_config.write_private(path, body)
    return ops_config.load(ops_config.Flags(runtime_root=tmp_path / "runtime"))


def _argv(calls: Path) -> list[list[str]]:
    return [json.loads(line)["argv"] for line in calls.read_text().splitlines()]


def _stamp(when: datetime) -> str:
    return when.strftime(db_backup._STAMP)


def _touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"PGDMP")
    return path


def test_only_source_events_holds_message_bodies() -> None:
    """Message bodies are ``source_events.text`` and ``source_events.body``. The other message-stream tables are
    metadata and stay in the schema-change dump."""
    assert db_backup.MESSAGE_BODY_TABLES == ("source_events",)
    assert db_backup.MESSAGE_KEEP == 7


def test_a_schema_dump_excludes_source_events_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    path, _ = db_backup.back_up(db_backup.find_dumper(16), DSN, tmp_path / "backups")

    dump = json.loads(calls.read_text().splitlines()[-1])
    assert dump["argv"] == ["--format=custom", "--no-password", EXCLUDE,
                            "--dbname=postgresql://u@127.0.0.1:1/loopx"]
    assert dump["PGPASSWORD"] == "p@ss"
    assert "p@ss" not in "".join(dump["argv"])
    assert not any(arg.startswith("--exclude-table=") or arg.startswith("--table=") for arg in dump["argv"])
    assert path.name.startswith("looper-loopx-") and "-messages-" not in path.name
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_pg_dump_command_and_docker_exec_get_the_same_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump_calls = _install(tmp_path / "bin")
    docker_calls = _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    program = str(tmp_path / "bin" / "pg_dump")
    local = _ops(tmp_path, f"command = [{json.dumps(program)}, \"--format=custom\", \"-U\", \"{{user}}\", "
                           "\"{dbname}\"]\n")
    docker = _ops(tmp_path / "docker-ops",
                  'command = ["docker", "exec", "pgc", "pg_dump", "--format=custom", "-U", "{user}", "{dbname}"]\n')

    db_backup.back_up(db_backup.find_dumper(16, local), DSN, tmp_path / "backups")
    db_backup.back_up(db_backup.find_dumper(16, docker), "postgresql://alice@127.0.0.1:1/loopx",
                      tmp_path / "docker-backups")

    assert _argv(dump_calls) == [["--format=custom", "-U", "u", "loopx", EXCLUDE]]
    assert _argv(docker_calls)[0] == ["exec", "pgc", "pg_dump", "--format=custom", "-U", "alice", "loopx", EXCLUDE]


def test_a_backup_command_that_is_not_pg_dump_is_run_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(tmp_path / "bin", program="dump-wrapper")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    program = str(tmp_path / "bin" / "dump-wrapper")
    ops = _ops(tmp_path, f"command = [{json.dumps(program)}, \"--format=custom\", \"{{dbname}}\"]\n")

    db_backup.back_up(db_backup.find_dumper(16, ops), DSN, tmp_path / "backups")

    assert _argv(calls) == [["--format=custom", "loopx"]]


def test_a_message_backup_dumps_only_source_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(tmp_path / "bin")
    docker_calls = _install_docker(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    backups = tmp_path / "backups"

    path, _ = db_backup.back_up_messages(db_backup.find_dumper(16), DSN, backups, now=NOW)
    docker = _ops(tmp_path, 'command = ["docker", "exec", "pgc", "pg_dump", "--format=custom", "{dbname}"]\n')
    docker_path, _ = db_backup.back_up_messages(
        db_backup.find_dumper(16, docker), DSN, tmp_path / "docker-backups", now=NOW)

    dump = json.loads(calls.read_text().splitlines()[-1])
    assert dump["argv"] == ["--format=custom", "--no-password", TABLE,
                            "--dbname=postgresql://u@127.0.0.1:1/loopx"]
    assert EXCLUDE not in dump["argv"]
    assert dump["PGPASSWORD"] == "p@ss"
    assert _argv(docker_calls)[0] == ["exec", "pgc", "pg_dump", "--format=custom", "loopx", TABLE]
    assert path.name == f"looper-loopx-messages-{_stamp(NOW)}.dump"
    assert docker_path.name.startswith("looper-loopx-messages-")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent == backups


def test_a_message_backup_refuses_a_command_that_is_not_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(tmp_path / "bin", program="dump-wrapper")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    program = str(tmp_path / "bin" / "dump-wrapper")
    ops = _ops(tmp_path, f"command = [{json.dumps(program)}, \"{{dbname}}\"]\n")
    backups = tmp_path / "backups"

    with pytest.raises(db_backup.BackupFailed, match="not pg_dump"):
        db_backup.back_up_messages(db_backup.find_dumper(16, ops), DSN, backups, now=NOW)

    assert not calls.exists()
    assert not backups.exists()


def test_message_backup_rotation_keeps_seven_and_touches_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    backups = tmp_path / "backups"
    backups.mkdir()
    old = [_touch(backups, f"looper-loopx-messages-2026110{day}T120000.000000Z.dump") for day in range(1, 9)]
    kept_others = [
        _touch(backups, "looper-loopx-20261101T120000.000000Z.dump"),
        _touch(backups, "looper-loopx_other-messages-20261101T120000.000000Z.dump"),
        _touch(backups, "looper-other-20261101T120000.000000Z.dump"),
        _touch(backups, "loopx-loopx-messages-20261101T120000.000000Z.dump"),
        _touch(backups, "loopx-loopx-20261101T120000.000000Z.dump"),
        _touch(backups, "looper-loopx-messages-20261101T120000.000000Z.dump.partial"),
        _touch(backups, "notes.txt"),
    ]

    new, _ = db_backup.back_up_messages(db_backup.find_dumper(16), DSN, backups, now=NOW)

    assert sorted(backups.iterdir()) == sorted([*old[-6:], new, *kept_others])
    assert len([path for path in backups.iterdir() if path.name.startswith("looper-loopx-messages-")
                and path.suffix == ".dump"]) == 7


def test_schema_rotation_does_not_delete_message_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    backups = tmp_path / "backups"
    backups.mkdir()
    schema = [_touch(backups, f"looper-loopx-2026100{day}T120000.000000Z.dump") for day in range(1, 7)]
    messages = [_touch(backups, f"looper-loopx-messages-2026110{day}T120000.000000Z.dump") for day in range(1, 4)]
    note = _touch(backups, "notes.txt")

    new, _ = db_backup.back_up(db_backup.find_dumper(16), DSN, backups, now=NOW)

    assert sorted(backups.iterdir()) == sorted([*schema[-4:], new, *messages, note])


def test_a_message_backup_younger_than_a_day_is_not_due(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    assert db_backup.message_backup_due(backups, "loopx", NOW)
    fresh = _touch(backups, f"looper-loopx-messages-{_stamp(NOW - timedelta(hours=23))}.dump")
    _touch(backups, "looper-loopx-20261101T120000.000000Z.dump")
    _touch(backups, "notes.txt")
    assert not db_backup.message_backup_due(backups, "loopx", NOW)
    assert db_backup.message_backup_due(backups, "loopx", NOW - timedelta(hours=23) + db_backup.MESSAGE_INTERVAL)
    assert db_backup.newest_message_backup(backups, "loopx") == NOW - timedelta(hours=23)
    assert fresh.read_bytes() == b"PGDMP"


def test_messages_defaults_off_and_renders_only_when_daily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setting = next(item for item in ops_config.SETTINGS if item.name == "message_backups")
    assert setting.variables == ()
    monkeypatch.setenv("LOOPER_BACKUP_MESSAGES", "daily")
    monkeypatch.setenv("LOOPX_BACKUP_MESSAGES", "daily")
    off = _ops(tmp_path)
    assert off.message_backups == "off"
    assert db_backup.message_backup_for(off, tmp_path / "runtime") is None
    assert "messages" not in ops_config.render(off)

    explicit = _ops(tmp_path / "explicit", 'messages = "off"\n')
    assert explicit.message_backups == "off"
    assert "messages" not in ops_config.render(explicit)

    daily = _ops(tmp_path / "daily", 'messages = "daily"\n')
    assert daily.message_backups == "daily"
    assert daily.sources["message_backups"].origin is ops_config.Origin.FILE
    rendered = ops_config.render(daily)
    assert 'messages = "daily"' in rendered
    ops_config.write_private(daily.path, rendered)
    assert ops_config.load(ops_config.Flags(runtime_root=daily.runtime_root)).message_backups == "daily"
    job = db_backup.message_backup_for(daily, daily.runtime_root)
    assert job is not None and job.dbname == "loopx"
    assert job.backup_dir == daily.runtime_root / "backups"


@pytest.mark.parametrize("value", ['"weekly"', "1", "true", '"Daily"', '"off "'])
def test_messages_rejects_anything_but_off_or_daily(tmp_path: Path, value: str) -> None:
    path = tmp_path / "runtime" / ops_config.FILE
    ops_config.write_private(path, f'[database]\nurl = "postgresql://u@127.0.0.1:1/loopx"\n[backup]\n'
                             f"messages = {value}\n")
    with pytest.raises(OpsConfigError, match=r"backup\.messages.*must be off or daily"):
        ops_config.load(ops_config.Flags(runtime_root=tmp_path / "runtime"))


def test_message_backups_default_off_dumps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("message backup ran")

    monkeypatch.setattr(db_backup, "run_message_backup", refuse)
    monkeypatch.setattr(db_backup, "back_up_messages", refuse)
    backups = tmp_path / "backups"
    backups.mkdir()
    kept = [_touch(backups, f"looper-loopx-messages-2026110{day}T120000.000000Z.dump") for day in range(1, 10)]
    note = _touch(backups, "notes.txt")
    scheduler = Scheduler(bus=None, runtime_root=tmp_path)

    scheduler._consider_message_backup(NOW)

    assert scheduler._message_backup is None
    assert scheduler._background == set()
    assert sorted(backups.iterdir()) == sorted([*kept, note])


def test_a_daily_backup_runs_once_until_the_next_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[datetime] = []

    def dump(job: db_backup.MessageBackup, server_major: int | None, now: datetime) -> tuple[Path, str]:
        calls.append(now)
        path = job.backup_dir / f"looper-{job.dbname}-messages-{_stamp(now)}.dump"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PGDMP")
        return path, "listed"

    async def major(_self: Scheduler) -> int:
        return 16

    monkeypatch.setattr(db_backup, "run_message_backup", dump)
    monkeypatch.setattr(Scheduler, "_message_backup_server_major", major)
    job = db_backup.message_backup_for(_ops(tmp_path, 'messages = "daily"\n'), tmp_path / "runtime")
    scheduler = Scheduler(bus=None, runtime_root=tmp_path, message_backup=job)

    async def run() -> None:
        scheduler._consider_message_backup(NOW)
        await scheduler.idle()
        scheduler._consider_message_backup(NOW + timedelta(minutes=1))
        await scheduler.idle()

    import asyncio
    asyncio.run(run())

    assert calls == [NOW]
    assert scheduler._message_backup_not_before == NOW + db_backup.MESSAGE_INTERVAL


def test_a_failed_daily_backup_is_not_retried_every_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[datetime] = []

    def fail(_job: object, _server_major: object, now: datetime) -> None:
        calls.append(now)
        raise db_backup.BackupFailed("disk full")

    async def major(_self: Scheduler) -> None:
        return None

    monkeypatch.setattr(db_backup, "run_message_backup", fail)
    monkeypatch.setattr(Scheduler, "_message_backup_server_major", major)
    job = db_backup.message_backup_for(_ops(tmp_path, 'messages = "daily"\n'), tmp_path / "runtime")
    scheduler = Scheduler(bus=None, runtime_root=tmp_path, message_backup=job)

    async def run() -> None:
        scheduler._consider_message_backup(NOW)
        await scheduler.idle()
        scheduler._consider_message_backup(NOW + timedelta(seconds=15))
        await scheduler.idle()

    import asyncio
    asyncio.run(run())

    assert calls == [NOW]
    assert not (tmp_path / "runtime" / "backups").exists()


def test_a_recent_message_backup_is_not_repeated_and_a_restart_retries_an_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[datetime] = []

    def dump(job: db_backup.MessageBackup, _server_major: object, now: datetime) -> tuple[Path, str]:
        calls.append(now)
        path = job.backup_dir / f"looper-{job.dbname}-messages-{_stamp(now)}.dump"
        path.write_bytes(b"PGDMP")
        return path, "listed"

    async def major(_self: Scheduler) -> int:
        return 16

    monkeypatch.setattr(db_backup, "run_message_backup", dump)
    monkeypatch.setattr(Scheduler, "_message_backup_server_major", major)
    root = tmp_path / "runtime"
    job = db_backup.message_backup_for(_ops(tmp_path, 'messages = "daily"\n'), root)
    assert job is not None
    job.backup_dir.mkdir(parents=True)
    fresh = _touch(job.backup_dir, f"looper-loopx-messages-{_stamp(NOW - timedelta(hours=2))}.dump")
    scheduler = Scheduler(bus=None, runtime_root=tmp_path, message_backup=job)

    scheduler._consider_message_backup(NOW)

    assert calls == []
    assert fresh.read_bytes() == b"PGDMP"
    assert scheduler._message_backup_not_before == NOW - timedelta(hours=2) + db_backup.MESSAGE_INTERVAL

    fresh.unlink()
    _touch(job.backup_dir, f"looper-loopx-messages-{_stamp(NOW - timedelta(days=2))}.dump")
    restarted = Scheduler(bus=None, runtime_root=tmp_path, message_backup=job)

    async def run() -> None:
        restarted._consider_message_backup(NOW)
        await restarted.idle()

    import asyncio
    asyncio.run(run())
    assert calls == [NOW]
