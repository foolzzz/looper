"""What a machine still needs before Looper can run, and how to fix it.

``looper doctor`` prints these checks and exits 1 when one failed. ``looper
serve`` runs the same checks first: an unreachable PostgreSQL stops it; a
missing Claude Code CLI or login is logged as a warning, the dashboard still
starts, and ``GET /api/status`` reports every result under ``checks``. The
``pg_dump`` check matters only when the schema is about to change: serve then
backs the database up first and does not start without a way to
(``db_backup``); doctor reports it as a warning otherwise.

Nothing here spends money: the Claude login is read with
``claude auth status``, which reads the local credentials and makes no model
call. The Codex login (``codex_login``) is checked only when an Agent Profile
uses it, with ``codex login status`` in a probe home; the Grok login
(``grok_login``) likewise, by whether its login file is there. A forked login
that a run recorded is a failure until the operator clears it.

The git checks matter for projects whose parallel steps work in worktrees of
their own (``step_workspaces``): git 2.40 or later (serve checks it too), and,
in doctor only, whether each such project's default directory can have them
now (``step_workspaces.inspect``) and has the paths it links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import os
import shutil
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db_backup

if TYPE_CHECKING:
    from .runtimes import grok_login
    from .runtimes.codex_login import LoginStatus

logger = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# The install command and documentation the Claude Code package's own README names.
CLAUDE_INSTALL_FIX = (
    "install Claude Code: npm install -g @anthropic-ai/claude-code "
    "(docs: https://code.claude.com/docs/en/overview)"
)
CLAUDE_LOGIN_FIX = "run `claude auth login`, or export ANTHROPIC_API_KEY for looper"
DATABASE_FIX = (
    "start PostgreSQL if it is not running, for example with Docker:\n"
    "  docker run -d --name agentbox-postgres -e POSTGRES_DB=loopx -e POSTGRES_USER=postgres "
    "-e POSTGRES_PASSWORD=postgres -p 54329:5432 postgres:16\n"
    "then point Looper at it with LOOPER_DATABASE_URL (in the environment or .env):\n"
    '  export LOOPER_DATABASE_URL="postgresql://postgres:postgres@localhost:54329/loopx"'
)
LARK_CLI_FIX = "install lark-cli and sign in, or turn the Lark channel off on the Settings page"
CODEX_LOGIN_FIX = "run `codex login` and sign in with ChatGPT, or give the Agent Profiles an API key provider"
GROK_LOGIN_FIX = "run `grok login` on this machine, or give the Agent Profiles another runtime"
GIT_FIX = "install git 2.40 or later: https://git-scm.com/downloads"
STEP_WORKTREES_FIX = ("put the project's default directory on a branch of a git checkout with nothing in progress, "
                      "or set its parallel steps to share the task directory")

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
    """Can Looper open a connection to ``dsn``?"""
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


def check_codex_login(profiles: int | None, problem: dict[str, str] | None, probe: Callable[[], LoginStatus]) -> Check:
    """Can the Agent Profiles that use Codex's own login run? ``profiles``: how many there are (``None``:
    unknown); ``problem``: the forked login a run recorded; ``probe`` reads ``codex login status``."""
    from .runtimes.codex_login import PROBLEM_FIX, LoginState

    if profiles is None:
        return Check("codex_login", "Codex login", SKIP,
                     "not checked: the Agent Profiles are stored in PostgreSQL, which is unreachable")
    if not profiles:
        return Check("codex_login", "Codex login", SKIP, "not checked: no Agent Profile uses the Codex login")
    if problem is not None:
        return Check("codex_login", "Codex login", FAIL,
                     f"unavailable since {problem.get('at') or '?'}: {problem.get('detail')}", PROBLEM_FIX)
    status = probe()
    if status.state is LoginState.CHATGPT:
        return Check("codex_login", "Codex login", OK, status.detail)
    if status.state is LoginState.UNKNOWN:
        return Check("codex_login", "Codex login", WARN, f"not checked: {status.detail}", CODEX_LOGIN_FIX)
    return Check("codex_login", "Codex login", FAIL, status.detail, CODEX_LOGIN_FIX)


async def codex_login_check() -> Check:
    """``check_codex_login`` with the server's pool, read now (``/api/status`` asks on every poll; the probe's
    answer is reused for a while)."""
    from ..db import profile_registry
    from .runtimes import codex_login

    try:
        profiles = await profile_registry.login_profile_count("codex")
        problem = await codex_login.recorded_problem()
    except Exception:
        logger.warning("cannot read whether an Agent Profile uses the Codex login", exc_info=True)
        profiles, problem = None, None
    return await asyncio.to_thread(check_codex_login, profiles, problem, codex_login.cached_probe)


def check_grok_login(profiles: int | None, problem: dict[str, str] | None,
                     probe: Callable[[], grok_login.LoginStatus]) -> Check:
    """Can the Agent Profiles that use Grok's own login run? ``profiles``: how many there are (``None``: unknown);
    ``problem``: the forked login a run recorded; ``probe`` says whether the login file is there."""
    from .runtimes.grok_login import PROBLEM_FIX, LoginState

    if profiles is None:
        return Check("grok_login", "Grok login", SKIP,
                     "not checked: the Agent Profiles are stored in PostgreSQL, which is unreachable")
    if not profiles:
        return Check("grok_login", "Grok login", SKIP, "not checked: no Agent Profile uses the Grok login")
    if problem is not None:
        return Check("grok_login", "Grok login", FAIL,
                     f"unavailable since {problem.get('at') or '?'}: {problem.get('detail')}", PROBLEM_FIX)
    status = probe()
    if status.state is LoginState.LOGGED_IN:
        return Check("grok_login", "Grok login", OK, status.detail)
    return Check("grok_login", "Grok login", FAIL, status.detail, GROK_LOGIN_FIX)


async def grok_login_check() -> Check:
    """``check_grok_login`` with the server's pool, read now (``/api/status`` asks on every poll)."""
    from ..db import profile_registry
    from .runtimes import grok_login

    try:
        profiles = await profile_registry.login_profile_count("grok")
        problem = await grok_login.recorded_problem()
    except Exception:
        logger.warning("cannot read whether an Agent Profile uses the Grok login", exc_info=True)
        profiles, problem = None, None
    return await asyncio.to_thread(check_grok_login, profiles, problem, grok_login.cached_probe)


