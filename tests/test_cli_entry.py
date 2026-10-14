"""The ``loopx`` console script starts the agent system without upstream LoopX code.

Each case runs in a fresh interpreter and calls the function that
``[project.scripts]`` in pyproject.toml names for ``loopx``, as the installed
script does. ``serve_agent`` is replaced by a recorder, so nothing listens;
the case then lists every ``loopx`` module the interpreter loaded.
``doctor`` runs its checks against a stand-in ``claude`` on a minimal PATH.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from loopx import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]

# The agent system's own packages and the one helper it borrows.
ALLOWED_MODULE = re.compile(r"loopx(\.cli|\.paths|\.agents(\.\w+)*|\.db(\.\w+)*)?")

_DRIVER = r"""
import json, sys, tomllib
from importlib import import_module
from pathlib import Path

pyproject, *argv = sys.argv[1:]
target = tomllib.loads(Path(pyproject).read_text())["project"]["scripts"]["loopx"]
module_name, _, function = target.partition(":")
calls = []
if argv[:1] == ["serve"]:
    import loopx.agents.serve as agents_serve
    agents_serve.serve_agent = lambda **kwargs: calls.append({k: str(v) for k, v in kwargs.items()})
try:
    code = getattr(import_module(module_name), function)(argv)
except SystemExit as exit_:
    code = exit_.code
sys.stdout.flush()
modules = sorted(name for name in sys.modules if name == "loopx" or name.startswith("loopx."))
print("\n" + json.dumps({"code": code, "calls": calls, "modules": modules}))
"""


def _run_loopx(*argv: str, cwd: Path, env: dict[str, str] | None = None) -> tuple[dict, str, str]:
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(REPO_ROOT / "pyproject.toml"), *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output, _, summary = proc.stdout.rstrip("\n").rpartition("\n")
    assert summary.startswith("{"), proc.stdout + proc.stderr
    return json.loads(summary), output.strip(), proc.stderr


def _upstream(modules: list[str]) -> list[str]:
    return [name for name in modules if not ALLOWED_MODULE.fullmatch(name)]


@pytest.mark.parametrize("flags", [True, False], ids=["flags", "defaults"])
def test_serve_starts_the_agent_system_without_upstream_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: bool
) -> None:
    monkeypatch.setenv("LOOPX_RUNTIME_ROOT", str(tmp_path / "env-root"))
    argv = ["serve"]
    if flags:
        argv += ["--host", "0.0.0.0", "--port", "9999", "--verbose", "--runtime-root", str(tmp_path / "flag-root")]

    result, _, stderr = _run_loopx(*argv, cwd=tmp_path)

    assert result["code"] == 0, stderr
    assert result["calls"] == [
        {
            "host": "0.0.0.0" if flags else "127.0.0.1",
            "port": "9999" if flags else "8767",
            "runtime_root": str(tmp_path / ("flag-root" if flags else "env-root")),
            "work_dir": str(tmp_path.resolve()),
            "verbose": str(flags),
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
    result, output, stderr = _run_loopx(*argv, cwd=tmp_path)

    assert result["code"] == 0, stderr
    if "json" in argv:
        assert json.loads(output) == {
            "ok": True,
            "schema_version": "loopx_version_v0",
            "name": "loopx",
            "version": __version__,
        }
    else:
        assert output == f"loopx {__version__}"
    assert result["modules"] == ["loopx", "loopx.cli"]


@pytest.mark.parametrize("command", ["update", "init"])
def test_removed_commands_are_rejected(tmp_path: Path, command: str) -> None:
    result, _, stderr = _run_loopx(command, "--help", cwd=tmp_path)

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


def _doctor_env(tmp_path: Path, *, claude: str | None, database_url: str) -> dict[str, str]:
    """PATH holds only the system tools and, unless ``claude`` is None, a stand-in in that state."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if claude is not None:
        stub = bin_dir / "claude"
        stub.write_text(_STUB_CLAUDE.format(calls=tmp_path / "calls", state=claude))
        stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "LOOPX_DATABASE_URL": database_url}


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _check_lines(output: str) -> dict[str, str]:
    """Each check's line, by its label."""
    labels = ("PostgreSQL", "Claude Code CLI", "Claude login", "Lark channel")
    return {label: line for line in output.splitlines() for label in labels if line[6:].startswith(label)}


