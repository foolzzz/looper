"""The MCP servers an operator allows the task Manager's direct session to load: user scope only."""
from __future__ import annotations

import asyncio
import json
import os
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
    own_definition = {"command": "/usr/bin/python3", "args": ["-m", "looper.cli"], "env": {"LOOPER_TOKEN": "t"}}
    own = write_config(root, [("looper", own_definition)])
    library = AgentLibrary(mcp_config=str(own))

    def build(names, lib=library):
        return asyncio.run(operator_mcp.library_with_allowed(lib, root, names, environment=environment(home),
                                                                    cwd=root / "cwd"))

    allowed = build(["alpha", "shared"])
    servers = json.loads(Path(allowed.mcp_config).read_text())["mcpServers"]
    assert sorted(servers) == ["alpha", "looper", "shared"]
    # Every stdio server, Looper's own included, starts through the wrapper in a directory of Looper's own.
    assert all(operator_mcp.is_wrapped(entry) for entry in servers.values())
    assert operator_mcp.unwrap(servers["looper"]) == {**own_definition, "args": ["-m", "looper.cli"]}
    assert servers["alpha"]["args"][3] == str(root / "cwd")
    assert operator_mcp.unwrap(servers["alpha"]) == stdio("alpha")
    # Fingerprints are those of the user-scope definitions, before wrapping.
    assert operator_mcp.fingerprints(allowed, ["alpha", "shared"]) == {
        "alpha": operator_mcp.fingerprint(stdio("alpha")), "shared": operator_mcp.fingerprint(stdio("user-shared"))}
    assert operator_mcp.fingerprint(servers["alpha"]) != operator_mcp.fingerprint(stdio("alpha"))
    with pytest.raises(operator_mcp.UnresolvedServers, match="looper"):  # Looper's own name is not the operator's
        build(["looper"])
    # Looper's own server alone is wrapped too; a library with nothing in it is returned as it is.
    only_own = json.loads(Path(build([]).mcp_config).read_text())["mcpServers"]
    assert list(only_own) == ["looper"] and operator_mcp.is_wrapped(only_own["looper"])
    empty = AgentLibrary()
    assert build([], empty) is empty


def test_wrapping_is_idempotent_and_unwrap_leaves_other_definitions_alone(tmp_path):
    definition = stdio("alpha", env={"K": "v"}, cwd="/somewhere")
    wrapped = operator_mcp.wrap(definition, tmp_path)
    assert operator_mcp.wrap(wrapped, tmp_path) == wrapped and operator_mcp.unwrap(wrapped) == definition
    assert operator_mcp.unwrap(definition) == definition
    remote = {"type": "http", "url": "https://h.example/m"}
    assert operator_mcp.wrap(remote, tmp_path) == remote
    with pytest.raises(ValueError, match="absolute"):
        operator_mcp.wrap({"command": "alpha"}, tmp_path)
    # A server that merely uses sh with other arguments is not mistaken for the wrapper.
    assert not operator_mcp.is_wrapped({"command": "/bin/sh", "args": ["-c", "echo hi", "sh", "a", "b"]})


def test_the_wrapper_starts_the_command_in_the_private_directory_not_the_task_directory(tmp_path):
    task, private = tmp_path / "task", tmp_path / "private"
    task.mkdir()
    private.mkdir()
    (task / "looper").mkdir()
    marker = tmp_path / "out.txt"
    script = tmp_path / "server.sh"
    script.write_text(f'#!/bin/sh\npwd -P > {marker}\nprintf "%s|%s" "$1" "$2" >> {marker}\n')
    script.chmod(0o755)
    wrapped = operator_mcp.wrap({"command": str(script), "args": ["a b", "$HOME"]}, private)
    subprocess.run([wrapped["command"], *wrapped["args"]], cwd=task, check=True)
    assert marker.read_text() == f"{os.path.realpath(private)}\na b|$HOME"  # arguments arrive verbatim
    missing = operator_mcp.wrap({"command": str(script)}, tmp_path / "gone")
    marker.unlink()
    assert subprocess.run([missing["command"], *missing["args"]], cwd=task).returncode != 0 and not marker.exists()


