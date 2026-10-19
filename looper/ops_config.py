"""Looper's deployment settings: the ops file, ``<runtime root>/ops/server.toml``, read once when a command starts.

::

    [database]
    url = "postgresql://looper:secret@127.0.0.1:5432/looper"

    [server]
    host = "127.0.0.1"
    port = 8767
    public_url = "https://looper.example.com"   # the dashboard's address in chat links
    log_level = "info"                          # debug, info, warning or error

    [backup]                                    # how the database is backed up before a schema change
    pg_dump = "/usr/lib/postgresql/17/bin/pg_dump"
    command = ["docker", "exec", "pg", "pg_dump", "--format=custom", "-U", "{user}", "{dbname}"]

    [operator_notes]
    root = "/home/me/.claude/projects"          # where the operator's Claude Code keeps its project notes

``looper serve``, ``doctor``, ``config import``/``export`` and ``service install`` read it with ``load`` before they
connect to the database or start anything. Each setting comes from the first of: an explicit command-line flag;
the file; then, for this release only, the variable it came from before (``LOOPER_X``, else ``LOOPX_X``), from the
environment, then from the checkout's ``.env``, each use warned about by name (never with the value); then the
built-in default. The database URL has no default: a command that needs it does not start without one.
``looper config init-ops`` writes the file from the values in effect, so an installation moves to it in one step.

The file is ``--config`` when given, else under the runtime root (``--runtime-root``, else ``LOOPER_RUNTIME_ROOT``
for this release, else ``~/.looper``), which the file itself never names. It holds the database password, so it
must be a regular file (never a symbolic link) owned by the user running Looper and private to them, in a directory
of theirs that no one else can enter (``ops/`` is 0700). An unknown key, a value of the wrong type or a bad value
stops the command with a message that names the key and never shows the value. Nothing is expanded: no variable,
no ``~``, no include.

``.env`` is no longer put into Looper's environment: only the settings above are read from it, and the names of
the other variables it holds are warned about.
"""

from __future__ import annotations

import os
import shlex
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .paths import RUNTIME_ROOT_ENV, home_runtime_root, settle_runtime_root

FILE = Path("ops") / "server.toml"
DOTENV = Path(__file__).resolve().parents[1] / ".env"  # the checkout's
# What a database URL defaulted to before the ops file; ``init-ops`` writes it when nothing else names one.
LEGACY_DEFAULT_DATABASE_URL = "postgresql://localhost:5432/loopx"
LOG_LEVELS = ("debug", "info", "warning", "error")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
_LEGACY_PREFIX = ("LOOPER_", "LOOPX_")


class OpsConfigError(ValueError):
    """The ops settings cannot be used; the message says why and what to do, and never shows a value."""


class Origin(StrEnum):
    """Where a setting in effect came from."""

    FLAG = "flag"
    FILE = "file"
    ENVIRONMENT = "environment"  # a variable of the environment, read for this release only
    DOTENV = ".env"  # a variable of the checkout's .env, read for this release only


@dataclass(frozen=True)
class Source:
    origin: Origin
    label: str  # what to call it: --port, database.url in <file>, LOOPER_DATABASE_URL, LOOPER_PG_DUMP in <.env>

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True)
class Flags:
    """What the command line gave; None for a flag it did not."""

    runtime_root: str | Path | None = None
    config: str | Path | None = None
    host: str | None = None
    port: int | None = None
    log_level: str | None = None


@dataclass(frozen=True)
class OpsConfig:
    """The settings in effect, and where each came from (``sources``; a setting at its default has none)."""

    runtime_root: Path
    path: Path  # the ops file, whether or not it exists
    database_url: str = field(default="", repr=False)  # "" only when the command does not need one
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    public_url: str = ""
    log_level: str = "info"
    pg_dump: str = ""
    backup_command: tuple[str, ...] = ()
    operator_notes_root: str = ""
    sources: Mapping[str, Source] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


