"""The ``looper`` console script starts the agent system without upstream LoopX code.

Each case runs in a fresh interpreter and calls the function that
``[project.scripts]`` in pyproject.toml names for ``looper``, as the installed script does. ``serve_agent``
is replaced by a recorder, so nothing listens; the case then lists every
``looper`` module the interpreter loaded.
``doctor`` runs its checks against a stand-in ``claude`` on a minimal PATH.
The ops-file cases run the actual entry point, in a temporary home and runtime root.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from looper import __version__, ops_config

REPO_ROOT = Path(__file__).resolve().parents[1]

# The agent system's own packages and the one helper it borrows.
ALLOWED_MODULE = re.compile(r"looper(\.cli|\.env|\.paths|\.ops_config|\.child_env|\.agents(\.\w+)*|\.db(\.\w+)*)?")

_DRIVER = r"""
import json, subprocess, sys, tomllib
from importlib import import_module
from pathlib import Path

# sitecustomize may preload the runtime-root guard's looper.child_env; measure this command's imports.
baseline = set(sys.modules)
pyproject, script, *argv = sys.argv[1:]
target = tomllib.loads(Path(pyproject).read_text())["project"]["scripts"][script]
module_name, _, function = target.partition(":")
calls = []
if argv[:1] == ["serve"]:
    import looper.agents.serve as agents_serve
    agents_serve.serve_agent = lambda **kwargs: calls.append({k: str(v) for k, v in kwargs.items()})
if argv[:1] in (["doctor"], ["service"]):
    import looper.agents.service as service
    service._run = lambda argv: subprocess.CompletedProcess(argv, 1, "", "not installed")
try:
    code = getattr(import_module(module_name), function)(argv)
except SystemExit as exit_:
    code = exit_.code
sys.stdout.flush()
modules = sorted(name for name in sys.modules
                 if name == "looper" or (name.startswith("looper.") and name not in baseline))
