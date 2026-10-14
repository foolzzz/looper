"""What a machine still needs before LoopX can run, and how to fix it.

``loopx doctor`` prints these checks and exits 1 when one failed. ``loopx
serve`` runs the same checks first: an unreachable PostgreSQL stops it; a
missing Claude Code CLI or login is logged as a warning, the dashboard still
starts, and ``GET /api/status`` reports every result under ``checks``. The
``pg_dump`` check matters only when the schema is about to change: serve then
backs the database up first and does not start without a way to
(``db_backup``); doctor reports it as a warning otherwise.

Nothing here spends money: the Claude login is read with
``claude auth status``, which reads the local credentials and makes no model
call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass

from . import db_backup

logger = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# The install command and documentation the Claude Code package's own README names.
CLAUDE_INSTALL_FIX = (
    "install Claude Code: npm install -g @anthropic-ai/claude-code "
    "(docs: https://code.claude.com/docs/en/overview)"
)
CLAUDE_LOGIN_FIX = "run `claude auth login`, or export ANTHROPIC_API_KEY for loopx"
DATABASE_FIX = (
    "start PostgreSQL if it is not running, for example with Docker:\n"
    "  docker run -d --name agentbox-postgres -e POSTGRES_DB=loopx -e POSTGRES_USER=postgres "
    "-e POSTGRES_PASSWORD=postgres -p 54329:5432 postgres:16\n"
    "then point LoopX at it with LOOPX_DATABASE_URL (in the environment or .env):\n"
    '  export LOOPX_DATABASE_URL="postgresql://postgres:postgres@localhost:54329/loopx"'
)
LARK_CLI_FIX = "install lark-cli and sign in, or turn the Lark channel off on the Settings page"

_COMMAND_TIMEOUT_SECONDS = 15.0
_DATABASE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    status: str  # OK, WARN, FAIL or SKIP (not checked, with the reason in detail)
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def mask_dsn(dsn: str) -> str:
    """``dsn`` with its password, in the user part or the query, replaced by ``***``."""
    try:
        parts = urllib.parse.urlsplit(dsn)
        has_password = parts.password is not None
    except ValueError:
        return "<unparseable database URL>"
    netloc = parts.netloc
    if has_password:
        userinfo, _, host = netloc.rpartition("@")
        netloc = f"{userinfo.partition(':')[0]}:***@{host}"
    query = re.sub(r"(?i)(^|&)(password=)[^&]*", r"\1\2***", parts.query)
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=query))


def _without_password(text: str, dsn: str) -> str:
    try:
        password = urllib.parse.urlsplit(dsn).password
    except ValueError:
        password = None
    return text.replace(password, "***") if password else text


async def check_database(dsn: str) -> Check:
    """Can LoopX open a connection to ``dsn``?"""
    import asyncpg

    shown = mask_dsn(dsn)
    try:
        conn = await asyncpg.connect(dsn, timeout=_DATABASE_TIMEOUT_SECONDS)
    except Exception as exc:  # any failure to connect is this check's answer
        reason = _without_password(f"{type(exc).__name__}: {exc}".strip(), dsn)
        return Check("database", "PostgreSQL", FAIL, f"cannot connect to {shown} ({reason})", DATABASE_FIX)
    await conn.close()
    return Check("database", "PostgreSQL", OK, shown)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=_COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def check_claude(claude_bin: str = "claude") -> list[Check]:
    """Is the Claude Code CLI installed, and is it signed in? Two checks, CLI first."""
    path = shutil.which(claude_bin)
    if path is None:
        return [
            Check("claude_cli", "Claude Code CLI", FAIL, f"`{claude_bin}` is not on PATH", CLAUDE_INSTALL_FIX),
            Check("claude_login", "Claude login", SKIP, "not checked: no Claude Code CLI"),
        ]
    version = _run([path, "--version"])
    if version is None or version.returncode != 0:
        reason = _first_line(version.stderr or version.stdout) if version else "did not finish"
        return [
            Check("claude_cli", "Claude Code CLI", FAIL, f"`{claude_bin} --version` failed: {reason}",
                  CLAUDE_INSTALL_FIX),
            Check("claude_login", "Claude login", SKIP, "not checked: the Claude Code CLI does not run"),
        ]
    return [Check("claude_cli", "Claude Code CLI", OK, _first_line(version.stdout)), _check_login(path)]


def _check_login(path: str) -> Check:
    # `--help` never calls a model. A CLI without `auth status` would read the
    # words as a prompt, so the subcommand is confirmed before it is run.
    usage = _run([path, "auth", "--help"])
    if usage is None or "Usage: claude auth" not in usage.stdout or "status" not in usage.stdout:
        return Check("claude_login", "Claude login", SKIP,
                     "not checked: this Claude Code has no `claude auth status`, and a model call would cost money")
    status = _run([path, "auth", "status", "--json"])
    try:
        payload = json.loads(status.stdout) if status else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("loggedIn"), bool):
        reason = _first_line(status.stderr) if status else "did not finish"
        return Check("claude_login", "Claude login", SKIP,
                     f"not checked: `claude auth status` gave no answer ({reason or 'no JSON'})")
    if payload["loggedIn"]:
        return Check("claude_login", "Claude login", OK, f"logged in ({payload.get('authMethod') or 'unknown method'})")
    return Check("claude_login", "Claude login", FAIL, "not logged in", CLAUDE_LOGIN_FIX)


def check_lark_cli(lark_enabled: bool | None) -> Check:
    """Does the enabled Lark channel have ``lark-cli``? ``None``: whether it is enabled is unknown."""
    if lark_enabled is None:
        return Check("lark_cli", "Lark channel", SKIP,
                     "not checked: whether Lark is enabled is stored in PostgreSQL, which is unreachable")
    if not lark_enabled:
        return Check("lark_cli", "Lark channel", SKIP, "not enabled")
    if shutil.which("lark-cli"):
        return Check("lark_cli", "Lark channel", OK, "enabled, lark-cli found")
    return Check("lark_cli", "Lark channel", WARN, "enabled, but `lark-cli` is not on PATH; it will not start",
                 LARK_CLI_FIX)


def pg_dump_fix(server_major: int | None) -> str:
    """How to give LoopX a pg_dump for the server, or do without one."""
    version = f"{server_major} or newer" if server_major else "of at least the server's major version"
    install = (
        'macOS: brew install libpq, then put it on PATH: export PATH="$(brew --prefix libpq)/bin:$PATH"'
        if sys.platform == "darwin" else
        f"Debian/Ubuntu: apt install postgresql-client-{server_major or '<major>'} (from apt.postgresql.org)"
    )
    return (
        f"install pg_dump {version}. {install}\n"
        f"or set {db_backup.PG_DUMP_ENV} to a pg_dump; with PostgreSQL in Docker, dump inside its container:\n"
        f"  export {db_backup.BACKUP_CMD_ENV}='docker exec <container> pg_dump --format=custom -U {{user}} {{dbname}}'\n"
        "or migrate once without a backup: loopx serve --skip-backup"
    )


def check_pg_dump(server_version_num: int | None, pending: bool | None) -> tuple[Check, db_backup.Dumper | None]:
    """Can LoopX back the database up before it changes the schema? With the way it would, when it can.

    ``pending``: whether the schema is about to change (``None``: unknown).
    Without a way to back up, ``loopx serve`` refuses only then, so only then
    is this a failure.
    """
    server_major = server_version_num // 10000 if server_version_num else None
    try:
        dumper = db_backup.find_dumper(server_major)
    except db_backup.NoDumper as exc:
        if pending:
            return Check("pg_dump", "pg_dump", FAIL,
                         f"{exc}; the database schema is about to change, and LoopX backs it up first",
                         pg_dump_fix(server_major)), None
        return Check("pg_dump", "pg_dump", WARN,
                     f"{exc}; after an upgrade that changes the database schema, `loopx serve` backs the database "
                     "up first and will not start without it",
                     pg_dump_fix(server_major)), None
    unchecked = "" if server_major or dumper.command else "; not compared with the server, which is unreachable"
    return Check("pg_dump", "pg_dump", OK, dumper.detail + unchecked), dumper


@dataclass(frozen=True)
class _DatabaseFacts:
    lark_enabled: bool | None = None
    server_version_num: int | None = None
    schema_change_pending: bool | None = None


async def _database_facts(dsn: str) -> _DatabaseFacts:
    import asyncpg

    from ..db.channel_configs import get_config
    from ..db.connection import schema_change_pending

    try:
        conn = await asyncpg.connect(dsn, timeout=_DATABASE_TIMEOUT_SECONDS)
    except Exception:
        return _DatabaseFacts()
    try:
        try:
            row = await get_config("channel-lark", pool=conn)  # a connection answers like a pool here
        except asyncpg.UndefinedTableError:  # a database `loopx serve` has not set up yet
            row = None
        return _DatabaseFacts(
            lark_enabled=bool(row and row.get("enabled")),
            server_version_num=int(await conn.fetchval("SELECT current_setting('server_version_num')")),
            schema_change_pending=await schema_change_pending(conn),
        )
    finally:
        await conn.close()


async def run_checks(*, dsn: str, claude_bin: str = "claude") -> list[Check]:
    """Every check, reading only what it can: the channel settings and the schema state need the database."""
    database = await check_database(dsn)
    facts = await _database_facts(dsn) if database.status == OK else _DatabaseFacts()
    claude = await asyncio.to_thread(check_claude, claude_bin)
    pg_dump, _ = await asyncio.to_thread(check_pg_dump, facts.server_version_num, facts.schema_change_pending)
    return [database, *claude, check_lark_cli(facts.lark_enabled), pg_dump]


def format_checks(checks: list[Check]) -> str:
    width = max(len(check.label) for check in checks)
    lines = []
    for check in checks:
        lines.append(f"{check.status:<5} {check.label:<{width}}  {check.detail}")
        if check.fix and check.status != OK:
            fix = check.fix.splitlines()
            lines.append(f"      fix: {fix[0]}")
            lines.extend(f"           {line}" for line in fix[1:])
    return "\n".join(lines)


def log_problems(checks: list[Check]) -> None:
    """Log every check that is not ok or skipped, loudly."""
    for check in checks:
        if check.status in (FAIL, WARN):
            consequence = " Agents cannot run until this is fixed." if check.id.startswith("claude") else ""
            logger.warning("SELF-CHECK %s %s: %s.%s Fix: %s", check.status.upper(), check.label, check.detail,
                           consequence, check.fix.replace("\n", " "))


def doctor(claude_bin: str = "claude") -> int:
    """``loopx doctor``: print every check, and whether the background service runs; 1 when one failed."""
    from ..db.connection import configured_dsn
    from .service import doctor_check

    checks = [*asyncio.run(run_checks(dsn=configured_dsn(), claude_bin=claude_bin)), doctor_check()]
    print(format_checks(checks))
    return 1 if any(check.status == FAIL for check in checks) else 0