# --- The settings --------------------------------------------------------------


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpsConfigError(f"{where} must be a non-empty string")
    return value.strip()


def _database_url(value: Any, where: str) -> str:
    value = _text(value, where)
    try:
        scheme = urlsplit(value).scheme
    except ValueError:
        scheme = ""
    if scheme not in ("postgresql", "postgres"):
        raise OpsConfigError(f"{where} must be a postgresql:// URL")
    return value


def _port(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise OpsConfigError(f"{where} must be a TCP port, an integer from 1 to 65535")
    return value


def _public_url(value: Any, where: str) -> str:
    value = _text(value, where)
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in ("http", "https") and bool(parts.hostname) and parts.port != 0
                 and not (parts.username or parts.password or parts.query or parts.fragment))
    except ValueError:  # a port that is not a number up to 65535
        valid = False
    if not valid:
        raise OpsConfigError(f"{where} must be the dashboard's http:// or https:// address, without a user, query or "
                             "fragment, such as https://looper.example.com")
    return value.rstrip("/")


def _log_level(value: Any, where: str) -> str:
    if value not in LOG_LEVELS:
        raise OpsConfigError(f"{where} must be one of {', '.join(LOG_LEVELS)}")
    return value


def _command(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(word, str) and word for word in value):
        raise OpsConfigError(f"{where} must be a non-empty array of non-empty strings, the program first")
    return tuple(value)


def _split(value: str, where: str) -> list[str]:
    """``LOOPER_BACKUP_CMD``: one string, split into words as a shell would."""
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise OpsConfigError(f"{where} cannot be split into words: {exc}") from None


def _absolute_path(value: Any, where: str) -> str:
    value = _text(value, where)
    if not os.path.isabs(value):
        raise OpsConfigError(f"{where} must be an absolute path")
    return value


@dataclass(frozen=True)
class _Setting:
    name: str  # the OpsConfig field
    section: str
    key: str
    check: Callable[[Any, str], Any]
    variable: str | None = None  # the LOOPER_* variable it came from before the file
    legacy_prefix: bool = True  # whether LOOPX_* of that variable is read too
    flag: str | None = None  # the Flags field
    # What a variable's text becomes before ``check`` (the file's values are checked as they are).
    from_variable: Callable[[str, str], Any] = lambda value, where: value

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.key}"

    @property
    def variables(self) -> tuple[str, ...]:
        if self.variable is None:
            return ()
        legacy = self.variable.replace(_LEGACY_PREFIX[0], _LEGACY_PREFIX[1], 1)
        return (self.variable, legacy) if self.legacy_prefix else (self.variable,)


SETTINGS = (
    _Setting("database_url", "database", "url", _database_url, variable="LOOPER_DATABASE_URL"),
    _Setting("host", "server", "host", _text, flag="host"),
    _Setting("port", "server", "port", _port, flag="port"),
    _Setting("public_url", "server", "public_url", _public_url, variable="LOOPER_PUBLIC_URL"),
    _Setting("log_level", "server", "log_level", _log_level, flag="log_level"),
    _Setting("pg_dump", "backup", "pg_dump", _text, variable="LOOPER_PG_DUMP"),
    _Setting("backup_command", "backup", "command", _command, variable="LOOPER_BACKUP_CMD", from_variable=_split),
    _Setting("operator_notes_root", "operator_notes", "root", _absolute_path, variable="LOOPER_OPERATOR_NOTES_ROOT",
             legacy_prefix=False, from_variable=lambda value, where: os.path.expanduser(value.strip())),
)
_SECTIONS: dict[str, dict[str, _Setting]] = {}
for _setting in SETTINGS:
    _SECTIONS.setdefault(_setting.section, {})[_setting.key] = _setting
# The variables an ops setting came from, as ``service install --env`` must not copy them.
VARIABLES = frozenset(name for setting in SETTINGS for name in setting.variables)