def test_doctor_shows_an_unreachable_database_masked_with_its_fix(tmp_path: Path) -> None:
    port = _closed_port()
    env = _doctor_env(tmp_path, claude="logged_in", database_url=f"postgresql://loopx:s3cret-pw@127.0.0.1:{port}/loopx")

    result, output, stderr = _run_loopx("doctor", cwd=tmp_path, env=env)

    assert result["code"] == 1, stderr
    lines = _check_lines(output)
    assert lines["PostgreSQL"].startswith("fail")
    assert f"postgresql://loopx:***@127.0.0.1:{port}/loopx" in lines["PostgreSQL"]
    assert "s3cret-pw" not in output + stderr
    assert "docker run" in output and "export LOOPX_DATABASE_URL=" in output
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

    result, output, stderr = _run_loopx("doctor", cwd=tmp_path, env=env)

    assert result["code"] == 1, stderr
    assert _check_lines(output)[failing].startswith("fail")
    fix_line = output.splitlines()[output.splitlines().index(_check_lines(output)[failing]) + 1]
    assert fix_line.lstrip().startswith("fix:") and fix in fix_line


def test_doctor_never_runs_a_claude_without_auth_status(tmp_path: Path) -> None:
    """An older CLI would take `auth status` as a prompt, a paid model call; the login is then not checked."""
    env = _doctor_env(tmp_path, claude="old", database_url=f"postgresql://127.0.0.1:{_closed_port()}/loopx")

    result, output, stderr = _run_loopx("doctor", cwd=tmp_path, env=env)

    assert _check_lines(output)["Claude login"].startswith("skip  Claude login     not checked:"), output
    assert (tmp_path / "calls").read_text().splitlines() == ["--version", "auth --help"]


@pytest.mark.skipif(not os.environ.get("LOOPX_TEST_DATABASE_URL"), reason="LOOPX_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(
    ("lark", "lark_line", "code"),
    [("off", "skip", 0), ("on", "ok", 0), ("on-without-cli", "warn", 0)],
    ids=["lark-off", "lark-on", "lark-on-without-cli"],
)
def test_doctor_passes_unless_a_check_failed(tmp_path: Path, lark: str, lark_line: str, code: int) -> None:
    """An enabled Lark channel needs lark-cli; without it the channel will not start, but LoopX runs."""
    import asyncio

    import asyncpg

    from evals.prompt_regression.runner import fresh_database

    async def set_up(url: str) -> None:
        conn = await asyncpg.connect(url)
        try:
            await conn.execute((REPO_ROOT / "loopx" / "db" / "schema.sql").read_text())
            await conn.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled) "
                               "VALUES ('channel-lark', 'lark', $1)", lark != "off")
        finally:
            await conn.close()

    with fresh_database(os.environ["LOOPX_TEST_DATABASE_URL"]) as (database_url, _):
        asyncio.run(set_up(database_url))
        env = _doctor_env(tmp_path, claude="logged_in", database_url=database_url)
        if lark == "on":
            (tmp_path / "bin" / "lark-cli").write_text("#!/bin/sh\n")
            (tmp_path / "bin" / "lark-cli").chmod(0o755)
        result, output, stderr = _run_loopx("doctor", cwd=tmp_path, env=env)

    assert result["code"] == code, output + stderr
    assert [line[:5].strip() for line in _check_lines(output).values()] == ["ok", "ok", "ok", lark_line], output
    if lark_line == "warn":
        assert "install lark-cli" in output
