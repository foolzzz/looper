"""A ``pg_dump`` of Looper's database before ``looper serve`` changes its schema.

``looper serve`` applies ``schema.sql`` at every start, and the file holds
migrations that drop columns and tables. When the schema is about to change
(``schema_change_pending``), serve first writes a custom-format dump into
``<runtime root>/backups/`` and keeps the newest ``KEEP`` of that database's;
without a way to make one it does not start. Registry upgrades cannot use
the general ``--skip-backup`` override.

The dump is made by, in order of precedence (the ops file's ``[backup]``,
``looper.ops_config``; ``LOOPER_BACKUP_CMD`` and ``LOOPER_PG_DUMP`` for one
release):

- ``backup.command``: a command run as given whose stdout is the dump, for a
  ``pg_dump`` that cannot reach the database URL as written, such as the one
  inside a PostgreSQL container; ``{dbname}`` and ``{user}`` are filled in
  from the database URL. Its version is not checked; a verifier must still
  list the archive before migration (below). When that command is ``pg_dump``
  (including ``docker exec [options] <container> pg_dump``), Looper appends
  ``--exclude-table-data`` for the message-body tables. Any other program is
  run as given and has to exclude that data itself.
- the ``pg_dump`` that ``backup.pg_dump`` names, else ``pg_dump`` on PATH. Its
  major version must be at least the server's; the dump is then listed with
  the ``pg_restore`` beside it (else the one on PATH) to prove it is readable.

The schema-change dump keeps every table's definition. It omits the rows of
the message-body tables (``MESSAGE_BODY_TABLES``: ``source_events``, whose
``text`` and ``body`` are the ingested messages). The other message-stream
tables (``event_sources``, ``source_chats``, ``source_threads``,
``source_people``, ``source_runs``) are metadata and cursors and stay, rows
included. Delivered subscription output stays too.

A daily dump of just those message-body tables is off until ``[backup]
messages = "daily"`` (``message_backup_for``). It writes
``looper-<database>-<stamp>.messages.dump`` in the same directory, at most
once a day, and keeps the newest ``MESSAGE_KEEP``. The ``.messages.dump``
suffix is one a schema-change backup cannot have, so the two rotations do
not match each other's files (a database named ``demo-messages`` would
otherwise share ``looper-demo-messages-<stamp>.dump`` with ``demo``'s message
backups). Rotation deletes only its own files. Omit the key, or set
``"off"``, and nothing is dumped or deleted. The daily dump is an asyncio
subprocess: cancelling it terminates the process, kills it if it is still
running, and reaps it, so stopping the service does not leave ``pg_dump``
behind.

The dump is proved readable by, in order: ``backup.verify_command`` (the
dump on its stdin; it must list the archive like ``pg_restore --list``;
``LOOPER_BACKUP_VERIFY_CMD`` for one release); for a ``backup.command`` that is
``docker exec [options] <container> pg_dump ...``, ``docker exec -i
<container> pg_restore --list``; else the local ``pg_restore`` as above. With
none of these the backup fails, before any DDL.

Each of these runs with a backup's environment (``child_env.backup``): a
child's, with libpq's and Docker's variables, and ``PGPASSWORD`` from the
database URL for ``pg_dump``.
"""

from __future__ import annotations

import asyncio
import datetime
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import child_env
from ..ops_config import OpsConfig, Origin

if TYPE_CHECKING:
    from ..components import Manifest

ONLINE_BACKUP_MAX_AGE = datetime.timedelta(minutes=15)
BACKUP_DIRNAME = "backups"
KEEP = 5
# Ingested message bodies live only in source_events (text and body). The other
# message-stream tables are metadata; subscription output is a processor reply.
MESSAGE_BODY_TABLES = ("source_events",)
MESSAGE_KEEP = 7
MESSAGE_INTERVAL = datetime.timedelta(days=1)
_SCHEMA_EXCLUDES = tuple(f"--exclude-table-data={name}" for name in MESSAGE_BODY_TABLES)
_MESSAGE_TABLES = tuple(f"--table={name}" for name in MESSAGE_BODY_TABLES)

