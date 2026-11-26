"""Looper's MCP server starts in the working directory of the agent session that loads it, which an agent
can write to. The generated entry must not let that directory replace the server's code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from looper.agents import claude_code_entry
from looper.agents.ops.service import server_config

MARKER = "planted-module-was-imported"
PLANTED = (
    "from pathlib import Path\n"
    f"Path(__file__).resolve().parent.parent.joinpath({MARKER!r}).write_text('imported')\n"
)
INITIALIZE = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}}}) + "\n"


def _plant(directory: Path) -> Path:
    package = directory / "looper"
    package.mkdir()
    (package / "__init__.py").write_text(PLANTED)
    (package / "cli.py").write_text(PLANTED)
    return directory / MARKER


def _start(entry: dict, cwd: Path) -> tuple[str, str]:
    """Start the entry as Claude Code does: its command and args in ``cwd`` with the entry's env only."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), **entry["env"]}
    done = subprocess.run([entry["command"], *entry["args"]], cwd=cwd, env=env, input=INITIALIZE, text=True,
                          capture_output=True, timeout=60)
    return done.stdout, done.stderr


def test_a_module_planted_in_the_working_directory_is_not_imported(tmp_path: Path) -> None:
    marker = _plant(tmp_path)
    entry = server_config("http://127.0.0.1:9", "token-for-tests")

    stdout, stderr = _start(entry, tmp_path)

    assert not marker.exists(), "the working directory's looper package ran inside the MCP server"
    reply = json.loads(stdout.splitlines()[0])
    assert reply["id"] == 1 and reply["result"]["serverInfo"]["name"], stderr


def test_the_planted_module_would_run_without_isolation(tmp_path: Path) -> None:
    # Positive control: the plant works, so the test above can fail.
    marker = _plant(tmp_path)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    subprocess.run([sys.executable, "-m", "looper.cli", "mcp-server"], cwd=tmp_path, env=env, input="",
                   text=True, capture_output=True, timeout=60)
    assert marker.exists()


def test_the_entry_is_isolated_with_an_absolute_interpreter() -> None:
    entry = server_config("http://127.0.0.1:9", "token-for-tests", reconnect_seconds=5)

    assert os.path.isabs(entry["command"])
    assert entry["args"] == ["-I", "-m", "looper.cli", "mcp-server"]


def test_the_claude_code_entry_never_leaves_the_module_form_unisolated(tmp_path: Path, monkeypatch) -> None:
    python = tmp_path / "bin" / "python3"
    python.parent.mkdir()
    python.write_text("")
    monkeypatch.setattr(claude_code_entry.sys, "executable", str(python))

    entry = claude_code_entry.mcp_entry(tmp_path / "runtime", None)

    assert entry["command"] == str(python)
    assert entry["args"][:3] == ["-I", "-m", "looper.cli"]


def test_the_session_start_hook_fallback_never_imports_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    from looper.agents import service

    runtime, work = tmp_path / "runtime", tmp_path / "work"
    runtime.mkdir()
    work.mkdir()
    marker = _plant(work)
    monkeypatch.setattr(service, "looper_command", lambda executable: [executable, "-m", "looper.cli"])
    command = claude_code_entry.desired_handler(runtime, None)["command"]

    assert " -I -m looper.cli " in command
    subprocess.run(["sh", "-c", command], cwd=work, env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
                   capture_output=True, timeout=60)

    assert not marker.exists()


def test_the_profile_reader_never_imports_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from looper.agents import profile_commands

    work = tmp_path / "work"
    work.mkdir()
    marker = _plant(work)
    monkeypatch.chdir(work)
    monkeypatch.delenv("PYTHONPATH", raising=False)  # the suite's own guard imports looper before the cwd counts
    profile_commands.read_text(SimpleNamespace(runtime_root=str(tmp_path / "runtime"), config=None))

    assert not marker.exists()