# The checks of the runtimes' own logins, by id: what reads each now (``/api/status`` reads them on every poll).
LOGIN_CHECKS = {"codex_login": codex_login_check, "grok_login": grok_login_check}


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


def check_git() -> Check:
    """Is git there, and new enough for step worktrees? Without it, Looper still runs."""
    from .step_workspaces import GIT_MIN_VERSION

    needed = ".".join(map(str, GIT_MIN_VERSION))
    consequence = "projects whose parallel steps work in worktrees run their steps one at a time"
    path = shutil.which("git")
    if path is None:
        return Check("git", "git", WARN, f"`git` is not on PATH: {consequence}", GIT_FIX)
    version = _run([path, "--version"])
    match = re.search(r"(\d+)\.(\d+)", version.stdout) if version and version.returncode == 0 else None
    if match is None:
        return Check("git", "git", WARN, f"`git --version` gave no version: {consequence}", GIT_FIX)
    if (int(match.group(1)), int(match.group(2))) < GIT_MIN_VERSION:
        return Check("git", "git", WARN, f"{_first_line(version.stdout)} is older than {needed}: {consequence}",
                     GIT_FIX)
    return Check("git", "git", OK, _first_line(version.stdout))


async def check_step_worktrees(projects: Sequence[Mapping[str, Any]] | None) -> Check:
    """Can the projects whose parallel steps work in worktrees have them in their default directories now, and do
    those have the paths they link? ``projects``: such projects' rows (``None``: unknown)."""
    from . import step_workspaces as ws

    label = "Step worktrees"
    if projects is None:
        return Check("step_workspaces", label, SKIP,
                     "not checked: the projects are stored in PostgreSQL, which is unreachable")
    if not projects:
        return Check("step_workspaces", label, SKIP, "no project runs its parallel steps in worktrees")
    problems: list[str] = []
    fine: list[str] = []
    for project in projects:
        name, work_dir = project.get("name") or project.get("id"), str(project.get("work_dir") or "")
        if not work_dir:
            fine.append(f"{name}: no default directory, each task's is checked when it runs")
            continue
        found = await ws.inspect(work_dir)
        if isinstance(found, ws.Unusable):
            effect = "its steps run one at a time" if found.repository else "its steps share the task directory"
            problems.append(f"{name}: {found.reason} ({effect})")
            continue
        size = ws._size_text(await ws.checkout_size(found.toplevel))
        text = f"{name}: {ws.short_branch(found.branch)}, {size} checkout"
        missing = [path for path in project.get("step_workspace_links") or ()
                   if not os.path.lexists(Path(work_dir) / path)]
        if missing:
            problems.append(f"{text}, nothing to link at {', '.join(missing)}")
        else:
            fine.append(text)
    return Check("step_workspaces", label, WARN if problems else OK, "; ".join(problems + fine),
                 STEP_WORKTREES_FIX if problems else "")


def pg_dump_fix(server_major: int | None) -> str:
    """How to give Looper a pg_dump for the server, or do without one."""
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
        "or migrate once without a backup: looper serve --skip-backup"
    )