_VERSION_TIMEOUT_SECONDS = 15.0
_DUMP_TIMEOUT_SECONDS = 3600.0
# How long a cancelled daily dump is given to exit after SIGTERM before SIGKILL.
# Well under serve.SHUTDOWN_DEADLINE_SEC, so stop() finishes inside that bound.
_DUMP_STOP_GRACE_SECONDS = 2.0
_LIST_TIMEOUT_SECONDS = 300.0
_MAGIC = b"PGDMP"  # how every custom-format archive starts
_STAMP = "%Y%m%dT%H%M%S.%fZ"
_STAMP_PATTERN = r"\d{8}T\d{6}\.\d{6}Z"
_PG_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)")
_PLACEHOLDER = re.compile(r"\{(dbname|user)\}")
# docker exec options that take a value in the next word (or attached: -eNAME, --env=NAME), and those that do not.
_EXEC_VALUE_SHORT = frozenset("euw")
_EXEC_VALUE_LONG = frozenset({"--env", "--env-file", "--user", "--workdir", "--detach-keys"})
_EXEC_FLAG_LONG = frozenset({"--interactive", "--tty", "--detach", "--privileged"})
NO_VERIFIER = ("no way to verify the backup: pg_restore is not beside pg_dump or on PATH; set backup.verify_command in "
               "the ops file's [backup] to a command that lists the archive from stdin like `pg_restore --list`; with "
               "PostgreSQL in Docker:\n"
               '  verify_command = ["docker", "exec", "-i", "<container>", "pg_restore", "--list"]')


class NoDumper(Exception):
    """No usable way to make a backup; the message says why."""


class BackupFailed(Exception):
    """The backup was not made; the message says why. Nothing is left behind."""


@dataclass(frozen=True)
class Dumper:
    """How a backup is made."""

    argv: tuple[str, ...]  # the pg_dump program, or the whole backup.command
    command: bool  # backup.command: placeholders filled in; pg_dump also gets the message-body flags
    detail: str  # what doctor and the log show
    verify: tuple[str, ...] = ()  # lists the archive read from stdin; else the local pg_restore does
    verify_label: str = ""  # how the result names ``verify``

    @property
    def verifier(self) -> str | None:
        """How the dump will be proved readable, as doctor and the check result say it; None when it cannot be."""
        if self.verify:
            return self.verify_label
        local = self.pg_restore
        return f"{local} --list" if local else None

    @property
    def pg_restore(self) -> str | None:
        """The pg_restore that lists this pg_dump's backups: the one beside it, else on PATH."""
        if self.command:
            return shutil.which("pg_restore")
        sibling = Path(self.argv[0]).with_name("pg_restore")
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
        return shutil.which("pg_restore")


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def find_dumper(server_major: int | None, ops: OpsConfig | None = None) -> Dumper:
    """The way backups are made here with the ops settings ``ops`` (None: none set); ``NoDumper`` when there is none
    or ``pg_dump`` is older than the server."""
    command, configured = (ops.backup_command, ops.pg_dump) if ops else ((), "")
    verify, verify_label = _verifier(command, ops.backup_verify_command if ops else (), ops)
    if command:
        # The program only: its arguments can hold a password (docker exec -e PGPASSWORD=...).
        return Dumper(command, True, f"{ops.sources['backup_command']}: {command[0]} … (version not checked)",
                      verify, verify_label)
    program = shutil.which(configured or "pg_dump")
    if program is None:
        raise NoDumper(f"{ops.sources['pg_dump']} names {configured}, which is not an executable" if configured
                       else "`pg_dump` is not on PATH")
    try:
        version = subprocess.run([program, "--version"], capture_output=True, text=True, env=child_env.backup(),
                                 stdin=subprocess.DEVNULL, timeout=_VERSION_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NoDumper(f"`{program} --version` failed: {exc}") from None
    line = _first_line(version.stdout)
    match = _PG_VERSION.search(line)
    if version.returncode != 0 or match is None:
        raise NoDumper(f"`{program} --version` gave no PostgreSQL version: "
                       f"{line or _first_line(version.stderr) or 'no output'}")
    if server_major is not None and int(match.group(1)) < server_major:
        raise NoDumper(f"{line} ({program}) is older than the PostgreSQL {server_major} server; "
                       "pg_dump must be at least the server's major version")
    return Dumper((program,), False, f"{line} ({program})", verify, verify_label)


def docker_exec_container(command: tuple[str, ...]) -> str | None:
    """The container of ``docker exec [options] <container> pg_dump ...``; None for any other command."""
    if len(command) < 4 or Path(command[0]).name != "docker" or command[1] != "exec":
        return None
    words = iter(command[2:])
    for word in words:
        if word == "--":
            word = next(words, "")
        elif word.startswith("--"):
            name = word.partition("=")[0]
            if name in _EXEC_VALUE_LONG:
                if "=" not in word:
                    next(words, None)
            elif name not in _EXEC_FLAG_LONG:
                return None  # an option whose value we cannot tell from the container
            continue
        elif word.startswith("-") and word != "-":
            for index, letter in enumerate(word[1:], 1):
                if letter in _EXEC_VALUE_SHORT:
                    if index == len(word) - 1:
                        next(words, None)
                    break
                if letter == "=":
                    break  # `-t=false`: the rest is the boolean's value
                if letter not in "idt":
                    return None
            continue
        container, program = word, next(words, "")
        return container if container and Path(program).name == "pg_dump" else None
    return None


def _accepts_pg_dump_args(argv: tuple[str, ...]) -> bool:
    """Whether ``argv`` is pg_dump, or ``docker exec ... pg_dump``, so extra pg_dump flags are safe to append."""
    if not argv:
        return False
    return Path(argv[0]).name == "pg_dump" or docker_exec_container(argv) is not None


def _verifier(command: tuple[str, ...], explicit: tuple[str, ...], ops: OpsConfig | None) -> tuple[tuple[str, ...], str]:
    """The command that lists the dump from stdin and how the result names it; ((), "") for the local pg_restore."""
    if explicit:
        # The program only, like the dump command: its arguments can hold a password.
        source = ops.sources["backup_verify_command"]
        return explicit, f"{explicit[0]} … ({'backup.verify_command' if source.origin is Origin.FILE else source})"
    container = docker_exec_container(command)
    if container is None:
        return (), ""
    return (command[0], "exec", "-i", container, "pg_restore", "--list"), \
        f"docker exec -i {container} pg_restore --list"


def _connection(dsn: str) -> tuple[str, str | None, str, str]:
    """``dsn`` without its password, the password, the database name and the user."""
    parts = urllib.parse.urlsplit(dsn)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    options = dict(query)
    password = urllib.parse.unquote(parts.password) if parts.password is not None else options.get("password")
    netloc = parts.netloc
    if parts.password is not None:
        userinfo, _, host = netloc.rpartition("@")
        netloc = f"{userinfo.partition(':')[0]}@{host}"
    kept = urllib.parse.urlencode([(key, value) for key, value in query if key != "password"])
    user = urllib.parse.unquote(parts.username or "") or options.get("user") or getpass.getuser()
    dbname = urllib.parse.unquote(parts.path.lstrip("/")) or options.get("dbname") or user
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=kept)), password, dbname, user