print("\n" + json.dumps({"code": code, "calls": calls, "modules": modules}))
"""


def _clean_env(root: Path) -> dict[str, str]:
    """Give children only test-owned state and the import/runtime-root guard settings."""
    home = root / "home"
    home.mkdir(exist_ok=True)
    names = ("PATH", "PYTHONPATH", "PYTEST_LOOPER_PROTECTED_RUNTIME_ROOTS", "PYTEST_LOOPER_RUNTIME_ROOT_GUARD_REPORT")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO_ROOT), env.get("PYTHONPATH"))))
    return {**env, "HOME": str(home)}


def _run_looper(*argv: str, cwd: Path, env: dict[str, str] | None = None, script: str = "looper",
                ) -> tuple[dict, str, str]:
    if argv[:1] == ("doctor",):
        argv = (*argv, "--runtime-root", str(cwd / "runtime"))
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(REPO_ROOT / "pyproject.toml"), script, *argv],
        cwd=cwd,
        env=_clean_env(cwd) if env is None else env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output, _, summary = proc.stdout.rstrip("\n").rpartition("\n")
    assert summary.startswith("{"), proc.stdout + proc.stderr
    return json.loads(summary), output.strip(), proc.stderr


def _run_cli(*argv: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the actual entry point without the serve recorder."""
    return subprocess.run([sys.executable, "-m", "looper.cli", *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def _ops_file(root: Path, database_url: str) -> Path:
    directory = root / "ops"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / "server.toml"
    path.write_text(f"[database]\nurl = {json.dumps(database_url)}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("database_url", [
    "postgresql://looper:Sekr1t-init-pw@127.0.0.1:5432/loopx", ops_config.SAMPLE_DATABASE_URL,
])
def test_init_ops_cli_preserves_the_database_from_deployment_environment(tmp_path: Path, database_url: str) -> None:
    root = tmp_path / "runtime"
    env = {**_clean_env(tmp_path), "LOOPER_DATABASE_URL": database_url}

    proc = _run_cli("config", "init-ops", "--runtime-root", str(root), cwd=tmp_path, env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    path = root / "ops" / "server.toml"
    assert tomllib.loads(path.read_text())["database"]["url"] == database_url
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "database.url from LOOPER_DATABASE_URL" in proc.stdout
    assert "Sekr1t-init-pw" not in proc.stdout + proc.stderr
    assert "sample DSN" not in proc.stdout and "set database.url" not in proc.stdout
    assert "postgresql://" not in proc.stdout


def test_init_ops_cli_reports_the_sample_written_when_no_database_is_configured(tmp_path: Path) -> None:
    root = tmp_path / "runtime"

    proc = _run_cli("config", "init-ops", "--runtime-root", str(root), cwd=tmp_path, env=_clean_env(tmp_path))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    database_url = tomllib.loads((root / ops_config.FILE).read_text())["database"]["url"]
    assert database_url == ops_config.SAMPLE_DATABASE_URL
    assert "sample DSN" in proc.stdout and "edit it for your database" in proc.stdout
    assert set(re.findall(r"postgresql://[^\s;]+", proc.stdout)) == {database_url}


def test_serve_cli_without_a_database_exits_with_setup_instructions(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    port = _closed_port()
    assert port != 8767

    proc = _run_cli("serve", "--runtime-root", str(root), "--port", str(port),
                    cwd=tmp_path, env=_clean_env(tmp_path))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "looper config init-ops" in proc.stdout + proc.stderr
    assert not (root / "ops" / "server.toml").exists()


def test_doctor_cli_uses_the_ops_file_and_masks_the_database_password(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    port = _closed_port()
    database_url = f"postgresql://looper:Sekr1t-doctor-pw@127.0.0.1:{port}/loopx"
    _ops_file(root, database_url)
    env = _doctor_env(tmp_path, claude="logged_in", database_url=None)

    proc = _run_cli("doctor", "--runtime-root", str(root), cwd=tmp_path, env=env)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    line = _check_lines(proc.stdout)["PostgreSQL"]
    assert line.startswith("fail")
    assert f"postgresql://looper:***@127.0.0.1:{port}/loopx" in line
    assert "Sekr1t-doctor-pw" not in proc.stdout + proc.stderr


def _upstream(modules: list[str]) -> list[str]:
    return [name for name in modules if not ALLOWED_MODULE.fullmatch(name)]


@pytest.mark.parametrize("flags", [True, False], ids=["flags", "defaults"])
def test_serve_starts_the_agent_system_without_upstream_modules(
    tmp_path: Path, flags: bool
) -> None:
    argv = ["serve"]
    if flags:
        argv += ["--host", "0.0.0.0", "--port", "9999", "--verbose", "--runtime-root", str(tmp_path / "flag-root"),
                 "--config", str(tmp_path / "custom.toml"), "--skip-backup"]

    env = _clean_env(tmp_path)
    env["LOOPER_RUNTIME_ROOT"] = str(tmp_path / "env-root")
    result, _, stderr = _run_looper(*argv, cwd=tmp_path, env=env)

    assert result["code"] == 0, stderr
    assert result["calls"] == [
        {
            "host": "0.0.0.0" if flags else "None",
            "port": "9999" if flags else "None",
            "runtime_root": str(tmp_path / "flag-root") if flags else "None",
            "config": str(tmp_path / "custom.toml") if flags else "None",
            "work_dir": str(tmp_path.resolve()),
            "verbose": str(flags),
            "skip_backup": str(flags),
        }
    ]
    upstream = _upstream(result["modules"])
    assert not upstream, f"{len(upstream)} upstream modules loaded, e.g. {upstream[:10]}"


@pytest.mark.parametrize(
    "argv",
    [["version"], ["--version"], ["version", "--format", "json"]],
    ids=["version", "--version", "version-json"],
)
def test_version_loads_only_the_cli(tmp_path: Path, argv: list[str]) -> None:
    result, output, stderr = _run_looper(*argv, cwd=tmp_path)

    assert result["code"] == 0, stderr
    if "json" in argv:
        assert json.loads(output) == {
            "ok": True,
            "schema_version": "looper_version_v0",
            "name": "looper",
            "version": __version__,
        }
    else:
        assert output == f"looper {__version__}"
    assert stderr == ""
    assert result["modules"] == ["looper", "looper.cli"]


@pytest.mark.parametrize("command", ["export", "import"])
def test_removed_config_commands_are_rejected_by_argparse(tmp_path: Path, command: str) -> None:
    """Removed offline commands fail before reading state."""
    result, output, stderr = _run_looper("config", command, "portable.json", cwd=tmp_path)

    assert result["code"] == 2, output + stderr
    assert f"invalid choice: '{command}'" in stderr
    assert not (tmp_path / "portable.json").exists()
    assert result["modules"] == ["looper", "looper.cli"]


def test_config_help_keeps_only_init_ops(tmp_path: Path) -> None:
    result, output, stderr = _run_looper("config", "--help", cwd=tmp_path)

    assert result["code"] == 0, stderr
    assert "{init-ops}" in output
    assert "{export" not in output


def test_service_status_loads_no_upstream_modules(tmp_path: Path) -> None:
    """The status CLI routes to a stand-in manager reporting a label never installed."""
    result, output, stderr = _run_looper("service", "status", "--label", "com.looper.serve.never-installed", cwd=tmp_path)

    assert result["code"] == 1, stderr
    assert "not installed" in output
    assert not _upstream(result["modules"])


@pytest.mark.parametrize("command", ["update", "init"])
def test_removed_commands_are_rejected(tmp_path: Path, command: str) -> None:
    result, _, stderr = _run_looper(command, "--help", cwd=tmp_path)

    assert result["code"] == 2
    assert f"invalid choice: '{command}'" in stderr


# A stand-in Claude Code: records each call; `auth status` answers like 2.1's.
# An "old" one has no `auth` command, so its `auth --help` prints the main usage.
_STUB_CLAUDE = """#!/bin/sh
echo "$*" >> "{calls}"
case "$*" in
  "--version") echo "9.9.9 (Claude Code)" ;;
  "auth --help")
    if [ "{state}" = old ]; then echo "Usage: claude [options] [command] [prompt]"
    else echo "Usage: claude auth [options] [command]"; echo "  status [options]  Show authentication status"; fi ;;
  "auth status --json")
    if [ "{state}" = logged_in ]; then echo '{{"loggedIn": true, "authMethod": "claude.ai"}}'
    else echo '{{"loggedIn": false, "authMethod": "none"}}'; exit 1; fi ;;
  *) echo "unexpected call" >&2; exit 99 ;;
esac
"""


def _doctor_env(tmp_path: Path, *, claude: str | None, database_url: str | None) -> dict[str, str]:
    """PATH holds only the system tools and, unless ``claude`` is None, a stand-in in that state."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if claude is not None:
        stub = bin_dir / "claude"
        stub.write_text(_STUB_CLAUDE.format(calls=tmp_path / "calls", state=claude))
        stub.chmod(0o755)
    # doctor also checks the optional service; never invoke the actual service manager.
    for name in ("launchctl", "systemctl"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    if database_url is not None:
        _ops_file(tmp_path / "runtime", database_url)
    return {**_clean_env(tmp_path), "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _check_lines(output: str) -> dict[str, str]:
    """Each check's line, by its label."""
    labels = ("PostgreSQL", "Claude Code CLI", "Claude login", "Lark channel")
    return {label: line for line in output.splitlines() for label in labels if line[6:].startswith(label)}


@pytest.mark.parametrize("command", ["serve", "doctor"])
def test_invalid_deployment_environment_is_validated_before_startup(tmp_path: Path, command: str) -> None:
    env = {**_clean_env(tmp_path), "LOOPER_DATABASE_URL": "mysql://u:Sekr1t-env-pw@ignored/current",
           "LOOPX_DATABASE_URL": "postgresql://ignored/retired"}
    result = _run_cli(command, "--runtime-root", str(tmp_path / "runtime"), cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert "LOOPER_DATABASE_URL must be a postgresql:// URL" in result.stdout + result.stderr
    assert "Sekr1t-env-pw" not in result.stdout + result.stderr
    assert "postgresql://ignored" not in result.stdout + result.stderr


def test_doctor_shows_an_unreachable_database_masked_with_its_fix(tmp_path: Path) -> None:
    port = _closed_port()
    env = _doctor_env(tmp_path, claude="logged_in", database_url=f"postgresql://looper:s3cret-pw@127.0.0.1:{port}/loopx")

    result, output, stderr = _run_looper("doctor", cwd=tmp_path, env=env)

    assert result["code"] == 1, stderr
    lines = _check_lines(output)
    assert lines["PostgreSQL"].startswith("fail")
    assert f"postgresql://looper:***@127.0.0.1:{port}/loopx" in lines["PostgreSQL"]
    assert "s3cret-pw" not in output + stderr
    assert "docker run" in output and "looper config init-ops" in output and "[database]" in output
    assert lines["Claude Code CLI"].startswith("ok") and lines["Claude login"].startswith("ok")
    # Whether Lark is enabled is in the database, so it is not checked.
    assert lines["Lark channel"].startswith("skip")
    assert not _upstream(result["modules"])


@pytest.mark.parametrize(
    ("claude", "failing", "fix"),
    [
        (None, "Claude Code CLI", "npm install -g @anthropic-ai/claude-code"),
        ("logged_out", "Claude login", "claude auth login"),
    ],
    ids=["no-claude", "logged-out"],
)
def test_doctor_fails_the_claude_check_with_its_fix(
    tmp_path: Path, claude: str | None, failing: str, fix: str
) -> None:
    env = _doctor_env(tmp_path, claude=claude, database_url=f"postgresql://127.0.0.1:{_closed_port()}/loopx")

    result, output, stderr = _run_looper("doctor", cwd=tmp_path, env=env)

    assert result["code"] == 1, stderr
    assert _check_lines(output)[failing].startswith("fail")
    fix_line = output.splitlines()[output.splitlines().index(_check_lines(output)[failing]) + 1]
    assert fix_line.lstrip().startswith("fix:") and fix in fix_line


def test_doctor_never_runs_a_claude_without_auth_status(tmp_path: Path) -> None:
    """An older CLI would take `auth status` as a prompt, a paid model call; the login is then not checked."""
    env = _doctor_env(tmp_path, claude="old", database_url=f"postgresql://127.0.0.1:{_closed_port()}/loopx")

    result, output, stderr = _run_looper("doctor", cwd=tmp_path, env=env)

    assert _check_lines(output)["Claude login"].startswith("skip  Claude login     not checked:"), output
    assert (tmp_path / "calls").read_text().splitlines() == ["--version", "auth --help"]


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(
    ("lark", "lark_line", "code"),
    [("off", "skip", 0), ("on", "ok", 0), ("on-without-cli", "warn", 0)],
    ids=["lark-off", "lark-on", "lark-on-without-cli"],
)
def test_doctor_passes_unless_a_check_failed(tmp_path: Path, lark: str, lark_line: str, code: int) -> None:
    """An enabled Lark channel needs lark-cli; without it the channel will not start, but Looper runs."""
    import asyncio

    import asyncpg

    from evals.prompt_regression.runner import fresh_database
    from looper.db.connection import ensure_schema

    async def set_up(url: str) -> None:
        conn = await asyncpg.connect(url)
        try:
            await ensure_schema(conn)
            await conn.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled) "
                               "VALUES ('channel-lark', 'lark', $1)", lark != "off")
        finally:
            await conn.close()

    with fresh_database(os.environ["LOOPER_TEST_DATABASE_URL"]) as (database_url, _):
        asyncio.run(set_up(database_url))
        env = _doctor_env(tmp_path, claude="logged_in", database_url=database_url)
        if lark == "on":
            (tmp_path / "bin" / "lark-cli").write_text("#!/bin/sh\n")
            (tmp_path / "bin" / "lark-cli").chmod(0o755)
        result, output, stderr = _run_looper("doctor", cwd=tmp_path, env=env)

    assert result["code"] == code, output + stderr
    assert [line[:5].strip() for line in _check_lines(output).values()] == ["ok", "ok", "ok", lark_line], output
    if lark_line == "warn":
        assert "install lark-cli" in output


def _stub_pg_dump(bin_dir: Path, version: str, *, pg_restore: bool = True) -> None:
    """A pg_dump at ``version``; with ``pg_restore`` the dump can be verified by the one beside it."""
    stub = bin_dir / "pg_dump"
    stub.write_text(f"#!/bin/sh\necho 'pg_dump (PostgreSQL) {version}'\n")
    stub.chmod(0o755)
    if pg_restore:
        restore = bin_dir / "pg_restore"
        restore.write_text("#!/bin/sh\n")
        restore.chmod(0o755)


def _pg_dump_line(output: str) -> tuple[str, str]:
    """The pg_dump check's line and the line after it (its fix, when it has one)."""
    lines = [*output.splitlines(), ""]
    [index] = [i for i, line in enumerate(lines) if line[6:].startswith("pg_dump ")]
    return lines[index], lines[index + 1]


@pytest.mark.parametrize(
    ("pg_dump", "pg_restore", "status"),
    [(None, False, "warn"), ("16.4", True, "ok"), ("16.4", False, "warn")],
    ids=["missing", "found", "unverifiable"],
)
def test_doctor_reports_the_pg_dump_that_backs_up_before_a_schema_change(
    tmp_path: Path, pg_dump: str | None, pg_restore: bool, status: str,
) -> None:
    env = _doctor_env(tmp_path, claude="logged_in", database_url=f"postgresql://127.0.0.1:{_closed_port()}/loopx")
    env["PATH"] = str(tmp_path / "bin")  # no system pg_dump
    if pg_dump:
        _stub_pg_dump(tmp_path / "bin", pg_dump, pg_restore=pg_restore)

    _, output, _ = _run_looper("doctor", cwd=tmp_path, env=env)

    line, fix = _pg_dump_line(output)
    assert line.startswith(status), output
    if pg_dump:
        assert "pg_dump (PostgreSQL) 16.4" in line
        if pg_restore:
            assert f"backup verified by {tmp_path / 'bin' / 'pg_restore'} --list" in line, output
        else:
            assert "no way to verify the backup" in line, output
            assert "verify_command" in output, output
    else:
        assert fix.lstrip().startswith("fix: install pg_dump"), output


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(("pg_dump", "status", "code"), [(None, "fail", 1), ("9.6.24", "fail", 1), ("99.0", "ok", 0)],
                         ids=["missing", "older-than-the-server", "usable"])
def test_doctor_fails_without_a_usable_pg_dump_when_the_schema_is_about_to_change(
    tmp_path: Path, pg_dump: str | None, status: str, code: int,
) -> None:
    import asyncio

    import asyncpg

    from evals.prompt_regression.runner import fresh_database
    from looper.db.connection import ensure_schema

    async def set_up(url: str) -> None:
        conn = await asyncpg.connect(url)
        try:
            await ensure_schema(conn)
            await conn.execute("UPDATE schema_fingerprint SET sha256 = 'an older schema.sql'")
        finally:
            await conn.close()

    with fresh_database(os.environ["LOOPER_TEST_DATABASE_URL"]) as (database_url, _):
        asyncio.run(set_up(database_url))
        env = _doctor_env(tmp_path, claude="logged_in", database_url=database_url)
        env["PATH"] = str(tmp_path / "bin")
        if pg_dump:
            _stub_pg_dump(tmp_path / "bin", pg_dump)  # with a pg_restore beside it to verify the dump
        result, output, stderr = _run_looper("doctor", cwd=tmp_path, env=env)

    assert result["code"] == code, output + stderr
    line, _ = _pg_dump_line(output)
    assert line.startswith(status), output
    if status == "fail":
        assert "the database schema is about to change" in line



@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="LOOPER_TEST_DATABASE_URL is not set")
def test_doctor_checks_git_and_the_directories_of_projects_with_step_worktrees(tmp_path: Path) -> None:
    """A warning, not a failure: a project whose directory cannot have step worktrees runs its steps one at a time."""
    import asyncio
    import subprocess

    import asyncpg

    from evals.prompt_regression.runner import fresh_database
    from looper.db.connection import ensure_schema

    checkout = tmp_path / "alarm"
    checkout.mkdir()
    git = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=checkout, env=git, check=True)
    (checkout / "report.md").write_text("# report\n")
    subprocess.run(["git", "add", "-A"], cwd=checkout, env=git, check=True)
    subprocess.run(["git", "-c", "user.name=U", "-c", "user.email=u@x", "commit", "-qm", "init"], cwd=checkout,
                   env=git, check=True)
    (checkout / ".venv").mkdir()

    async def set_up(url: str) -> None:
        conn = await asyncpg.connect(url)
        try:
            await ensure_schema(conn)
            await conn.execute("INSERT INTO projects (id, name, work_dir, step_workspaces, workspace_links) "
                               "VALUES ('proj-a', 'alarm', $1, 'worktree', $2)", str(checkout), [".venv", ".env"])
        finally:
            await conn.close()

    with fresh_database(os.environ["LOOPER_TEST_DATABASE_URL"]) as (database_url, _):
        asyncio.run(set_up(database_url))
        env = _doctor_env(tmp_path, claude="logged_in", database_url=database_url)
        result, output, stderr = _run_looper("doctor", cwd=tmp_path, env=env)

    assert result["code"] == 0, output + stderr
    lines = {line[6:].split("  ")[0].strip(): line for line in output.splitlines() if line[:5].strip()}
    assert lines["git"].startswith("ok") and "git version 2." in lines["git"], output
    assert lines["Step worktrees"] == ("warn  Step worktrees   alarm: main, 9 B checkout, nothing to link at .env"), output


@pytest.mark.parametrize("format", ["json", "markdown"])
def test_serve_format_is_rejected(tmp_path: Path, format: str) -> None:
    result, _, stderr = _run_looper("serve", "--format", format, cwd=tmp_path)
    assert result["code"] == 2 and "unrecognized arguments: --format" in stderr
    assert result["calls"] == []


def test_installed_looper_entrypoint_reports_the_current_version(tmp_path: Path) -> None:
    from importlib.metadata import distribution

    scripts = {entry.name: entry.value for entry in distribution("looper").entry_points
               if entry.group == "console_scripts"}
    assert scripts == {"looper": "looper.cli:main", "loopercli": "looper.loopercli:main"}
    proc = subprocess.run([str(Path(sys.executable).with_name("looper")), "--version"], cwd=tmp_path,
                          env=_clean_env(tmp_path), text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == f"looper {__version__}"
    assert proc.stderr == ""
