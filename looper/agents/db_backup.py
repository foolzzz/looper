"""A ``pg_dump`` of Looper's database before ``looper serve`` changes its schema.

``looper serve`` applies ``schema.sql`` at every start, and the file holds
migrations that drop columns and tables. When the schema is about to change
(``schema_change_pending``), serve first writes a custom-format dump into
``<runtime root>/backups/`` and keeps the newest ``KEEP`` of that database's;
without a way to make one it does not start (``--skip-backup`` overrides).

The dump is made by, in order of precedence:

- ``LOOPER_BACKUP_CMD``: a command run as given whose stdout is the dump, for a
  ``pg_dump`` that cannot reach ``LOOPER_DATABASE_URL`` as written, such as the
  one inside a PostgreSQL container; ``{dbname}`` and ``{user}`` are filled in
  from the database URL. Its version is not checked, and the dump is checked
  by its header only;
- the ``pg_dump`` that ``LOOPER_PG_DUMP`` names, else ``pg_dump`` on PATH. Its
  major version must be at least the server's; the dump is then listed with
  the ``pg_restore`` beside it (else the one on PATH) to prove it is readable.
"""

from __future__ import annotations

import datetime
import getpass
import os
import re
import shlex
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .. import env as looper_env

PG_DUMP_ENV = "LOOPER_PG_DUMP"
BACKUP_CMD_ENV = "LOOPER_BACKUP_CMD"
BACKUP_DIRNAME = "backups"
KEEP = 5

_VERSION_TIMEOUT_SECONDS = 15.0
_DUMP_TIMEOUT_SECONDS = 3600.0
_LIST_TIMEOUT_SECONDS = 300.0
_MAGIC = b"PGDMP"  # how every custom-format archive starts
_STAMP = "%Y%m%dT%H%M%S.%fZ"
_STAMP_PATTERN = r"\d{8}T\d{6}\.\d{6}Z"
_PG_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)")
_PLACEHOLDER = re.compile(r"\{(dbname|user)\}")


class NoDumper(Exception):
    """No usable way to make a backup; the message says why."""


class BackupFailed(Exception):
    """The backup was not made; the message says why. Nothing is left behind."""


@dataclass(frozen=True)
class Dumper:
    """How a backup is made."""

    argv: tuple[str, ...]  # the pg_dump program, or the whole LOOPER_BACKUP_CMD
    command: bool  # LOOPER_BACKUP_CMD: run as given (placeholders filled in)
    detail: str  # what doctor and the log show

    @property
    def pg_restore(self) -> str | None:
        """The pg_restore that lists this pg_dump's backups: the one beside it, else on PATH."""
        if self.command:
            return None
        sibling = Path(self.argv[0]).with_name("pg_restore")
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
        return shutil.which("pg_restore")


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def find_dumper(server_major: int | None) -> Dumper:
    """The way backups are made here; ``NoDumper`` when there is none or ``pg_dump`` is older than the server."""
    template = (looper_env.get(BACKUP_CMD_ENV) or "").strip()
    if template:
        try:
            argv = shlex.split(template)
        except ValueError as exc:
            raise NoDumper(f"{BACKUP_CMD_ENV} cannot be parsed: {exc}") from None
        return Dumper(tuple(argv), True, f"{BACKUP_CMD_ENV}: {template} (version not checked)")
    configured = (looper_env.get(PG_DUMP_ENV) or "").strip()
    program = shutil.which(configured or "pg_dump")
    if program is None:
        raise NoDumper(f"{PG_DUMP_ENV}={configured} is not an executable" if configured
                       else "`pg_dump` is not on PATH")
    try:
        version = subprocess.run([program, "--version"], capture_output=True, text=True,
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
    return Dumper((program,), False, f"{line} ({program})")


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


def prune(backup_dir: Path, dbname: str, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` backups of ``dbname`` (by the time in their names); the deleted ones."""
    pattern = re.compile(re.escape(_file_stem(dbname)) + rf"-({_STAMP_PATTERN})\.dump")
    backups = sorted(
        (match.group(1), path) for path in backup_dir.iterdir()
        if path.is_file() and (match := pattern.fullmatch(path.name))
    )
    old = [path for _, path in backups[:max(len(backups) - keep, 0)]]
    for path in old:
        path.unlink()
    return old


def _tail(text: str, *, password: str | None, lines: int = 5) -> str:
    tail = " | ".join(line.strip() for line in text.strip().splitlines()[-lines:] if line.strip())
    return tail.replace(password, "***") if password else tail


def back_up(dumper: Dumper, dsn: str, backup_dir: Path, *, now: datetime.datetime | None = None) -> tuple[Path, str]:
    """Write a verified dump of ``dsn`` into ``backup_dir`` (mode 0600), then prune; (its path, how it was checked).

    ``BackupFailed`` when the dump fails or is not a readable custom-format
    archive; the partial file is removed.
    """
    bare_dsn, password, dbname, user = _connection(dsn)
    if dumper.command:
        argv = [_PLACEHOLDER.sub(lambda m: dbname if m.group(1) == "dbname" else user, arg) for arg in dumper.argv]
        env = dict(os.environ)
    else:
        argv = [*dumper.argv, "--format=custom", "--no-password", f"--dbname={bare_dsn}"]
        env = {**os.environ, **({"PGPASSWORD": password} if password else {})}
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(_STAMP)
    path = backup_dir / f"{_file_stem(dbname)}-{stamp}.dump"
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            try:
                dump = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
                                      timeout=_DUMP_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BackupFailed(f"`{argv[0]}` did not finish: {exc}") from None
        if dump.returncode != 0:
            reason = _tail(dump.stderr.decode("utf-8", "replace"), password=password)
            raise BackupFailed(f"`{argv[0]}` exited with {dump.returncode}: {reason or 'no message'}")
        checked = _verify(partial, dumper.pg_restore, password=password)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    prune(backup_dir, dbname)
    return path, checked


def _verify(path: Path, pg_restore: str | None, *, password: str | None) -> str:
    with path.open("rb") as dump:
        head = dump.read(len(_MAGIC))
    if not head:
        raise BackupFailed("the dump is empty")
    if head != _MAGIC:
        raise BackupFailed("the dump is not a custom-format archive (does the command pass --format=custom?)")
    if pg_restore is None:
        return "header checked; no pg_restore to list it"
    try:
        listed = subprocess.run([pg_restore, "--list", str(path)], capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=_LIST_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupFailed(f"`{pg_restore} --list` did not finish: {exc}") from None
    if listed.returncode != 0:
        reason = _tail(listed.stderr.decode("utf-8", "replace"), password=password)
        raise BackupFailed(f"`{pg_restore} --list` cannot read the dump: {reason or 'no message'}")
    return f"listed with {pg_restore}"