@pytest.mark.parametrize("definition,launcher", [
    ({"command": "npx", "args": ["-y", "pkg"]}, "npx"),
    ({"command": "/usr/local/bin/npx", "args": ["-y", "pkg"]}, "npx"),
    ({"command": "/usr/local/bin/uvx", "args": ["tool"]}, "uvx"),
    ({"command": "/opt/bin/bunx", "args": ["tool"]}, "bunx"),
    ({"command": "/opt/bin/uv", "args": ["run", "x"]}, "uv run"),
    ({"command": "/opt/bin/pnpm", "args": ["dlx", "x"]}, "pnpm dlx"),
    ({"command": "/opt/bin/yarn", "args": ["dlx", "x"]}, "yarn dlx"),
    ({"command": "/opt/bin/deno", "args": ["run", "x.ts"]}, "deno run"),
    ({"command": "/opt/bin/bundle", "args": ["exec", "srv"]}, "bundle exec"),
    ({"command": "/opt/bin/go", "args": ["run", "."]}, "go run"),
    ({"command": "/opt/bin/cargo", "args": ["run"]}, "cargo run"),
    ({"command": "python", "args": ["-m", "pkg"]}, "python -m"),
    ({"command": "/usr/bin/python3", "args": ["-m", "pkg"]}, "python -m"),
    ({"command": "/usr/bin/python3", "args": ["-m", "pkg", "-I"]}, "python -m"),
    ({"command": "/usr/bin/node", "args": ["./server.js"]}, "relative_script"),
    ({"command": "node", "args": ["/opt/srv/index.js"]}, "relative_command"),
    ({"command": "server.sh"}, "relative_command"),
    ({"command": "./server"}, "relative_command"),
    ({"cwd": "sub"}, "relative_cwd"),
    ({"command": "/opt/tools/server", "cwd": "sub"}, "relative_cwd"),
    ({"command": "/opt/tools/server", "cwd": "/opt/tools"}, "cwd"),
    ({"command": "/opt/tools/server", "env": {"PYTHONPATH": "lib"}}, "relative PYTHONPATH"),
    ({"command": "/opt/tools/server", "env": {"NODE_PATH": "/ok:rel"}}, "relative NODE_PATH"),
])
def test_unsafe_launchers_are_named(definition, launcher):
    assert operator_mcp.launcher_problem(definition) == launcher


@pytest.mark.parametrize("definition", [
    {"command": "/usr/bin/python3", "args": ["-I", "-m", "pkg"]},
    {"command": "/usr/bin/python3", "args": ["-c", "print(1)"]},
    {"command": "/opt/tools/server", "args": ["--stdio"], "env": {"PYTHONPATH": "/opt/lib"}},
    {"command": "/usr/bin/node", "args": ["/opt/srv/index.js"]},
    {"command": "/opt/bin/uv", "args": ["tool", "list"]},
    {"type": "http", "url": "https://h.example/m"},
    {"type": "sse", "url": "https://h.example/s", "headers": {"A": "b"}},
])
def test_safe_definitions_pass(definition):
    assert operator_mcp.launcher_problem(definition) is None


def test_an_unsafe_allowed_server_raises_before_anything_is_written(home, tmp_path):
    from looper.agents.library import AgentLibrary

    config = json.loads((home / ".claude.json").read_text())
    config["mcpServers"]["risky"] = {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]}
    write_json(home / ".claude.json", config)
    root = tmp_path / "runtime"
    with pytest.raises(operator_mcp.UnsafeLauncher) as raised:
        asyncio.run(operator_mcp.library_with_allowed(AgentLibrary(), root, ["alpha", "risky"],
                                                     environment=environment(home), cwd=root / "cwd"))
    assert (raised.value.name, raised.value.launcher) == ("risky", "npx")
    assert "absolute path" in str(raised.value) and not root.exists()