def check_pg_dump(server_version_num: int | None, pending: bool | None) -> tuple[Check, db_backup.Dumper | None]:
    """Can Looper back the database up before it changes the schema? With the way it would, when it can.

    ``pending``: whether the schema is about to change (``None``: unknown).
    Without a way to back up, ``looper serve`` refuses only then, so only then
    is this a failure.
    """
    server_major = server_version_num // 10000 if server_version_num else None
    try:
        dumper = db_backup.find_dumper(server_major)
    except db_backup.NoDumper as exc:
        if pending:
            return Check("pg_dump", "pg_dump", FAIL,
                         f"{exc}; the database schema is about to change, and Looper backs it up first",
                         pg_dump_fix(server_major)), None
        return Check("pg_dump", "pg_dump", WARN,
                     f"{exc}; after an upgrade that changes the database schema, `looper serve` backs the database "
                     "up first and will not start without it",
                     pg_dump_fix(server_major)), None
    unchecked = "" if server_major or dumper.command else "; not compared with the server, which is unreachable"
    return Check("pg_dump", "pg_dump", OK, dumper.detail + unchecked), dumper


@dataclass(frozen=True)
class _DatabaseFacts:
    lark_enabled: bool | None = None
    server_version_num: int | None = None
    schema_change_pending: bool | None = None
    codex_login_profiles: int | None = None
    codex_login_problem: dict[str, str] | None = None
    grok_login_profiles: int | None = None
    grok_login_problem: dict[str, str] | None = None
    worktree_projects: list[dict[str, Any]] | None = None


async def _database_facts(dsn: str) -> _DatabaseFacts:
    import asyncpg

    from ..db.channel_configs import get_config
    from ..db.connection import schema_change_pending
    from ..db.profile_registry import login_profile_count
    from .runtimes import grok_login
    from ..db.projects import worktree_projects
    from .runtimes.codex_login import recorded_problem

    try:
        conn = await asyncpg.connect(dsn, timeout=_DATABASE_TIMEOUT_SECONDS)
    except Exception:
        return _DatabaseFacts()
    try:
        try:
            row = await get_config("channel-lark", pool=conn)  # a connection answers like a pool here
        except asyncpg.UndefinedTableError:  # a database `looper serve` has not set up yet
            row = None
        try:
            profiles: int | None = await login_profile_count("codex", pool=conn)
            problem = await recorded_problem(pool=conn)
            grok_profiles: int | None = await login_profile_count("grok", pool=conn)
            grok_problem = await grok_login.recorded_problem(pool=conn)
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):  # set up by an older `looper serve`
            profiles, problem, grok_profiles, grok_problem = 0, None, 0, None
        try:
            worktrees: list[dict[str, Any]] | None = await worktree_projects(pool=conn)
        except asyncpg.UndefinedTableError:
            worktrees = []
        except asyncpg.UndefinedColumnError:  # set up by a `looper serve` from before step workspaces
            worktrees = None
        return _DatabaseFacts(
            lark_enabled=bool(row and row.get("enabled")),
            server_version_num=int(await conn.fetchval("SELECT current_setting('server_version_num')")),
            schema_change_pending=await schema_change_pending(conn),
            codex_login_profiles=profiles,
            codex_login_problem=problem,
            grok_login_profiles=grok_profiles,
            grok_login_problem=grok_problem,
            worktree_projects=worktrees,
        )
    finally:
        await conn.close()


async def run_checks(*, dsn: str, claude_bin: str = "claude") -> list[Check]:
    """Every check, reading only what it can: the channel settings and the schema state need the database."""
    database = await check_database(dsn)
    facts = await _database_facts(dsn) if database.status == OK else _DatabaseFacts()
    from .runtimes import grok_login
    from .runtimes.codex_login import probe_login

    claude = await asyncio.to_thread(check_claude, claude_bin)
    codex = await asyncio.to_thread(check_codex_login, facts.codex_login_profiles, facts.codex_login_problem,
                                    probe_login)
    grok = check_grok_login(facts.grok_login_profiles, facts.grok_login_problem, grok_login.probe_login)
    pg_dump, _ = await asyncio.to_thread(check_pg_dump, facts.server_version_num, facts.schema_change_pending)
    git = await asyncio.to_thread(check_git)
    return [database, *claude, codex, grok, check_lark_cli(facts.lark_enabled), pg_dump, git,
            await check_step_worktrees(facts.worktree_projects)]


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
            consequence = (" Agents cannot run until this is fixed." if check.id.startswith("claude") else
                           f" Agent Profiles with the {check.label} cannot run until this is fixed."
                           if check.id in LOGIN_CHECKS else "")
            logger.warning("SELF-CHECK %s %s: %s.%s Fix: %s", check.status.upper(), check.label, check.detail,
                           consequence, check.fix.replace("\n", " "))


def doctor(claude_bin: str = "claude") -> int:
    """``looper doctor``: print every check, and whether the background service runs; 1 when one failed."""
    from ..db.connection import configured_dsn
    from .service import doctor_check

    checks = [*asyncio.run(run_checks(dsn=configured_dsn(), claude_bin=claude_bin)), doctor_check()]
    print(format_checks(checks))
    return 1 if any(check.status == FAIL for check in checks) else 0
