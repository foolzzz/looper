"""The MCP servers an operator allows the task Manager's direct session to load."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents import operator_mcp


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def stdio(name: str) -> dict:
    return {"type": "stdio", "command": f"/bin/{name}", "args": ["--serve"], "env": {}}


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    write_json(path / ".claude.json", {
        "mcpServers": {"alpha": stdio("alpha"), "shared": stdio("user-shared")},
        "projects": {str(tmp_path / "work"): {"mcpServers": {"beta": stdio("beta"), "shared": stdio("local-shared")}}},
    })
    return path


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    (path / "sub").mkdir(parents=True)
    write_json(path / ".mcp.json", {"mcpServers": {"gamma": stdio("gamma"), "shared": stdio("project-shared")}})
    return path


def environment(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


def test_resolves_user_local_and_project_scope(home, work):
    found = operator_mcp.resolve(["alpha", "beta", "gamma"], cwd=work, environment=environment(home))
    assert found == {"alpha": stdio("alpha"), "beta": stdio("beta"), "gamma": stdio("gamma")}


def test_scope_precedence_is_local_then_project_then_user(home, work):
    found = operator_mcp.resolve(["shared"], cwd=work, environment=environment(home))
    assert found["shared"]["command"] == "/bin/local-shared"
    write_json(home / ".claude.json", {"mcpServers": {"shared": stdio("user-shared")}})
    assert operator_mcp.resolve(["shared"], cwd=work / "sub", environment=environment(home))["shared"]["command"] == (
        "/bin/project-shared")  # a project file is found from a subdirectory too
    (work / ".mcp.json").unlink()
    assert operator_mcp.resolve(["shared"], cwd=work, environment=environment(home))["shared"]["command"] == (
        "/bin/user-shared")


def test_config_dir_overrides_home(home, work, tmp_path):
    config_dir = tmp_path / "claude-config"
    write_json(config_dir / ".claude.json", {"mcpServers": {"delta": stdio("delta")}})
    env = environment(home, CLAUDE_CONFIG_DIR=str(config_dir))
    assert operator_mcp.resolve(["delta"], cwd=work, environment=env) == {"delta": stdio("delta")}
    with pytest.raises(operator_mcp.UnresolvedServers) as raised:
        operator_mcp.resolve(["alpha"], cwd=work, environment=env)
    assert raised.value.names == ("alpha",)


def test_every_missing_name_is_reported_and_nothing_is_skipped(home, work):
    with pytest.raises(operator_mcp.UnresolvedServers) as raised:
        operator_mcp.resolve(["alpha", "nope", "also-missing"], cwd=work, environment=environment(home))
    assert raised.value.names == ("nope", "also-missing")
    assert "nope" in str(raised.value) and "also-missing" in str(raised.value) and "alpha" not in str(raised.value)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"mcpServers": []}', '{"mcpServers": {"alpha": "x"}}'])
def test_unreadable_configuration_fails_closed(home, work, content):
    (home / ".claude.json").write_text(content, encoding="utf-8")
    with pytest.raises(operator_mcp.UnresolvedServers):
        operator_mcp.resolve(["alpha"], cwd=work, environment=environment(home))


def test_empty_allowlist_reads_nothing(tmp_path, work):
    assert operator_mcp.resolve([], cwd=work, environment=environment(tmp_path / "missing")) == {}


def test_library_gets_looper_servers_plus_allowed_ones(home, work, tmp_path):
    from looper.agents.library import AgentLibrary
    from looper.agents.mcp import write_config

    root = tmp_path / "runtime"
    own = write_config(root, [("looper", {"command": "python", "args": ["-m", "looper.cli"]})])
    library = AgentLibrary(mcp_config=str(own))
    def build(names):
        return asyncio.run(operator_mcp.library_with_allowed(library, root, names, cwd=work, environment=environment(home)))
    assert build([]) is library
    allowed = build(["alpha", "gamma"])
    servers = json.loads(Path(allowed.mcp_config).read_text())["mcpServers"]
    assert sorted(servers) == ["alpha", "gamma", "looper"] and servers["looper"]["command"] == "python"
    with pytest.raises(operator_mcp.UnresolvedServers, match="looper"):  # Looper's own name is not the operator's
        build(["looper"])
    assert replace(library, mcp_config=None).mcp_config is None