# --- Where the file is -----------------------------------------------------------


def runtime_root(flag: str | Path | None = None, environ: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """The runtime root, made absolute, and a deprecation warning when a variable named it (else "").

    ``--runtime-root``, else ``LOOPER_RUNTIME_ROOT`` (``LOOPX_RUNTIME_ROOT``) for this release, else ``~/.looper``.
    A blank variable names nothing.
    """
    environ = os.environ if environ is None else environ
    if flag is not None and str(flag).strip():
        return _absolute(flag), ""
    for name in (RUNTIME_ROOT_ENV, RUNTIME_ROOT_ENV.replace(*_LEGACY_PREFIX, 1)):
        if (value := (environ.get(name) or "").strip()):
            return _absolute(value), (f"{name} is deprecated: pass --runtime-root instead (`looper service install` "
                                      "writes it into the service); the variable is read until the next release")
    return home_runtime_root(), ""


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def config_path(flag: str | Path | None, root: Path) -> Path:
    """``--config`` made absolute, else the file under ``root``."""
    return _absolute(flag) if flag is not None and str(flag).strip() else root / FILE


# --- Reading ----------------------------------------------------------------------


def read_file(path: Path, *, required: bool = False) -> dict[str, Any]:
    """The settings ``path`` holds, by ``OpsConfig`` field, checked; {} when there is no file and none is required."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise OpsConfigError(f"--config {path}: no such file (`looper config init-ops --config {path}` writes "
                                 "one)") from None
        return {}
    except OSError as exc:
        raise OpsConfigError(f"cannot read {path}: {exc.strerror}") from None
    if stat.S_ISLNK(found.st_mode):
        raise OpsConfigError(f"{path} is a symbolic link; the ops file must be the file itself")
    if not stat.S_ISREG(found.st_mode):
        raise OpsConfigError(f"{path} is not a regular file")
    _check_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise OpsConfigError(f"cannot read {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OpsConfigError(f"{path} is not a regular file")
        _check_owner(path, opened)
        if opened.st_mode & 0o077:
            raise OpsConfigError(f"{path} has mode {stat.S_IMODE(opened.st_mode):04o}: others can read it, and it can "
                                 f"hold the database password; run `chmod 600 {path}`")
        data = handle.read()
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise OpsConfigError(f"{path} is not UTF-8 text") from None
    except tomllib.TOMLDecodeError as exc:
        raise OpsConfigError(f"{path} is not valid TOML: {exc}") from None
    values: dict[str, Any] = {}
    for section, body in table.items():
        settings = _SECTIONS.get(section)
        if settings is None:
            raise OpsConfigError(f"{path}: unknown section [{section}]; the sections are "
                                 f"{', '.join(f'[{name}]' for name in _SECTIONS)}")
        if not isinstance(body, dict):
            raise OpsConfigError(f"{path}: {section} must be a table, [{section}]")
        for key, value in body.items():
            setting = settings.get(key)
            if setting is None:
                raise OpsConfigError(f"{path}: unknown key {section}.{key}; [{section}] takes "
                                     f"{', '.join(settings)}")
            values[setting.name] = setting.check(value, f"{setting.dotted} in {path}")
    return values


def _check_owner(path: Path, found: os.stat_result) -> None:
    if found.st_uid != os.geteuid():
        raise OpsConfigError(f"{path} belongs to uid {found.st_uid}, not to the user running Looper "
                             f"(uid {os.geteuid()})")


def _check_directory(directory: Path) -> None:
    try:
        found = os.lstat(directory)
    except OSError as exc:
        raise OpsConfigError(f"cannot read {directory}: {exc.strerror}") from None
    if stat.S_ISLNK(found.st_mode):
        raise OpsConfigError(f"{directory} is a symbolic link; keep the ops file in a real directory")
    if not stat.S_ISDIR(found.st_mode):
        raise OpsConfigError(f"{directory} is not a directory")
    _check_owner(directory, found)
    if found.st_mode & 0o077:
        raise OpsConfigError(f"{directory} has mode {stat.S_IMODE(found.st_mode):04o}: others can enter it; keep the "
                             f"ops file in a directory only you can enter (`chmod 700 {directory}`, or move the file)")


def read_dotenv(path: Path = DOTENV) -> dict[str, str]:
    """``KEY=VALUE`` lines of ``path`` (blank lines and ``#`` comments skipped, the first of a key kept); {} without
    the file. Nothing is unquoted or expanded."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise OpsConfigError(f"cannot read {path}: {exc}") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip():
            values.setdefault(key.strip(), value.strip())
    return values


def load(flags: Flags = Flags(), *, environ: Mapping[str, str] | None = None, dotenv: Path = DOTENV,
         require_database: bool = True, require_file: bool = True) -> OpsConfig:
    """The settings in effect for a command with ``flags``; ``OpsConfigError`` when they cannot be used, or when
    ``require_database`` and nothing names a database. A ``--config`` file must exist unless not ``require_file``
    (``init-ops``, about to write it)."""
    environ = os.environ if environ is None else environ
    root, root_warning = runtime_root(flags.runtime_root, environ)
    path = config_path(flags.config, root)
    in_file = read_file(path, required=require_file and flags.config is not None and bool(str(flags.config).strip()))
    legacy = read_dotenv(dotenv)
    warnings = [root_warning] if root_warning else []
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for setting in SETTINGS:
        flag = getattr(flags, setting.flag) if setting.flag else None
        if flag is not None:
            values[setting.name] = setting.check(flag, f"--{setting.key.replace('_', '-')}")
            sources[setting.name] = Source(Origin.FLAG, f"--{setting.key.replace('_', '-')}")
            continue
        if setting.name in in_file:
            values[setting.name] = in_file[setting.name]
            sources[setting.name] = Source(Origin.FILE, f"{setting.dotted} in {path}")
            shadowed = [name for name in setting.variables if (environ.get(name) or "").strip()]
            if shadowed:
                warnings.append(f"{', '.join(shadowed)} is set but not used: {setting.dotted} in {path} is; remove "
                                "the variable")
            continue
        for origin, where, found in ((Origin.ENVIRONMENT, "the environment", environ),
                                     (Origin.DOTENV, str(dotenv), legacy)):
            name = next((name for name in setting.variables if (found.get(name) or "").strip()), None)
            if name is None:
                continue
            label = name if origin is Origin.ENVIRONMENT else f"{name} in {dotenv}"
            values[setting.name] = setting.check(setting.from_variable(found[name], label), label)
            sources[setting.name] = Source(origin, label)
            warnings.append(f"{name} in {where} is deprecated: set {setting.dotted} in {path} instead (`looper "
                            "config init-ops` writes the file from the values in effect); it is read until the next "
                            "release")
            break
    if ignored := sorted(set(legacy) - VARIABLES):
        warnings.append(f"{dotenv}: not read: {', '.join(ignored)}. Looper reads only its old settings from .env "
                        "and no longer puts the file's variables into its environment: give a channel's secret its "
                        "Settings field, or set the variable in the server's environment (`looper service install "
                        "--env NAME`)")
    if require_database and "database_url" not in values:
        raise OpsConfigError(f"no database configured: set [database] url in {path} (`looper config init-ops` "
                             "writes the file; edit its url)")
    return OpsConfig(runtime_root=root, path=path, sources=sources, warnings=tuple(warnings), **values)


# --- Writing ---------------------------------------------------------------------


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string: quotes, backslashes and control characters escaped, every other character
    as it is (a JSON escape of a character outside the BMP is a surrogate pair, which TOML refuses)."""
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise OpsConfigError("a setting holds a byte that is not text (an undecodable variable?); fix where it comes "
                             "from")
    return '"' + "".join(_toml_char(char) for char in value) + '"'


def _toml_char(char: str) -> str:
    if char in '"\\':
        return "\\" + char
    return f"\\u{ord(char):04X}" if ord(char) < 0x20 or ord(char) == 0x7F else char


def render(config: OpsConfig) -> str:
    """The ops file holding ``config``'s settings that are not defaults (and the database URL), the others as
    comments."""
    database_url = config.database_url or LEGACY_DEFAULT_DATABASE_URL
    lines = [
        "# Looper's deployment settings, read when `looper serve` starts: restart it after a change.",
        "# It holds the database password: keep it mode 0600, in a directory only you can enter.",
        "",
        "[database]",
        f"url = {_toml_string(database_url)}",
        "",
        "[server]",
        f"host = {_toml_string(config.host)}" if config.host != DEFAULT_HOST else f'# host = "{DEFAULT_HOST}"',
        f"port = {config.port}" if config.port != DEFAULT_PORT else f"# port = {DEFAULT_PORT}",
        f"public_url = {_toml_string(config.public_url)}" if config.public_url
        else '# public_url = "https://looper.example.com"',
        f"log_level = {_toml_string(config.log_level)}" if config.log_level != "info" else '# log_level = "info"',
        "",
        "[backup]",
        f"pg_dump = {_toml_string(config.pg_dump)}" if config.pg_dump else '# pg_dump = "/path/to/pg_dump"',
        (f"command = [{', '.join(_toml_string(word) for word in config.backup_command)}]" if config.backup_command
         else '# command = ["docker", "exec", "<container>", "pg_dump", "--format=custom", "-U", "{user}", '
              '"{dbname}"]'),
        "",
        "[operator_notes]",
        f"root = {_toml_string(config.operator_notes_root)}" if config.operator_notes_root
        else '# root = "/absolute/path/to/claude/projects"',
        "",
    ]
    return "\n".join(lines)


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` (mode 0600, in a 0700 directory of its own when Looper makes it), replacing any
    file in one step: a reader sees the old file or the new one, never part of one."""
    directory = path.parent
    if not os.path.lexists(directory):
        os.makedirs(directory, mode=0o700)
        os.chmod(directory, 0o700)  # makedirs' mode is masked by the umask
    _check_directory(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")  # O_EXCL, mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def init_file(flags: Flags = Flags(), *, environ: Mapping[str, str] | None = None,
              dotenv: Path = DOTENV) -> tuple[OpsConfig, list[str]]:
    """``looper config init-ops``: write the ops file from the settings in effect; what was written, and what to
    say about it (never a value). ``OpsConfigError`` when the file exists already."""
    root, _ = runtime_root(flags.runtime_root, environ)
    settled = settle_runtime_root(root)  # the first write under ~/.looper while only ~/.loopx exists moves it first
    root = settled.root
    path = config_path(flags.config, root)
    if os.path.lexists(path):
        raise OpsConfigError(f"{path} exists already: edit it (`looper serve` reads it at every start)")
    config = load(replace(flags, runtime_root=root), environ=environ, dotenv=dotenv, require_database=False,
                  require_file=False)
    write_private(path, render(config))
    written = load(Flags(runtime_root=root, config=path), environ={}, dotenv=Path(os.devnull))
    if written.database_url != (config.database_url or LEGACY_DEFAULT_DATABASE_URL):  # never expected
        raise OpsConfigError(f"{path} was written but does not read back as written; remove it and try again")
    notes = [settled.note] if settled.note else []
    notes += [f"{setting.dotted} from {config.sources[setting.name]}" for setting in SETTINGS
              if setting.name in config.sources]
    if "database_url" not in config.sources:
        notes.append(f"database.url: nothing named a database, so it is the old built-in default "
                     f"{LEGACY_DEFAULT_DATABASE_URL}; check it")
    return written, notes
