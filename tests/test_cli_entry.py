"""The ``loopx`` console script starts the agent system without upstream LoopX code.

Each case runs in a fresh interpreter and calls the function that
``[project.scripts]`` in pyproject.toml names for ``loopx``, as the installed
script does. ``serve_agent`` is replaced by a recorder, so nothing listens;
the case then lists every ``loopx`` module the interpreter loaded.
"""

from __future__ import annotations

import json
import re
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
