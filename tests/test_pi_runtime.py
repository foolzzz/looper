"""pi's identity, private launch commands and API-key configuration (RFC sections 1, 2, 3 and 6).

A successful 1.3.0+ probe enables all four roles. The probe runs only
``--version`` and ``--help``, offline, in temporary directories.
Provider requests are checked with installed pi 1.3 against a local fake
Responses service and, separately, an injected fetch transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import AgentProfile, CatalogModel, ProviderSpec, RuntimeSpec, servable_roles
from looper.agents.runtimes import (
    ADAPTERS, ApiProtocol, AuthKind, Probe, RuntimeAdapter, RuntimeUnsupported, adapter,
)
from looper.agents.runtimes.pi import PROBE_HELP_TOKENS, PiRuntime

PI = PiRuntime()


def test_the_module_imports_and_pi_runtime_can_be_constructed() -> None:
    assert isinstance(PI, RuntimeAdapter)
    assert (PI.type, PI.label, PI.default_executable) == ("pi", "pi", "pi")
    assert {capability.value for capability in PI.capabilities} == {
        "run", "resident_session", "resume", "steer", "read_tools", "compaction_events", "mcp"}
    assert PI.capabilities_for("1.3.0") == PI.capabilities
    assert PI.capabilities_for(None) == frozenset()
    assert PI.protocols == frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT})
    assert ApiProtocol.ANTHROPIC_MESSAGES not in PI.protocols
    assert PI.auth_kinds == frozenset({AuthKind.API_KEY})
    assert AuthKind.OAUTH not in PI.auth_kinds
    assert adapter("pi") is ADAPTERS["pi"]
    assert servable_roles(PI.capabilities) == ["manager", "planner", "worker", "reviewer"]


def test_the_probe_requires_the_launch_flags() -> None:
    assert PROBE_HELP_TOKENS == (
        "-p", "--mode", "json", "rpc", "--offline", "--no-approve", "--no-context-files", "--no-extensions",
        "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes", "-e", "--provider", "--model",
        "--thinking", "--system-prompt", "--append-system-prompt", "--tools", "--no-session", "--session-dir",
        "--session",
    )


def _help(*without: str) -> str:
    return " ".join(token for token in PROBE_HELP_TOKENS if token not in without)


def _fake(path: Path, *, version: str = "1.3.0", help_text: str | None = None, empty: bool = False,
          help_code: int = 0) -> str:
    """A stand-in pi. ``empty`` makes ``--version`` print nothing."""
    if help_text is None:
        help_text = _help()
    version_line = "sys.exit(0)\n" if empty else f"print({version!r})\n    sys.exit(0)\n"
    return fake_claude(path, (
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        f"    {version_line}"
        "if args == ['--help']:\n"
        f"    print({help_text!r})\n"
        f"    sys.exit({help_code})\n"
        "sys.exit(9)\n"
    ))


@pytest.mark.parametrize("version", ["1.3.0", "1.10.0", "2.0.0"])
def test_a_new_enough_pi_with_the_flags_probes_clean_and_offers_every_role(tmp_path: Path, version: str) -> None:
    probe = PI.probe(_fake(tmp_path / "pi", version=version))
    assert probe == Probe(version, PI.capabilities)


def test_a_missing_command_is_refused(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing-pi")
    probe = PI.probe(missing)
    assert probe.version is None and probe.capabilities == frozenset()
    assert probe.error == f"{missing!r} was not found"


def test_empty_version_output_is_refused(tmp_path: Path) -> None:
    probe = PI.probe(_fake(tmp_path / "pi", empty=True))
    assert probe == Probe(None, frozenset(), f"{tmp_path / 'pi'} --version printed nothing")


def test_a_timeout_names_the_command_and_the_deadline_is_ten_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        seen["env"] = kwargs["env"]
        raise subprocess.TimeoutExpired(command, float(kwargs["timeout"]))  # type: ignore[arg-type]

    monkeypatch.setattr("looper.agents.runtimes.pi.subprocess.run", run)
    probe = PI.probe("pi")
    assert probe.version is None and probe.capabilities == frozenset()
    assert probe.error is not None and probe.error.startswith("pi --version did not run: TimeoutExpired:")
    assert seen["command"] == ["pi", "--version"]
    assert seen["timeout"] == 10.0
    env = seen["env"]
    assert isinstance(env, dict)
    assert not Path(env["HOME"]).exists()


def test_an_old_version_asks_for_an_upgrade(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", version="1.2.9")
    probe = PI.probe(executable)
    assert probe.version == "1.2.9" and probe.capabilities == frozenset()
    assert probe.error == (f"1.2.9 from {executable} --version is older than 1.3.0, the oldest pi Looper supports; "
                           "upgrade pi and probe again")


def test_an_unreadable_version_asks_for_1_3_0(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", version="pi dev")
    probe = PI.probe(executable)
    assert probe.version == "pi dev" and probe.capabilities == frozenset()
    assert probe.error == (f"{executable} --version printed 'pi dev', which is not a pi version Looper can read; "
                           "install pi 1.3.0 or newer and probe again")


def test_help_without_a_required_flag_names_it_and_not_a_longer_flag(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", help_text=_help("--no-mcp", "--session") + " --session-id")
    probe = PI.probe(executable)
    assert probe.version == "1.3.0" and probe.capabilities == frozenset()
    assert probe.error == (f"{executable} --help does not show --no-mcp, --session; "
                           "upgrade pi to 1.3.0 or newer and probe again")


def test_help_that_fails_keeps_the_version_and_asks_for_another_probe(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", help_text="broken", help_code=2)
    probe = PI.probe(executable)
    assert probe.version == "1.3.0" and probe.capabilities == frozenset()
    assert probe.error == f"{executable} --help failed (exit code 2): broken; probe again"


def test_a_probe_uses_a_private_offline_home_and_only_version_and_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = tmp_path / "operator"
    for path in (operator, operator / "agent", operator / "sessions", operator / "package"):
        path.mkdir()
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(operator / "agent"))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(operator / "sessions"))
    monkeypatch.setenv("PI_PACKAGE_DIR", str(operator / "package"))
    monkeypatch.setenv("PI_OFFLINE", "0")
    monkeypatch.setenv("PI_TELEMETRY", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(operator / "xdg"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operator")
    monkeypatch.setenv("CODEX_HOME", str(operator / "codex"))
    monkeypatch.setenv("GROK_HOME", str(operator / "grok"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(operator / "claude"))
    record = tmp_path / "record.jsonl"
    body = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"record = Path({str(record)!r})\n"
        "env = os.environ\n"
        "entry = {\n"
        "    'argv': sys.argv[1:], 'cwd': os.getcwd(), 'home': env.get('HOME'),\n"
        "    'agent': env.get('PI_CODING_AGENT_DIR'), 'session': env.get('PI_CODING_AGENT_SESSION_DIR'),\n"
        "    'offline': env.get('PI_OFFLINE'), 'telemetry': env.get('PI_TELEMETRY'),\n"
        "    'skip_version': env.get('PI_SKIP_VERSION_CHECK'),\n"
        "    'leaked': sorted(name for name in ('ANTHROPIC_API_KEY', 'PI_PACKAGE_DIR', 'CODEX_HOME', 'GROK_HOME',\n"
        "                                       'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME') if name in env),\n"
        "    'modes': {name: (Path(env[name]).stat().st_mode & 0o777) for name in\n"
        "              ('HOME', 'PI_CODING_AGENT_DIR', 'PI_CODING_AGENT_SESSION_DIR')},\n"
        "}\n"
        "with record.open('a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(entry) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('1.3.0')\n"
        "elif sys.argv[1:] == ['--help']:\n"
        f"    print({_help()!r})\n"
        "else:\n"
        "    sys.exit(9)\n"
    )
    executable = fake_claude(tmp_path / "pi", body)
    probe = PI.probe(executable)
    assert probe == Probe("1.3.0", PI.capabilities)
    entries = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [entry["argv"] for entry in entries] == [["--version"], ["--help"]]
    home, agent, session = Path(entries[0]["home"]), Path(entries[0]["agent"]), Path(entries[0]["session"])
    for entry in entries:
        assert Path(entry["cwd"]).resolve() == home.resolve()
        assert (entry["home"], entry["agent"], entry["session"]) == (str(home), str(agent), str(session))
        assert (entry["offline"], entry["telemetry"], entry["skip_version"]) == ("1", "0", "1")
        assert entry["leaked"] == []
        assert entry["modes"] == {"HOME": 0o700, "PI_CODING_AGENT_DIR": 0o700,
                                  "PI_CODING_AGENT_SESSION_DIR": 0o700}
    assert agent == home / "agent"
    assert session.name == "session" and not session.is_relative_to(home)
    assert home != operator and agent != operator / "agent" and session != operator / "sessions"
    assert not home.exists() and not session.exists()
    assert {path.name for path in operator.iterdir()} == {"agent", "sessions", "package"}
    assert all(not any((operator / name).iterdir()) for name in ("agent", "sessions", "package"))


@pytest.mark.parametrize("call", [
    lambda: PI.consult_command("pi", cwd=".", prompt_file=Path("prompt"), launch=None, session_id="s",  # type: ignore[arg-type]
                               session_dir=Path("sessions"), fork_dir=Path("fork"), max_turns=3),
])
def test_an_unsupported_or_missing_session_path_is_refused(call: object) -> None:
    with pytest.raises(RuntimeUnsupported, match="pi"):
        call()  # type: ignore[operator]


KEY = "sk-test-pi-provider-only"


def _provider(**fields: Any) -> ProviderSpec:
    return ProviderSpec(**{"id": "prov-test", "name": "arbitrary", "auth": AuthKind.API_KEY,
                           "protocols": frozenset({ApiProtocol.OPENAI_RESPONSES}),
                           "base_url": "https://endpoint.example.invalid/v1", "api_key": KEY, **fields})


def _launch(provider: ProviderSpec, model: str = "selected", effort: str = "high") -> Launch:
    profile = AgentProfile("agent-pi", "pi/test", RuntimeSpec("rt-pi", "pi", "pi"), provider, model, effort)
    return Launch(AgentSettings(profile=profile))


def _command(tmp_path: Path, launch: Launch | None = None, session_dir: Path | None = None) -> list[str]:
    prompt = tmp_path / ".local" / "prompts" / "prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Looper prompt")
    return PI.run_command("pi", cwd=tmp_path, prompt_file=prompt, launch=launch or _launch(_provider()),
                          session_dir=session_dir)


def _command_env(command: list[str]) -> dict[str, str]:
    assert command[0] == "env"
    return dict(arg.split("=", 1) for arg in command[1:] if "=" in arg)


def _with_mcp(tmp_path: Path, config: Any, launch: Launch | None = None) -> Launch:
    from looper.agents.library import AgentLibrary

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config))
    return replace(launch or _launch(_provider()), library=AgentLibrary(mcp_config=str(path)))


@pytest.mark.parametrize("role", ["manager", "planner"])
def test_bridge_uses_the_scoped_library_credentials_only(tmp_path: Path, role: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.agent_settings import AgentType
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.planner import PlannerOps
    from looper.agents.ops.service import ManagerOps

    url, token = "http://127.0.0.1:19321", f"scoped-{role}-token"
    monkeypatch.setenv("LOOPER_AGENT_TOKEN", "operator-token")
    monkeypatch.setenv("LOOPER_PI_AGENT_TOKEN", "stale-extension-token")
    if role == "manager":
        library = asyncio.run(ManagerOps(None, url=url, token=token).library_with_server(AgentLibrary(), tmp_path))
    else:
        library = asyncio.run(PlannerOps(None, url=url).library_with_server(AgentLibrary(), tmp_path, token))
    launch = replace(_launch(_provider()), library=library, role=AgentType(role))
    assert launch.env == ()
    env = launch.environment()
    assert env["LOOPER_AGENT_URL"] == url
    assert env["LOOPER_AGENT_TOKEN"] == token
    assert "LOOPER_PI_AGENT_URL" not in env and "LOOPER_PI_AGENT_TOKEN" not in env
    command = _command(tmp_path, launch)
    home = Path(_command_env(command)["HOME"])
    exposed = repr(command) + json.dumps(launch.record()) + repr(PI.prompt_blocks(launch, "Looper prompt"))
    exposed += "".join(p.read_text() for p in home.rglob("*") if p.is_file())
    for secret in (url, token, "operator-token", "stale-extension-token"):
        assert secret not in exposed
    settings = json.loads((home / "agent/settings.json").read_text())
    assert "LOOPER_PI_AGENT_URL" in settings["shellCommandPrefix"]
    assert "LOOPER_PI_AGENT_TOKEN" in settings["shellCommandPrefix"]
    shell = subprocess.run(["/bin/sh", "-c", settings["shellCommandPrefix"] + " env"],
                           env=env, capture_output=True, text=True, timeout=5, check=True)
    assert url not in shell.stdout and token not in shell.stdout


@pytest.mark.parametrize("config", [None, {}, {"mcpServers": {}}, {"mcpServers": {"other": {"command": "false"}}}])
def test_bridge_is_absent_without_a_looper_entry(tmp_path: Path, config: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPER_AGENT_URL", "http://operator.invalid")
    monkeypatch.setenv("LOOPER_AGENT_TOKEN", "operator-token")
    launch = _launch(_provider()) if config is None else _with_mcp(tmp_path, config)
    assert not {"LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN", "LOOPER_PI_AGENT_URL", "LOOPER_PI_AGENT_TOKEN"} \
        & launch.environment().keys()
    assert "operator-token" not in repr(_command(tmp_path, launch)) + json.dumps(launch.record())


@pytest.mark.parametrize("entry", [
    None, [], "bad", {}, {"env": None}, {"env": []}, {"env": "bad"},
    {"env": {"LOOPER_AGENT_URL": "http://localhost:19321"}},
    {"env": {"LOOPER_AGENT_TOKEN": "secret"}},
    *({"env": {"LOOPER_AGENT_URL": value, "LOOPER_AGENT_TOKEN": "secret"}}
      for value in (None, 3, "", " ", "localhost:19321", "file:///tmp/bridge", "http://", "http://x:bad",
                    "http://user:password@localhost", "http://x?secret", "http://x#secret", "http://x\n")),
    *({"env": {"LOOPER_AGENT_URL": "http://localhost:19321", "LOOPER_AGENT_TOKEN": value}}
      for value in (None, 3, "", " ", "secret\nheader", "secret\x00", " secret")),
])
def test_invalid_bridge_credentials_fail_before_launch(tmp_path: Path, entry: Any) -> None:
    launch = _with_mcp(tmp_path, {"mcpServers": {"looper": entry}})
    with pytest.raises(RuntimeUnsupported, match="looper.*(env|URL|TOKEN)") as error:
        launch.environment()
    assert "secret" not in str(error.value) and "password" not in str(error.value)
    with pytest.raises(RuntimeUnsupported):
        _command(tmp_path, launch)
    assert not (tmp_path / ".local/pi").exists()


@pytest.mark.parametrize("contents", ["{broken secret", "[]", '{"mcpServers": []}', '{"mcpServers": null}'])
def test_invalid_mcp_configuration_fails_closed(tmp_path: Path, contents: str) -> None:
    launch = _with_mcp(tmp_path, {})
    path = Path(launch.library.mcp_config)
    path.write_text(contents)
    with pytest.raises(RuntimeUnsupported, match="MCP") as error:
        _command(tmp_path, launch)
    assert "secret" not in str(error.value)
    path.unlink()
    with pytest.raises(RuntimeUnsupported, match="MCP"):
        launch.environment()


@pytest.mark.parametrize("source", ["provider", "role"])
@pytest.mark.parametrize("name", ["LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN", "LOOPER_PI_AGENT_URL", "LOOPER_PI_AGENT_TOKEN"])
def test_explicit_env_cannot_replace_the_bridge(tmp_path: Path, source: str, name: str) -> None:
    launch = _launch(_provider(env=((name, "malicious"),)) if source == "provider" else _provider())
    if source == "role":
        launch = replace(launch, settings=replace(launch.settings, env=((name, "malicious"),)))
    launch = _with_mcp(tmp_path, {"mcpServers": {"looper": {
        "command": "never-run", "env": {"LOOPER_AGENT_URL": "http://localhost:19321", "LOOPER_AGENT_TOKEN": "scoped"},
    }}}, launch)
    with pytest.raises(RuntimeUnsupported, match=name):
        _command(tmp_path, launch)
    assert not (tmp_path / ".local/pi").exists()


def test_catalog_items_are_skipped_without_running_commands_or_loading_plugins(tmp_path: Path) -> None:
    from looper.agents.library import ItemKind, LoadedItem

    sentinel = tmp_path / "command-was-run"
    launch = _with_mcp(tmp_path, {"mcpServers": {
        "looper": {"command": "touch", "args": [str(sentinel)], "env": {
            "LOOPER_AGENT_URL": "https://localhost:19321", "LOOPER_AGENT_TOKEN": "scoped",
            "UNTRUSTED": "must-not-propagate"}},
        "other": {"command": "touch", "args": [str(sentinel)]},
    }})
    items = (LoadedItem(ItemKind.MCP, "looper", "builtin", plugin="", scope="builtin"),
             LoadedItem(ItemKind.MCP, "other", "mcp", plugin=""),
             LoadedItem(ItemKind.SKILL, "guide", "skill"), LoadedItem(ItemKind.PLUGIN, "helper", "plugin"))
    launch = replace(launch, library=replace(launch.library, items=items, plugin_dirs=(str(tmp_path / "plugin"),)))
    command = _command(tmp_path, launch)
    assert "UNTRUSTED" not in launch.environment()
    assert not sentinel.exists()
    skipped = launch.record()["runtime_catalog"]["skipped"]
    assert len(skipped) == 3
    assert all(any(name in entry for entry in skipped) for name in ("other", "guide", "helper"))
    assert all("looper" not in entry for entry in skipped)
    assert "--mcp-config" not in command and "--plugin-dir" not in command
    assert "must-not-propagate" not in repr(command) + json.dumps(launch.record())


def test_bridge_rotation_does_not_put_credentials_in_home_or_provider_hash(tmp_path: Path) -> None:
    first = _with_mcp(tmp_path, {"mcpServers": {"looper": {"env": {
        "LOOPER_AGENT_URL": "http://localhost:19321", "LOOPER_AGENT_TOKEN": "manager-token"}}}})
    first_env, first_command = first.environment(), _command(tmp_path, first)
    second = _with_mcp(tmp_path, {"mcpServers": {"looper": {"env": {
        "LOOPER_AGENT_URL": "http://localhost:19321", "LOOPER_AGENT_TOKEN": "task-token"}}}})
    assert first_env["LOOPER_AGENT_TOKEN"] == "manager-token"
    assert second.environment()["LOOPER_AGENT_TOKEN"] == "task-token"
    assert first.binding_sha256 == second.binding_sha256
    assert _command_env(first_command)["HOME"] == _command_env(_command(tmp_path, second))["HOME"]


def test_json_launch_uses_pi_flags_and_private_directories(tmp_path: Path) -> None:
    command = _command(tmp_path)
    env = _command_env(command)
    args = command[command.index("pi") + 1:]
    prompt = Path(args[args.index("--append-system-prompt") + 1])
    assert prompt.parent == Path(env["HOME"]) / "prompts"
    assert prompt.read_text().endswith("Looper prompt")
    assert args == [
        "-p", "--mode", "json", "--offline", "--no-approve", "--no-context-files", "--no-extensions",
        "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes",
        "-e", str(files("looper.agents.runtimes").joinpath("pi_extension/looper.ts")),
        "--provider", "looper", "--model", "selected", "--thinking", "high",
        "--append-system-prompt", str(prompt), "--no-session",
    ]
    assert not {"--config", "--no-ui"} & set(args)
    assert "PI_SHELL_PREFIX" not in env
    home = Path(env["HOME"])
    assert home.parent == tmp_path / ".local/pi/homes"
    assert env["PI_CODING_AGENT_DIR"] == str(home / "agent")
    assert Path(env["PI_CODING_AGENT_SESSION_DIR"]).parent == tmp_path / ".local/pi/sessions"
    assert KEY not in repr(command) + "".join(p.read_text() for p in home.rglob("*.json"))
    for path in (home.parent.parent, home.parent, home, home / "agent",
                 Path(env["PI_CODING_AGENT_SESSION_DIR"])):
        assert path.stat().st_mode & 0o777 == 0o700
    for name in ("settings.json", "models.json"):
        assert (home / "agent" / name).stat().st_mode & 0o777 == 0o600
    settings = json.loads((home / "agent/settings.json").read_text())
    assert settings["cacheWarming"] == "off"
    assert settings["enableAnalytics"] is settings["enableInstallTelemetry"] is False
    assert settings["defaultProjectTrust"] == "never"
    for resource in ("packages", "extensions", "skills", "prompts", "themes"):
        assert settings[resource] == []


def test_same_configuration_reuses_home_but_never_a_default_session(tmp_path: Path) -> None:
    first = _command_env(_command(tmp_path))
    settings = Path(first["PI_CODING_AGENT_DIR"]) / "settings.json"
    before = settings.stat()
    second = _command_env(_command(tmp_path))
    assert first["HOME"] == second["HOME"]
    assert (settings.stat().st_ino, settings.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert first["PI_CODING_AGENT_SESSION_DIR"] != second["PI_CODING_AGENT_SESSION_DIR"]
    for env in (first, second):
        assert not Path(env["PI_CODING_AGENT_SESSION_DIR"]).is_relative_to(env["HOME"])


@pytest.mark.parametrize("fields", [{"base_url": "https://other.invalid/v1"}, {"api_key": "different-key"}])
def test_different_provider_configuration_gets_a_separate_home(tmp_path: Path, fields: dict[str, str]) -> None:
    first = _command_env(_command(tmp_path))
    second = _command_env(_command(tmp_path, _launch(_provider(**fields))))
    assert first["HOME"] != second["HOME"]


def test_explicit_sessions_are_private_and_outside_the_shared_home(tmp_path: Path) -> None:
    sessions = [tmp_path / ".local/pi/sessions" / owner for owner in ("worker-a", "worker-b")]
    commands = [_command(tmp_path, session_dir=path) for path in sessions]
    assert _command_env(commands[0])["HOME"] == _command_env(commands[1])["HOME"]
    for path, command in zip(sessions, commands):
        assert command[-2:] == ["--session-dir", str(path)]
        assert "--no-session" not in command
        assert _command_env(command)["PI_CODING_AGENT_SESSION_DIR"] == str(path)
        assert path.stat().st_mode & 0o777 == 0o700
    with pytest.raises(RuntimeUnsupported, match="session"):
        _command(tmp_path, session_dir=Path(_command_env(commands[0])["HOME"]) / "sessions")


def test_concurrent_home_publication_is_complete_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes.pi import models_json, settings_json, write_home

    rename = os.rename
    published: list[Path] = []
    barrier = threading.Barrier(8)

    def publish(source: Path, target: Path) -> None:
        for name in ("models.json", "settings.json"):
            assert json.loads((source / "agent" / name).read_text())
        assert not (source / "agent/sessions").exists()
        barrier.wait(timeout=10)
        rename(source, target)
        published.append(target)

    monkeypatch.setattr("looper.agents.runtimes.pi.os.rename", publish)
    launch = _launch(_provider())
    with ThreadPoolExecutor(max_workers=8) as pool:
        homes = list(pool.map(lambda _: write_home(tmp_path, models_json(launch), settings_json(),
                                                  binding_sha256=launch.binding_sha256), range(8)))
    assert len(set(homes)) == len(published) == 1
    assert list(homes[0].parent.iterdir()) == [homes[0]]


def test_a_failed_home_write_publishes_nothing_and_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes.pi import models_json, settings_json, write_home

    launch = _launch(_provider())
    with monkeypatch.context() as patch:
        replace_file = os.replace
        def fail(*args: object) -> None:
            if Path(args[1]).name == "settings.json":
                raise OSError("disk full")
            replace_file(*args)
        patch.setattr("looper.agents.runtimes.pi.os.replace", fail)
        with pytest.raises(OSError, match="disk full"):
            write_home(tmp_path, models_json(launch), settings_json(), binding_sha256=launch.binding_sha256)
    assert list((tmp_path / "pi/homes").iterdir()) == []
    home = write_home(tmp_path, models_json(launch), settings_json(), binding_sha256=launch.binding_sha256)
    assert json.loads((home / "agent/models.json").read_text())


@pytest.mark.parametrize("umask", [0o000, 0o777])
def test_private_permissions_do_not_depend_on_the_server_umask(tmp_path: Path, umask: int) -> None:
    from looper.agents.runtimes.pi import models_json, settings_json, write_home

    launch = _launch(_provider())
    previous = os.umask(umask)
    try:
        home = write_home(tmp_path, models_json(launch), settings_json(), binding_sha256=launch.binding_sha256)
    finally:
        os.umask(previous)
    for path in (tmp_path / "pi", home.parent, home, home / "agent"):
        assert path.stat().st_mode & 0o777 == 0o700
    for name in ("models.json", "settings.json"):
        assert (home / "agent" / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("source", ["provider", "role"])
@pytest.mark.parametrize("name", ["HOME", "XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR", "PI_CONFIG_FILES",
                                  "PI_SHELL_PREFIX", "PI_OFFLINE", "PI_FUTURE_PATH", "LOOPER_AGENT_TOKEN",
                                  "LOOPER_PROVIDER_API_KEY"])
def test_explicit_environment_cannot_override_isolation(source: str, name: str) -> None:
    launch = _launch(_provider(env=((name, "override"),)) if source == "provider" else _provider())
    if source == "role":
        launch = replace(launch, settings=replace(launch.settings, env=((name, "override"),)))
    with pytest.raises(RuntimeUnsupported, match=name):
        launch.environment()


@pytest.mark.parametrize(("protocols", "api"), [
    ([ApiProtocol.OPENAI_RESPONSES], "openai-responses"),
    ([ApiProtocol.OPENAI_CHAT], "openai-completions"),
    ([ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT], "openai-responses"),
    ([ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES], "openai-responses"),
    ([ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT], "openai-completions"),
])
def test_binding_selects_the_protocol_and_keeps_the_key_only_in_env(protocols: list[ApiProtocol], api: str) -> None:
    from looper.agents.runtimes.pi import models_json, provider_api

    provider = _provider(protocols=frozenset(protocols), env=(("REGION", "test"),))
    launch = _launch(provider)
    binding = PI.bind_provider(provider, launch.model, launch.effort)
    config = models_json(launch)
    assert provider_api(provider) == config["providers"]["looper"]["api"] == api
    assert binding.args == ("--provider", "looper", "--model", "selected", "--thinking", "high")
    assert dict(binding.env) == {"REGION": "test", "LOOPER_PROVIDER_API_KEY": KEY}
    assert config["providers"]["looper"]["apiKey"] == "$LOOPER_PROVIDER_API_KEY"
    assert KEY not in json.dumps(config) + repr(binding) + repr(binding.args) + json.dumps(launch.record())
    assert "LOOPER_PROVIDER_API_KEY" in PI.provider_env_owned


@pytest.mark.parametrize("name", ["DeepSeek", "openai", "anthropic-oauth", "unknown"])
def test_endpoint_and_protocol_do_not_depend_on_the_provider_name(name: str) -> None:
    from looper.agents.runtimes.pi import models_json

    provider = _provider(name=name, protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
                         base_url="https://api.deepseek.com")
    assert models_json(_launch(provider))["providers"]["looper"]["baseUrl"] == "https://api.deepseek.com"


@pytest.mark.parametrize(("fields", "error"), [
    ({"api_key": None}, "API key"), ({"api_key": ""}, "API key"), ({"api_key": " \t"}, "API key"),
    ({"base_url": ""}, "base_url"), ({"base_url": "  "}, "base_url"),
    ({"auth": AuthKind.OAUTH, "oauth_runtime_type": "pi"}, "API key"),
    ({"protocols": frozenset({ApiProtocol.ANTHROPIC_MESSAGES})}, "protocol"),
    ({"protocols": frozenset()}, "protocol"),
])
def test_invalid_providers_are_refused_before_binding_or_rendering(fields: dict[str, Any], error: str) -> None:
    from looper.agents.runtimes.pi import models_json

    provider = _provider(**fields)
    for call in (lambda: PI.bind_provider(provider, "selected", "high"), lambda: models_json(_launch(provider))):
        with pytest.raises(RuntimeUnsupported, match=error):
            call()


@pytest.mark.parametrize("effort", ["", "off", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_only_pi_thinking_levels_are_passed(effort: str) -> None:
    binding = PI.bind_provider(_provider(), "selected", effort)
    assert binding.args == ("--provider", "looper", "--model", "selected", *(
        ("--thinking", effort) if effort else ()))


def test_an_invalid_thinking_level_is_refused() -> None:
    with pytest.raises(RuntimeUnsupported, match="thinking"):
        PI.bind_provider(_provider(), "selected", "ultra")


@pytest.mark.parametrize(("entry", "reasoning", "context"), [
    (None, True, None),
    (CatalogModel("selected"), True, None),
    (CatalogModel("selected", context_window=128_000, efforts=()), False, 128_000),
    (CatalogModel("selected", context_window=256_000, efforts=("low", "high")), True, 256_000),
])
def test_models_json_contains_only_the_selected_model_with_catalog_metadata(
    entry: CatalogModel | None, reasoning: bool, context: int | None,
) -> None:
    from looper.agents.runtimes.pi import models_json

    catalog = (CatalogModel("other", context_window=1_000_000), entry) if entry else ()
    provider = _provider(catalog=catalog)
    rendered = models_json(_launch(provider))
    expected: dict[str, Any] = {"id": "selected", "name": "selected", "input": ["text"], "reasoning": reasoning,
                                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                                "compat": {"supportsStrictMode": True}}
    if context:
        expected["contextWindow"] = context
    assert rendered == {"providers": {"looper": {"api": "openai-responses", "baseUrl": provider.base_url,
                                                  "apiKey": "$LOOPER_PROVIDER_API_KEY", "models": [expected]}}}
    chat = replace(provider, protocols=frozenset({ApiProtocol.OPENAI_CHAT}))
    assert "compat" not in models_json(_launch(chat))["providers"]["looper"]["models"][0]


def test_a_model_outside_a_nonempty_catalog_is_refused() -> None:
    from looper.agents.runtimes.pi import models_json

    provider = _provider(catalog=(CatalogModel("known"),))
    with pytest.raises(RuntimeUnsupported, match="model"):
        PI.bind_provider(provider, "unknown", "high")
    with pytest.raises(RuntimeUnsupported, match="model"):
        models_json(_launch(provider, model="unknown"))


@pytest.mark.parametrize("transport", ["http", "fetch"])
@pytest.mark.parametrize("configured_compat", [False, True])
def test_pi_responses_wire_preserves_optional_tool_parameters(
    tmp_path: Path, configured_compat: bool, transport: str,
) -> None:
    """Use installed pi 1.3's model loader and HTTP client, without a login or a real model endpoint."""
    from looper.agents.runtimes.pi import models_json

    executable = shutil.which("pi")
    node = shutil.which("node")
    if not executable or not node:
        pytest.skip("pi 1.3 and Node are required for the local Responses compatibility check")
    package = next((p for p in Path(executable).resolve().parents if (p / "package.json").exists()), None)
    if package is None or json.loads((package / "package.json").read_text()).get("version") != "1.3.0":
        pytest.skip("the compatibility check targets installed pi 1.3.0")

    requests: list[dict[str, Any]] = []

    class Responses(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            response = {"id": "resp-test", "status": "completed", "output": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}
            data = ("event: response.completed\ndata: " + json.dumps(
                {"type": "response.completed", "response": response}) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    try:
        service = ThreadingHTTPServer(("127.0.0.1", 0), Responses) if transport == "http" else nullcontext()
    except PermissionError:
        pytest.skip("sandbox does not permit a local HTTP listener; fetch transport is tested separately")
    with service as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True) if server else None
        if thread:
            thread.start()
        try:
            provider = _provider(base_url=f"http://127.0.0.1:{server.server_port if server else 1}/v1")
            config = models_json(_launch(provider))
            if not configured_compat:
                del config["providers"]["looper"]["models"][0]["compat"]
            models_path = tmp_path / "models.json"
            models_path.write_text(json.dumps(config))
            script = tmp_path / "request.mjs"
            script.write_text('''
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [pkg, root, transport] = process.argv.slice(2);
const { ModelRuntime } = await import(pathToFileURL(join(pkg, 'dist/core/model-runtime.js')));
const runtime = await ModelRuntime.create({
  authPath: join(root, 'auth.json'), modelsPath: join(root, 'models.json'), refreshOnCreate: false,
});
if (runtime.getError()) throw new Error(runtime.getError());
const model = runtime.getModel('looper', 'selected');
if (!model) throw new Error('configured model was not loaded');
const requests = [];
const fetch = transport === 'fetch' ? async (url, init) => {
  requests.push({path: new URL(url).pathname, authorization: new Headers(init.headers).get('authorization'),
    body: JSON.parse(init.body)});
  const response = {id: 'resp-test', status: 'completed', output: [],
    usage: {input_tokens: 1, output_tokens: 0, total_tokens: 1}};
  return new Response('event: response.completed\\ndata: ' + JSON.stringify({
    type: 'response.completed', response}) + '\\n\\n', {headers: {'Content-Type': 'text/event-stream'}});
} : undefined;
const result = await runtime.complete(model, {
  messages: [{role: 'user', content: 'test', timestamp: 0}],
  tools: [{name: 'example', description: 'test', parameters: {
    type: 'object', properties: {required_value: {type: 'string'}, optional_value: {type: 'string'}},
    required: ['required_value'], additionalProperties: false,
  }}],
}, {signal: AbortSignal.timeout(10000), fetch});
if (result.stopReason === 'error') throw new Error(result.errorMessage);
console.log(JSON.stringify({requests, contextWindow: model.contextWindow, maxTokens: model.maxTokens}));
''')
            home, agent, session = tmp_path / "home", tmp_path / "agent", tmp_path / "sessions"
            for path in (home, agent, session):
                path.mkdir()
            from looper.agents.runtimes.pi import _probe_env
            env = _probe_env(home, agent, session)
            env.update(dict(PI.bind_provider(provider, "selected", "").env))
            env.update({"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
            done = subprocess.run([node, str(script), str(package), str(tmp_path), transport], cwd=tmp_path, env=env,
                                  capture_output=True, text=True, timeout=20, check=False)
            assert done.returncode == 0, done.stderr
        finally:
            if server:
                server.shutdown()
            if thread:
                thread.join(timeout=2)
    defaults = json.loads(done.stdout)
    if transport == "fetch":
        requests = defaults["requests"]
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/v1/responses"
    assert request["authorization"] == f"Bearer {KEY}"  # interpolation was resolved by pi, not sent literally
    tool = request["body"]["tools"][0]
    assert tool["parameters"]["required"] == ["required_value"]
    assert set(tool["parameters"]["properties"]) == {"required_value", "optional_value"}
    if configured_compat:
        assert tool["strict"] is False
    else:
        assert "strict" not in tool  # positive control: pi's default omits the flag
    assert defaults["contextWindow"] > 0 and defaults["maxTokens"] > 0
    assert KEY not in models_path.read_text() + script.read_text()


@pytest.mark.parametrize("read_only", [False, True])
def test_every_launch_path_loads_the_same_packaged_extension(tmp_path: Path, read_only: bool) -> None:
    launch = replace(_launch(_provider()), consult=read_only)
    _command(tmp_path, launch)
    prompt = tmp_path / ".local/prompts/prompt.md"
    sessions = tmp_path / "saved"
    sessions.mkdir()
    (sessions / "source.jsonl").write_text(json.dumps({"type": "session", "id": "saved"}) + "\n")
    kwargs = dict(cwd=tmp_path, prompt_file=prompt, launch=launch)
    resident = PI.resident_session("manager")
    base = PI.resident_command("pi", **kwargs)

    async def start() -> None:
        await resident.start(base, cwd=str(tmp_path), signature=tuple(base), env=launch.environment())

    asyncio.run(start())
    first_turn = resident._turn_argv()
    owner = resident._session_dir()
    (owner / "saved.jsonl").write_text(json.dumps({"type": "session", "id": "saved"}) + "\n")
    resident._session = "saved"
    resumed = resident._turn_argv()
    commands = [PI.launch_args(launch), PI.run_command("pi", **kwargs),
                PI.run_command("pi", **kwargs, session_dir=sessions), base, first_turn, resumed,
                PI.consult_command("pi", **kwargs, session_id="saved", session_dir=sessions,
                                   fork_dir=tmp_path / "consult"),
                PI.steer_command("pi", **kwargs),
                PI.steer_command("pi", **kwargs, session_dir=sessions)]
    resource = Path(str(files("looper.agents.runtimes").joinpath("pi_extension/looper.ts")))
    assert resource.is_absolute() and resource.is_file()
    for command in commands:
        assert command.count("-e") == 1
        assert command[command.index("-e") + 1] == str(resource)
        assert "--no-extensions" in command and "--no-mcp" in command
        if read_only:
            assert command[command.index("--tools") + 1] == "read,grep,find,ls,mcp__looper__*"
    for command in commands[-2:]:
        assert command[command.index("--mode") + 1] == "rpc"
        assert "-p" not in command and "--no-ui" not in command


def test_resident_signature_tracks_agent_directory_and_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import looper.agents.runtimes.pi as pi

    _command(tmp_path)
    kwargs = dict(cwd=tmp_path, prompt_file=tmp_path / ".local/prompts/prompt.md", launch=_launch(_provider()))
    base = PI.resident_command("pi", **kwargs)
    resident = PI.resident_session("manager")
    asyncio.run(resident.start(base, cwd=str(tmp_path), signature=tuple(base)))
    assert not resident.stale(tuple(PI.resident_command("pi", **kwargs)))
    other = tmp_path / "installed" / "looper.ts"
    other.parent.mkdir()
    other.write_text("export default function () {}")
    monkeypatch.setattr(pi, "bridge_extension", lambda: other)
    changed = PI.resident_command("pi", **kwargs)
    assert changed[changed.index("-e") + 1] == str(other)
    assert resident.stale(tuple(changed))
    monkeypatch.undo()
    changed = PI.resident_command("pi", **{**kwargs, "launch": _launch(_provider(api_key="rotated"))})
    assert _command_env(changed)["PI_CODING_AGENT_DIR"] != _command_env(base)["PI_CODING_AGENT_DIR"]
    assert resident.stale(tuple(changed))


@pytest.mark.parametrize("resource", ["missing", "directory"])
def test_a_missing_packaged_bridge_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str) -> None:
    from looper.agents.runtimes.pi import bridge_extension

    if resource == "directory":
        (tmp_path / "pi_extension/looper.ts").mkdir(parents=True)
    monkeypatch.setattr("looper.agents.runtimes.pi.files", lambda _: tmp_path)
    with pytest.raises(RuntimeUnsupported, match="packaged Looper tool bridge is missing"):
        bridge_extension()