def _file_stem(dbname: str) -> str:
    return "looper-" + re.sub(r"[^A-Za-z0-9_.-]", "_", dbname)


def _named_dumps(backup_dir: Path, pattern: re.Pattern[str]) -> list[tuple[str, Path]]:
    if not backup_dir.is_dir():
        return []
    return sorted(
        (match.group(1), path) for path in backup_dir.iterdir()
        if path.is_file() and (match := pattern.fullmatch(path.name))
    )


def _drop_oldest(found: list[tuple[str, Path]], keep: int) -> list[Path]:
    old = [path for _, path in found[:max(len(found) - keep, 0)]]
    for path in old:
        path.unlink()
    return old


def _schema_pattern(dbname: str) -> re.Pattern[str]:
    """``looper-<database>-<stamp>.dump`` exactly. ``.messages.dump`` does not match."""
    return re.compile(re.escape(_file_stem(dbname)) + rf"-({_STAMP_PATTERN})\.dump")


def prune(backup_dir: Path, dbname: str, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` schema-change backups of ``dbname`` (by the time in their names).

    Message backups (``looper-<database>-<stamp>.messages.dump``) do not match, nor does any other file.
    A database whose name ends in ``-messages`` cannot match another database's message backups.
    """
    removed = _drop_oldest(_named_dumps(backup_dir, _schema_pattern(dbname)), keep)
    for path in removed:
        path.with_suffix(".dump.json").unlink(missing_ok=True)
    return removed


def _message_pattern(dbname: str) -> re.Pattern[str]:
    """``looper-<database>-<stamp>.messages.dump`` exactly. A schema-change dump cannot match."""
    return re.compile(re.escape(_file_stem(dbname)) + rf"-({_STAMP_PATTERN})\.messages\.dump")


def prune_messages(backup_dir: Path, dbname: str, keep: int = MESSAGE_KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` message backups of ``dbname``; the deleted ones.

    Only ``looper-<database>-<stamp>.messages.dump`` matches. Schema-change dumps and every other name stay.
    """
    return _drop_oldest(_named_dumps(backup_dir, _message_pattern(dbname)), keep)


def newest_message_backup(backup_dir: Path, dbname: str) -> datetime.datetime | None:
    """When the newest message backup of ``dbname`` was taken, from its name; None when there is none."""
    found = _named_dumps(backup_dir, _message_pattern(dbname))
    if not found:
        return None
    return datetime.datetime.strptime(found[-1][0], _STAMP).replace(tzinfo=datetime.timezone.utc)


def message_backup_due(backup_dir: Path, dbname: str, now: datetime.datetime) -> bool:
    """Whether ``dbname`` has no message backup, or its newest one is at least ``MESSAGE_INTERVAL`` old."""
    newest = newest_message_backup(backup_dir, dbname)
    return newest is None or now - newest >= MESSAGE_INTERVAL


def _tail(text: str, *, password: str | None, lines: int = 5) -> str:
    tail = " | ".join(line.strip() for line in text.strip().splitlines()[-lines:] if line.strip())
    return tail.replace(password, "***") if password else tail


def back_up(dumper: Dumper, dsn: str, backup_dir: Path, *, now: datetime.datetime | None = None) -> tuple[Path, str]:
    """Write a verified schema-change dump of ``dsn`` into ``backup_dir`` (mode 0600), then prune.

    The rows of ``MESSAGE_BODY_TABLES`` are left out when the dump is ``pg_dump``;
    their schema stays. A ``backup.command`` that is not pg_dump is run as given.
    ``BackupFailed`` when the dump fails or is not a readable custom-format
    archive; the partial file is removed. Returns (its path, how it was checked).
    """
    return _write_dump(dumper, dsn, backup_dir, now=now, extra=_SCHEMA_EXCLUDES, require_extra=False,
                       name_for=lambda dbname, stamp: f"{_file_stem(dbname)}-{stamp}.dump",
                       keep_own=prune)


async def schema_state(conn: Any) -> dict:
    """The applied schema and migration markers, read from one consistent snapshot without DDL."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        state = {}
        for table, field, query in (
            ("schema_fingerprint", "fingerprint", "SELECT sha256 FROM schema_fingerprint"),
            ("registry_schema_version", "registry_version", "SELECT version FROM registry_schema_version"),
        ):
            state[field] = (await conn.fetchval(query)
                            if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table) else None)
        state["migrations"] = ([row["name"] for row in await conn.fetch(
            "SELECT name FROM schema_migrations ORDER BY name")]
            if await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL") else [])
        return state


def _database_identity(dsn: str) -> str:
    # Password changes do not change which database an archive protects. Never persist the URL.
    return hashlib.sha256(_connection(dsn)[0].encode()).hexdigest()


def _archive_identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


async def online_upgrade_backup(ops: OpsConfig, runtime_root: Path, target: Manifest, *,
                                now: datetime.datetime | None = None) -> Path:
    """Take the regular verified backup online; publish reuse metadata only if its schema stayed unchanged.

    The snapshot starts before pg_dump runs, so freshness never hides the dump's duration.
    A schema change during the dump leaves the archive for recovery but no reusable metadata.
    """
    import asyncpg
    from ..ops_config import write_private

    conn = None
    try:
        conn = await asyncpg.connect(ops.database_url, timeout=15, command_timeout=30)
        before = await schema_state(conn)
        major = int(await conn.fetchval("SELECT current_setting('server_version_num')")) // 10000
        dumper = await asyncio.to_thread(find_dumper, major, ops)
        started = now or datetime.datetime.now(datetime.timezone.utc)
        path, checked = await asyncio.to_thread(back_up, dumper, ops.database_url,
                                               runtime_root / BACKUP_DIRNAME, now=started)
        if await schema_state(conn) != before:
            raise BackupFailed("the database schema changed during the online backup; retry before draining")
        metadata = {"target_version": target.version, "target_control_plane": target.components["control_plane"],
                    "database": _database_identity(ops.database_url), "schema_state": before,
                    "started_at": started.isoformat(),
                    "verified_at": (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(),
                    "verified": True, "verification": checked, "archive": _archive_identity(path)}
        await asyncio.to_thread(write_private, path.with_suffix(".dump.json"), json.dumps(metadata))
        return path
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as exc:
        # Connection errors and filesystem errors can embed credentials or command arguments.
        raise BackupFailed(f"online backup could not complete ({type(exc).__name__}); nothing was drained") from None
    finally:
        if conn is not None:
            await conn.close()


def _matching_online_metadata(path: Path, ops: OpsConfig, target: Manifest, now: datetime.datetime) -> dict | None:
    try:
        sidecar = path.with_suffix(".dump.json")
        if sidecar.is_symlink() or path.is_symlink():
            return None
        metadata = json.loads(sidecar.read_text())
        if not isinstance(metadata, dict):
            return None
        started = datetime.datetime.fromisoformat(metadata["started_at"])
        verified = datetime.datetime.fromisoformat(metadata["verified_at"])
        if (started.tzinfo is None or verified.tzinfo is None or not started <= verified <= now
                or not datetime.timedelta(0) <= now - started <= ONLINE_BACKUP_MAX_AGE
                or metadata.get("verified") is not True or not metadata.get("verification")
                or metadata.get("database") != _database_identity(ops.database_url)
                or metadata.get("target_version") != target.version
                or metadata.get("target_control_plane") != target.components["control_plane"]
                or metadata.get("archive") != _archive_identity(path)):
            return None
        return metadata
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _verify_online_archive(dumper: Dumper, path: Path, dsn: str) -> None:
    _, password, dbname, user = _connection(dsn)

    def fill(text: str) -> str:
        return _PLACEHOLDER.sub(lambda match: dbname if match[1] == "dbname" else user, text)

    _verify(path, dumper.pg_restore, password=password, verify=tuple(fill(arg) for arg in dumper.verify),
            verify_label=fill(dumper.verify_label))


async def reusable_upgrade_backup(conn: Any, ops: OpsConfig, runtime_root: Path, target: Manifest | None = None, *,
                                   now: datetime.datetime | None = None) -> Path | None:
    """Consume one fresh, matching, still-readable archive's metadata, or fall back to the normal startup dump.

    Without online metadata this performs no database queries or command resolution.
    Registry upgrades must be excluded by the caller before reaching this function.
    """
    backup_dir = runtime_root / BACKUP_DIRNAME
    _, _, dbname, _ = _connection(ops.database_url)
    candidates = [path for _, path in reversed(_named_dumps(backup_dir, _schema_pattern(dbname)))
                  if path.with_suffix(".dump.json").is_file()]
    if not candidates:
        return None
    if target is None:
        from ..components import prepare_loaded_manifest
        target = await prepare_loaded_manifest()
    current = None
    for path in candidates:
        metadata = _matching_online_metadata(path, ops, target, now or datetime.datetime.now(datetime.timezone.utc))
        if metadata is None:
            continue
        if current is None:
            current = await schema_state(conn)
        if metadata.get("schema_state") != current:
            continue
        try:
            dumper = await asyncio.to_thread(find_dumper, None, ops)
            await asyncio.to_thread(_verify_online_archive, dumper, path, ops.database_url)
            # Do not authorize a replacement archive or a schema changed while listing it.
            if (_matching_online_metadata(path, ops, target, now or datetime.datetime.now(datetime.timezone.utc))
                    is None or await schema_state(conn) != current):
                continue
            # Consume before migration: a subsequent startup may not reuse this recovery point.
            path.with_suffix(".dump.json").unlink()
        except (NoDumper, BackupFailed, OSError):
            continue
        return path
    return None


def back_up_messages(dumper: Dumper, dsn: str, backup_dir: Path, *,
                     now: datetime.datetime | None = None) -> tuple[Path, str]:
    """Write a verified dump of the message-body tables into ``backup_dir``, then keep ``MESSAGE_KEEP`` of them.

    The file is ``looper-<database>-<stamp>.messages.dump``. A command that is
    not pg_dump is refused, so this cannot take a full database dump by mistake.
    """
    return _write_dump(dumper, dsn, backup_dir, now=now, extra=_MESSAGE_TABLES, require_extra=True,
                       name_for=_message_name, keep_own=prune_messages)


def _message_name(dbname: str, stamp: str) -> str:
    return f"{_file_stem(dbname)}-{stamp}.messages.dump"


@dataclass(frozen=True)
class MessageBackup:
    """One enabled daily message backup: where, and the ops file that says how to run pg_dump."""

    dsn: str
    backup_dir: Path
    ops: OpsConfig

    @property
    def dbname(self) -> str:
        return _connection(self.dsn)[2]


def message_backup_for(ops: OpsConfig, runtime_root: Path) -> MessageBackup | None:
    """The daily message backup ``ops`` asks for; None when ``[backup] messages`` is off (the default)."""
    if ops.message_backups != "daily":
        return None
    return MessageBackup(ops.database_url, runtime_root / BACKUP_DIRNAME, ops)


async def run_message_backup(job: MessageBackup, server_major: int | None, now: datetime.datetime) -> tuple[Path, str]:
    """Dump ``job``'s message-body tables. Cancellation stops the ``pg_dump`` process and reaps it.

    ``NoDumper`` or ``BackupFailed`` when the dump cannot be made. The process is an asyncio subprocess, not a
    thread, so ``scheduler.stop`` does not leave it running past the service's shutdown bound.
    """
    return await _write_dump_async(find_dumper(server_major, job.ops), job.dsn, job.backup_dir, now=now,
                                   extra=_MESSAGE_TABLES, require_extra=True, name_for=_message_name,
                                   keep_own=prune_messages)


@dataclass
class _PlannedDump:
    argv: list[str]
    env: dict[str, str]
    path: Path
    partial: Path
    password: str | None
    dumper: Dumper
    fill: Callable[[str], str]
    dbname: str


def _plan_dump(dumper: Dumper, dsn: str, backup_dir: Path, *, now: datetime.datetime | None,
               extra: tuple[str, ...], require_extra: bool, name_for: Callable[[str, str], str]) -> _PlannedDump:
    bare_dsn, password, dbname, user = _connection(dsn)

    def fill(text: str) -> str:
        return _PLACEHOLDER.sub(lambda m: dbname if m.group(1) == "dbname" else user, text)

    if dumper.command:
        argv = [fill(arg) for arg in dumper.argv]
        if extra and _accepts_pg_dump_args(dumper.argv):
            argv.extend(extra)
        elif extra and require_extra:
            raise BackupFailed(
                f"`{Path(dumper.argv[0]).name}` is not pg_dump, so a message backup cannot limit the dump to "
                f"{', '.join(MESSAGE_BODY_TABLES)}; set backup.command to pg_dump or `docker exec ... pg_dump`")
        env = child_env.backup()
    else:
        # Options stay in front of --dbname. pg_dump accepts either order; the last argument is the database.
        argv = [*dumper.argv, "--format=custom", "--no-password", *extra, f"--dbname={bare_dsn}"]
        env = {**child_env.backup(), **({"PGPASSWORD": password} if password else {})}
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(_STAMP)
    path = backup_dir / name_for(dbname, stamp)
    return _PlannedDump(argv, env, path, path.with_name(path.name + ".partial"), password, dumper, fill, dbname)


def _write_dump(dumper: Dumper, dsn: str, backup_dir: Path, *, now: datetime.datetime | None,
                extra: tuple[str, ...], require_extra: bool, name_for: Callable[[str, str], str],
                keep_own: Callable[[Path, str], list[Path]]) -> tuple[Path, str]:
    plan = _plan_dump(dumper, dsn, backup_dir, now=now, extra=extra, require_extra=require_extra, name_for=name_for)
    try:
        _run_dump(plan)
        return _checked(plan, keep_own)
    except BaseException:
        plan.partial.unlink(missing_ok=True)
        raise


async def _write_dump_async(dumper: Dumper, dsn: str, backup_dir: Path, *, now: datetime.datetime | None,
                            extra: tuple[str, ...], require_extra: bool, name_for: Callable[[str, str], str],
                            keep_own: Callable[[Path, str], list[Path]]) -> tuple[Path, str]:
    plan = _plan_dump(dumper, dsn, backup_dir, now=now, extra=extra, require_extra=require_extra, name_for=name_for)
    try:
        await _run_dump_async(plan)
        return _checked(plan, keep_own)
    except BaseException:
        plan.partial.unlink(missing_ok=True)
        raise


def _checked(plan: _PlannedDump, keep_own: Callable[[Path, str], list[Path]]) -> tuple[Path, str]:
    checked = _verify(plan.partial, plan.dumper.pg_restore, password=plan.password,
                      verify=tuple(plan.fill(arg) for arg in plan.dumper.verify),
                      verify_label=plan.fill(plan.dumper.verify_label))
    os.replace(plan.partial, plan.path)
    keep_own(plan.path.parent, plan.dbname)
    return plan.path, checked


def _run_dump(plan: _PlannedDump) -> None:
    fd = os.open(plan.partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as out:
        try:
            dump = subprocess.run(plan.argv, stdout=out, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  env=plan.env, timeout=_DUMP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupFailed(f"`{plan.argv[0]}` did not finish: {_why(exc)}") from None
    if dump.returncode != 0:
        reason = _tail(dump.stderr.decode("utf-8", "replace"), password=plan.password)
        raise BackupFailed(f"`{plan.argv[0]}` exited with {dump.returncode}: {reason or 'no message'}")


async def _run_dump_async(plan: _PlannedDump) -> None:
    """Run the dump as an asyncio subprocess. Cancellation terminates it, then kills and reaps it."""
    fd = os.open(plan.partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    out = os.fdopen(fd, "wb")
    try:
        process = await asyncio.create_subprocess_exec(
            *plan.argv, stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=asyncio.subprocess.PIPE,
            env=plan.env)
    except OSError as exc:
        raise BackupFailed(f"`{plan.argv[0]}` did not finish: {_why(exc)}") from None
    finally:
        out.close()
    assert process.stderr is not None
    try:
        stderr = await asyncio.wait_for(process.stderr.read(), _DUMP_TIMEOUT_SECONDS)
        code = await process.wait()
    except asyncio.TimeoutError:
        await _stop_dump(process)
        raise BackupFailed(
            f"`{plan.argv[0]}` did not finish: timed out after {_DUMP_TIMEOUT_SECONDS:g}s") from None
    except asyncio.CancelledError:
        await _stop_dump(process)
        raise
    if code != 0:
        reason = _tail(stderr.decode("utf-8", "replace"), password=plan.password)
        raise BackupFailed(f"`{plan.argv[0]}` exited with {code}: {reason or 'no message'}")


async def _stop_dump(process: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL if it is still running after the grace, and wait until it has been reaped."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), _DUMP_STOP_GRACE_SECONDS)
    except asyncio.TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


def _why(exc: OSError | subprocess.TimeoutExpired) -> str:
    # TimeoutExpired's text embeds the full argv, which can carry secrets.
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout:g}s"
    return exc.strerror or type(exc).__name__


def _verify(path: Path, pg_restore: str | None, *, password: str | None, verify: tuple[str, ...] = (),
            verify_label: str = "") -> str:
    with path.open("rb") as dump:
        head = dump.read(len(_MAGIC))
    if not head:
        raise BackupFailed("the dump is empty")
    if head != _MAGIC:
        raise BackupFailed("the dump is not a custom-format archive (does the command pass --format=custom?)")
    if not verify and pg_restore is None:
        raise BackupFailed(NO_VERIFIER)
    label = verify_label if verify else f"{pg_restore} --list"
    try:
        with path.open("rb") as dump:
            listed = subprocess.run(list(verify) or [pg_restore, "--list", str(path)], capture_output=True,
                                    stdin=dump if verify else subprocess.DEVNULL, env=child_env.backup(),
                                    timeout=_LIST_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupFailed(f"`{label}` did not finish: {_why(exc)}") from None
    if listed.returncode != 0:
        reason = _tail(listed.stderr.decode("utf-8", "replace"), password=password)
        raise BackupFailed(f"`{label}` cannot read the dump: {reason or 'no message'}")
    # A readable schema-only or data-only archive cannot recover the old
    # database on its own. The startup gate requires both in one archive.
    kinds = set(re.findall(rb"(?m)^\d+;\s+\d+\s+\d+\s+(TABLE DATA|TABLE)\s", listed.stdout))
    if kinds != {b"TABLE", b"TABLE DATA"}:
        raise BackupFailed("the archive is not a full database backup (table definitions and data are required)")
    return f"listed with {label}"
