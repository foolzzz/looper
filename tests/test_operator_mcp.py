"""The MCP servers an operator allows the task Manager's direct session to load: user scope only."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from looper.agents import operator_mcp


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def stdio(name: str, **extra) -> dict:
    return {"type": "stdio", "command": f"/bin/{name}", "args": ["--serve"], "env": {}, **extra}


def environment(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    write_json(path / ".claude.json", {"mcpServers": {"alpha": stdio("alpha"), "shared": stdio("user-shared")}})
    return path


def hostile(tmp_path, home):
    """A task directory (a repository) whose project file and whose local-scope entry both claim allowed names."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=work, check=True)
    write_json(work / ".mcp.json", {"mcpServers": {"shared": stdio("project-shared"), "projonly": stdio("projonly")}})
    config = json.loads((home / ".claude.json").read_text())
    config["projects"] = {str(work): {"mcpServers": {"shared": stdio("local-shared"), "localonly": stdio("localonly")}}}
    write_json(home / ".claude.json", config)
    return work


def test_resolves_from_user_scope(home):
    assert operator_mcp.resolve(["alpha", "shared"], environment=environment(home)) == {
        "alpha": stdio("alpha"), "shared": stdio("user-shared")}


def test_project_file_and_local_scope_never_override_the_user_definition(home, tmp_path):
    work = hostile(tmp_path, home)
    assert work.is_dir()
    found = operator_mcp.resolve(["shared"], environment=environment(home))
    assert found["shared"]["command"] == "/bin/user-shared"


@pytest.mark.parametrize("name", ["projonly", "localonly"])
def test_a_name_defined_only_in_project_or_local_scope_is_unresolved(home, tmp_path, name):
    hostile(tmp_path, home)
    with pytest.raises(operator_mcp.UnresolvedServers) as raised:
        operator_mcp.resolve(["alpha", name], environment=environment(home))
    assert raised.value.names == (name,)


def test_config_dir_overrides_home(home, tmp_path):
    config_dir = tmp_path / "claude-config"
    write_json(config_dir / ".claude.json", {"mcpServers": {"delta": stdio("delta")}})
    env = environment(home, CLAUDE_CONFIG_DIR=str(config_dir))
    assert operator_mcp.resolve(["delta"], environment=env) == {"delta": stdio("delta")}
    with pytest.raises(operator_mcp.UnresolvedServers) as raised:
        operator_mcp.resolve(["alpha"], environment=env)
    assert raised.value.names == ("alpha",)


def test_every_missing_name_is_reported_and_nothing_is_skipped(home):
    with pytest.raises(operator_mcp.UnresolvedServers) as raised:
        operator_mcp.resolve(["alpha", "nope", "also-missing"], environment=environment(home))
    assert raised.value.names == ("nope", "also-missing")
    assert "nope" in str(raised.value) and "also-missing" in str(raised.value) and "alpha" not in str(raised.value)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"mcpServers": []}', '{"mcpServers": {"alpha": "x"}}'])
def test_unreadable_configuration_fails_closed(home, content):
    (home / ".claude.json").write_text(content, encoding="utf-8")
    with pytest.raises(operator_mcp.UnresolvedServers):
        operator_mcp.resolve(["alpha"], environment=environment(home))


def test_empty_allowlist_reads_nothing(tmp_path):
    assert operator_mcp.resolve([], environment=environment(tmp_path / "missing")) == {}


def test_fingerprint_is_stable_and_follows_command_args_transport_and_env_keys():
    base = stdio("alpha", env={"TOKEN": "secret-one"})
    same = {"env": {"TOKEN": "secret-one"}, "args": ["--serve"], "command": "/bin/alpha", "type": "stdio"}
    fingerprint = operator_mcp.fingerprint(base)
    assert fingerprint == operator_mcp.fingerprint(same) and len(fingerprint) == 64
    assert operator_mcp.fingerprint({**base, "env": {"TOKEN": "another-secret"}}) == fingerprint  # values stay out
    assert "secret-one" not in json.dumps(operator_mcp.fingerprint_input(base))
    for changed in ({**base, "command": "/bin/other"}, {**base, "args": ["--serve", "--x"]},
                    {**base, "env": {"TOKEN": "x", "MORE": "y"}}, {**base, "type": "sse", "url": "https://h.example/m"}):
        assert operator_mcp.fingerprint(changed) != fingerprint
    assert operator_mcp.fingerprint({"command": "/bin/alpha", "args": ["--serve"], "env": {"TOKEN": "x"}}) == fingerprint  # stdio by default
    remote = {"type": "http", "url": "https://h.example/m", "headers": {"Authorization": "Bearer x"}}
    assert operator_mcp.fingerprint({**remote, "headers": {"Authorization": "Bearer y"}}) == operator_mcp.fingerprint(remote)
    assert operator_mcp.fingerprint({**remote, "url": "https://other.example/m"}) != operator_mcp.fingerprint(remote)


def test_library_gets_looper_servers_plus_allowed_ones_and_their_fingerprints(home, tmp_path):
    from looper.agents.library import AgentLibrary
    from looper.agents.mcp import write_config

    root = tmp_path / "runtime"
    own = write_config(root, [("looper", {"command": "python", "args": ["-m", "looper.cli"]})])
    library = AgentLibrary(mcp_config=str(own))

    def build(names):
        return asyncio.run(operator_mcp.library_with_allowed(library, root, names, environment=environment(home)))

    assert build([]) is library
    allowed = build(["alpha", "shared"])
    servers = json.loads(Path(allowed.mcp_config).read_text())["mcpServers"]
    assert sorted(servers) == ["alpha", "looper", "shared"] and servers["looper"]["command"] == "python"
    assert operator_mcp.fingerprints(allowed, ["alpha", "shared"]) == {
        "alpha": operator_mcp.fingerprint(stdio("alpha")), "shared": operator_mcp.fingerprint(stdio("user-shared"))}
    with pytest.raises(operator_mcp.UnresolvedServers, match="looper"):  # Looper's own name is not the operator's
        build(["looper"])
