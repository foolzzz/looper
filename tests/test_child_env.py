"""Every child starts from named variables, then receives only its run's additions."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from looper import child_env
from looper.agents import proc_tree, schedule_gate
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.channels import lark_cli
from looper.agents.profile_registry import (
    Endpoint, ModelSpec, ProviderSpec, RegistryError, ResolvedModel, RuntimeSpec, builtin_resolved_profile,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported, VendorType
from looper.agents.runtimes.grok import FIXED_ENV
from tests.runtime_model_helpers import resolved_profile
from test_codex_runtime import _launch as codex_launch, _login_provider, _operator_login
from test_dsh_runtime import PROVIDER as DSH_PROVIDER, dsh_policy  # noqa: F401 (fixture)
from test_grok_runtime import LOGIN, _launch as grok_launch, _operator
from test_omp_runtime import _launch as omp_launch
from test_pi_runtime import _command as pi_command, _launch as pi_launch, _provider as pi_provider

KEPT = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "SSL_CERT_FILE",
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK",
    # Each runtime CLI's own login: Claude Code's API key, token and `claude setup-token` token; Codex's home and the
    # keys `codex exec` reads; Grok's home and keys.
    "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "GROK_HOME",
    "XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GROK_AUTH",
)
DROPPED = (
    "LOOPER_DATABASE_URL", "LOOPX_DATABASE_URL", "PGPASSWORD", "PGHOST", "LOOPER_TELEGRAM_BOT_TOKEN",
    "LOOPER_AGENT_TOKEN", "LOOPER_RUNTIME_ROOT", "LOOPER_PUBLIC_URL", "CLAUDECODE", "AWS_SECRET_ACCESS_KEY",
    "SOME_SECRET", "DEEPSEEK_API_KEY", "DSH_HOME", "DSH_AGENTS_HOME", "DSH_TOOLS_MODE",
)
CASES = (
    ("claude-code", "login"), ("claude-code", "api-key"), ("claude-code", "endpoint"),
    ("codex", "login"), ("codex", "api-key"), ("grok", "login"), ("omp", "api-key"),
)


def test_inherited_keeps_named_variables() -> None:
    """The allowlist is data, each name checked by the same lookup: every name in one call."""
    mapping = {key: f"value-{key}" for key in (*KEPT, *DROPPED)}
    inherited = child_env.inherited(mapping)
    assert {name: inherited.get(name) for name in KEPT} == {name: mapping[name] for name in KEPT}


def test_inherited_drops_other_variables() -> None:
    mapping = {"PATH": "/bin", **{name: "secret" for name in DROPPED}}
    assert child_env.inherited(mapping) == {"PATH": "/bin"}


@pytest.mark.parametrize(("name", "kept"), [
    ("PGPASSWORD", True), ("PGHOST", True), ("DOCKER_HOST", True), ("PATH", True),
    ("LOOPER_DATABASE_URL", False), ("SOME_SECRET", False),
])
def test_backup_adds_only_database_and_docker_variables(name: str, kept: bool) -> None:
    assert (name in child_env.backup({name: "value"})) is kept


def test_runtime_root_guard_variables_reach_children(tmp_path: Path) -> None:
    from real_runtime_root_guard import PROTECTED_ROOTS_ENV, REPORT_PATH_ENV

    actual = child_env.inherited()
    for name in (PROTECTED_ROOTS_ENV, REPORT_PATH_ENV):
        assert actual[name] == os.environ[name]
    # sitecustomize installs the same allowance in a child before it starts another program.
    actual["HOME"] = str(tmp_path)
    run = subprocess.run([sys.executable, "-c",
                          "import json; from looper.child_env import inherited; print(json.dumps(inherited()))"],
                         cwd=tmp_path, env=actual, capture_output=True, text=True, check=True, timeout=30)
    forwarded = json.loads(run.stdout)
    for name in (PROTECTED_ROOTS_ENV, REPORT_PATH_ENV):
        assert forwarded[name] == actual[name]


def test_allow_adds_a_variable_for_this_process() -> None:
    assert "X_TEST" not in child_env._allowed
    try:
        child_env.allow("X_TEST")
        assert child_env.inherited({"X_TEST": "value"}) == {"X_TEST": "value"}
    finally:
        child_env._allowed.remove("X_TEST")


@pytest.fixture
def server_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    visible = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "operator"), "LANG": "C.UTF-8",
               "HTTPS_PROXY": "http://proxy.example.invalid:8080"}
    Path(visible["HOME"]).mkdir()
    for name, value in visible.items():
        monkeypatch.setenv(name, value)
    for name in DROPPED:
        monkeypatch.setenv(name, f"server-secret-{name}")
    return visible


def _api_key_model(runtime: str, auth: str, name: str) -> ResolvedModel:
    """An API-key account's model for ``runtime``: at the endpoint ``auth`` names, or its runtime default."""
    protocol = ApiProtocol.ANTHROPIC_MESSAGES if runtime == "claude-code" else ApiProtocol.OPENAI_RESPONSES
    provider = ProviderSpec("provider-test", "test", VendorType.CUSTOM, AuthKind.API_KEY, api_key="run-api-key")
    base_url = "https://provider.example.invalid/v1" if auth == "endpoint" else ""
    return ResolvedModel(ModelSpec("model-test", provider.id, name, protocol), provider,
                         Endpoint(provider.id, protocol, base_url, use_runtime_default=not base_url))


def _with_provider_env(launch: Launch, env: tuple[tuple[str, str], ...]) -> Launch:
    """``launch`` whose account carries ``env``."""
    resolved = launch.profile.resolved_model
    resolved = replace(resolved, provider=replace(resolved.provider, env=env))
    return replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, resolved_model=resolved)))


def _with_stored_env(launch: Launch, *env: tuple[str, str]) -> Launch:
    """``launch`` whose account also holds ``env`` the registry now refuses to accept (a credential-looking name or
    one a runtime binding owns), as a row stored before that rule could still hold them: every runtime must keep
    ignoring or scrubbing them."""
    provider = launch.profile.resolved_model.provider
    object.__setattr__(provider, "env", (*provider.env, *env))
    return launch


def _launch(runtime: str, auth: str) -> Launch:
    if runtime == "claude-code":
        profile = builtin_resolved_profile()
        if auth != "login":
            resolved = _api_key_model(runtime, auth, "opus")
            profile = replace(profile, model_id=resolved.model.id, resolved_model=resolved)
        launch = Launch(AgentSettings(profile=profile))
    elif runtime == "codex":
        launch = codex_launch(provider=_login_provider() if auth == "login" else _api_key_model(runtime, auth, "gpt-5.5"))
    elif runtime == "grok":
        launch = grok_launch()
    else:
        launch = omp_launch(resolved=_api_key_model(runtime, auth, "gpt-5.5"))
    # Explicit settings and provider variables must still be added, even though neither is in the allowlist.
    launch = _with_provider_env(launch, (("PROVIDER_SETTING", "bound"),))
    return replace(launch, settings=replace(launch.settings, env=(("RUN_SETTING", "configured"),)))


def _assert_environment(env: dict[str, str], visible: dict[str, str], runtime: str, auth: str) -> None:
    assert not set(DROPPED) & env.keys()
    for name, value in visible.items():
        if name != "HOME":
            assert env[name] == value
    assert env["HOME"] == visible["HOME"]
    assert env["RUN_SETTING"] == "configured" and env["PROVIDER_SETTING"] == "bound"
    if auth != "login":
        key = ("ANTHROPIC_AUTH_TOKEN" if auth == "endpoint" else "ANTHROPIC_API_KEY") \
            if runtime == "claude-code" else "LOOPER_PROVIDER_API_KEY"
        assert env[key] == "run-api-key"
    if runtime == "grok":
        assert all(env[name] == value for name, value in FIXED_ENV)


@pytest.mark.parametrize(("runtime", "auth"), CASES)
def test_runtime_environments_keep_only_allowed_and_explicit_variables(
    runtime: str, auth: str, server_environment: dict[str, str],
) -> None:
    _assert_environment(_launch(runtime, auth).environment(), server_environment, runtime, auth)


@pytest.mark.parametrize("runtime", ["grok", "omp"])
def test_unsupported_authentication_stays_unsupported(runtime: str, server_environment: dict[str, str]) -> None:
    launch = _launch(runtime, "api-key" if runtime == "grok" else "login")
    resolved = LOGIN if runtime == "omp" else _api_key_model(runtime, "api-key", "grok-4.7")
    launch = replace(launch, settings=replace(launch.settings, profile=replace(
        launch.profile, model_id=resolved.model.id, resolved_model=resolved)))
    with pytest.raises((RegistryError, RuntimeUnsupported)):
        launch.environment()


@pytest.mark.parametrize(("runtime", "auth"), CASES)
def test_real_runtime_commands_do_not_receive_server_secrets(
    runtime: str, auth: str, server_environment: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if runtime == "codex" and auth == "login":
        _operator_login(tmp_path, monkeypatch, codex_home_set=True)
    if runtime == "grok":
        _operator(tmp_path, monkeypatch)
    executable = tmp_path / "stand-in"
    executable.write_text(f"#!{sys.executable}\n"
                          "import json, os\nfrom pathlib import Path\n"
                          "Path(__file__).with_suffix('.json').write_text(json.dumps(dict(os.environ)))\n")
    executable.chmod(0o755)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    launch = _launch(runtime, auth)
    command = launch.runtime.run_command(str(executable), cwd=tmp_path, prompt_file=prompt, launch=launch)
    subprocess.run(command, env=launch.environment(), cwd=tmp_path, input=b"", timeout=30, check=True)
    actual = json.loads(executable.with_suffix(".json").read_text())
    visible = dict(server_environment)
    if runtime in ("grok", "omp"):
        visible["HOME"] = command[2 if runtime == "grok" else 1].partition("=")[2]
    _assert_environment(actual, visible, runtime, auth)


@pytest.mark.parametrize("env", [{"LOOPER_SCHEDULE_ID": "s1"}, None])
def test_gate_environment_is_allowlisted_with_or_without_additions(
    env: dict[str, str] | None, server_environment: dict[str, str], tmp_path: Path,
) -> None:
    result = asyncio.run(schedule_gate.run_gate("env", str(tmp_path), 10, env=env))
    assert result.exit_code == 0 and not result.error and not result.timed_out
    actual = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert actual["PATH"] == server_environment["PATH"]
    assert not set(DROPPED) & actual.keys()
    assert actual.get("LOOPER_SCHEDULE_ID") == ("s1" if env else None)


def test_lark_environment_keeps_only_lark_and_child_variables(
    server_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LARK_PROFILE", "test-profile")
    actual = lark_cli.environment()
    assert actual["LARK_PROFILE"] == "test-profile" and actual["PATH"] == server_environment["PATH"]
    assert not set(DROPPED) & actual.keys()


@pytest.mark.parametrize("kwargs", [{}, {"env": None}])
def test_process_spawn_defaults_to_child_environment(
    kwargs: dict[str, None], server_environment: dict[str, str], tmp_path: Path,
) -> None:
    async def run() -> bytes:
        process = await proc_tree.spawn(
            sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))", cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, **kwargs)
        stdout, _ = await process.communicate()
        assert process.returncode == 0
        return stdout

    actual = json.loads(asyncio.run(run()))
    assert actual["PATH"] == server_environment["PATH"]
    assert not set(DROPPED) & actual.keys()


def test_dsh_child_uses_private_homes_and_explicit_binding(tmp_path, monkeypatch, dsh_policy):
    from looper.agents.runtimes import dsh

    names = ("HOME", "DSH_HOME", "DSH_AGENTS_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
             "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_DIRS", "XDG_DATA_DIRS")
    relatives = ("marker", ".env", ".dsh/profiles/marker", ".agents/marker")
    operator = {}
    for name in names:
        directory = tmp_path / "operator" / name
        directory.mkdir(parents=True)
        for relative in relatives:
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("operator-only-" + name)
        operator[name] = str(directory)
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-key")
    monkeypatch.setenv("DSH_PROFILE", "operator-profile")
    before = {str(p): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
              for p in (tmp_path / "operator").rglob("*")}
    # Positive control: this child really can read the markers with the operator environment.
    reader = ("import json, os; from pathlib import Path; "
              f"names = {names!r}; relatives = {relatives!r}; "
              "print(json.dumps({k: {r: (Path(os.environ[k]) / r).read_text() "
              "if (Path(os.environ[k]) / r).exists() else None for r in relatives} for k in names}))")
    control = subprocess.run([sys.executable, "-c", reader], env={**child_env.inherited(), **operator},
                             cwd=tmp_path, text=True, capture_output=True, check=True, timeout=10)
    assert json.loads(control.stdout) == {name: dict.fromkeys(relatives, "operator-only-" + name) for name in names}
    overrides = tuple(operator.items()) + (("DSH_PROFILE", "foreign"), ("DSH_TOOLS_MODE", "ptc"),
        ("DEEPSEEK_API_KEY", "foreign-key"), ("DEEPSEEK_BASE_URL", "https://wrong.invalid"),
        ("ANTHROPIC_BASE_URL", "https://wrong.invalid"), ("OPENAI_BASE_URL", "https://wrong.invalid"),
        ("CLAUDE_CONFIG_DIR", operator["HOME"]), ("PI_CODING_AGENT_DIR", operator["HOME"]),
        ("AGENTS_HOME", operator["HOME"]))
    token = "synthetic-run-token"
    provider = replace(DSH_PROVIDER, env=overrides + (("PROVIDER_SETTING", "bound"), ("LOOPER_AGENT_TOKEN", token)))
    profile = resolved_profile("test", "test", RuntimeSpec("test", "test", dsh_policy.type),
                               replace(DSH_PROVIDER, env=(("PROVIDER_SETTING", "bound"),)), "deepseek-flash")
    launch = _with_stored_env(Launch(AgentSettings(profile=profile, env=overrides + (("RUN_SETTING", "configured"),))),
                              *overrides, ("LOOPER_AGENT_TOKEN", token))
    home = dsh.write_home(tmp_path / ".local", {"looper.yml": '{"apiKeyEnv":"DEEPSEEK_API_KEY"}\n',
                          "prompt.md": "A synthetic prompt with {{literal}} text.\n"}, owner="child")
    # Exercise Launch.environment's adapter contract without registering an unfinished DSH runtime.
    monkeypatch.setattr(dsh_policy, "environment", lambda run: dsh.environment(run, home))
    actual = launch.environment()
    assert actual["DEEPSEEK_API_KEY"] == DSH_PROVIDER.api_key
    assert actual["LOOPER_AGENT_TOKEN"] == token
    assert actual["RUN_SETTING"] == "configured" and actual["PROVIDER_SETTING"] == "bound"
    assert not {"DSH_PROFILE", "DSH_TOOLS_MODE", "DEEPSEEK_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
                "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "AGENTS_HOME"} & actual.keys()
    assert dsh.validate_provider(launch.profile.provider, launch.model).base_url == DSH_PROVIDER.base_url
    command = [sys.executable, "-c", reader]
    isolated = subprocess.run(command, env=actual, cwd=tmp_path, text=True,
                              capture_output=True, check=True, timeout=10)
    assert json.loads(isolated.stdout) == dict.fromkeys(names, dict.fromkeys(relatives))
    assert actual | home.paths == actual
    account = replace(profile.resolved_model.provider, api_key="rotated-dsh-key", env=())
    rotated = _with_stored_env(replace(launch, settings=replace(launch.settings, profile=replace(
        profile, resolved_model=replace(profile.resolved_model, provider=account)))),
        ("LOOPER_AGENT_TOKEN", "rotated-run-token"))
    rotated_env = rotated.environment()
    assert {name: rotated_env[name] for name in names} == home.paths
    assert rotated_env["DEEPSEEK_API_KEY"] == "rotated-dsh-key"
    assert rotated_env["LOOPER_AGENT_TOKEN"] == "rotated-run-token"
    for secret in (DSH_PROVIDER.api_key, token, "rotated-dsh-key", "rotated-run-token", "foreign-key", "ambient-key"):
        assert secret not in repr(command)
        assert secret not in repr(launch.record())
        assert secret not in repr(rotated.record())
        assert secret not in repr(dsh.bind_provider(provider, "deepseek-flash"))
        assert secret not in repr(home)
        assert all(secret not in p.read_text() for p in home.config.rglob("*") if p.is_file())
    for path in home.config.rglob("*"):
        if path.is_file():
            assert "operator-only-" not in path.read_text()
            assert all(value not in path.read_text() for value in operator.values())
    assert before == {str(p): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                      for p in (tmp_path / "operator").rglob("*")}


@pytest.mark.parametrize("key", [None, "", "   "])
def test_dsh_missing_binding_key_cannot_fall_back_to_project_dotenv(tmp_path, monkeypatch, key):
    from looper.agents.runtimes import dsh

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-key")
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=synthetic-project-key\n")
    assert "synthetic-project-key" in (tmp_path / ".env").read_text()
    home = dsh.write_home(tmp_path / ".local", {"looper.yml": "{}\n"})
    profile = resolved_profile("test", "test", RuntimeSpec("test", "test", "omp"),
                               replace(DSH_PROVIDER, api_key=None), "deepseek-flash")
    # The registry refuses a blank key; a row stored before that rule could still hold one.
    object.__setattr__(profile.resolved_model.provider, "api_key", key)
    launch = Launch(AgentSettings(profile=profile))
    with pytest.raises(RuntimeUnsupported, match="API key") as failure:
        dsh.environment(launch, home)
    assert "synthetic-project-key" not in repr(failure.value)
    assert "ambient-key" not in repr(failure.value)


def test_pi_main_and_nested_shell_keep_personal_config_and_server_secrets_out(
    server_environment: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = Path(server_environment["HOME"])
    sentinels = {}
    for name in (".pi/agent/settings.json", ".pi/agent/auth.json", ".claude/settings.json",
                 ".codex/auth.json", ".grok/settings.json"):
        path = operator / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"personal_sentinel": name}))
        sentinels[path] = path.read_bytes()
    for name in (*child_env.RUNTIMES, "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR",
                 "PI_CONFIG_FILES", "PI_SHELL_PREFIX", "XDG_CONFIG_HOME", "XDG_DATA_DIRS"):
        monkeypatch.setenv(name, str(operator))
    launch = _with_stored_env(pi_launch(pi_provider(env=(("PROVIDER_SETTING", "explicit"),))),
                              ("PROVIDER_SECRET", "private-key"))
    launch = replace(launch, settings=replace(launch.settings, env=(("RUN_SETTING", "configured"),)))
    command = pi_command(tmp_path, launch)
    executable = tmp_path / "stand-in-pi"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\nfrom pathlib import Path\n"
        "home = Path(os.environ['HOME'])\nagent = Path(os.environ['PI_CODING_AGENT_DIR'])\n"
        "settings = json.loads((agent / 'settings.json').read_text())\n"
        f"nested = settings['shellCommandPrefix'] + {shlex.quote(sys.executable)!r} + "
        f"' -c ' + {shlex.quote('import json, os; print(json.dumps(dict(os.environ)))')!r}\n"
        "shell = subprocess.run(['/bin/sh', '-c', nested], capture_output=True, text=True, check=True, timeout=10)\n"
        "print(json.dumps({'env': dict(os.environ), 'shell': json.loads(shell.stdout),\n"
        " 'personal': any((home / name).exists() for name in "
        "('.pi/agent/auth.json', '.claude/settings.json', '.codex/auth.json', '.grok/settings.json'))}))\n")
    executable.chmod(0o700)
    command[command.index("pi")] = str(executable)
    main_env = launch.environment()
    done = subprocess.run(command, cwd=tmp_path, env=main_env, text=True, capture_output=True, check=True, timeout=15)
    actual = json.loads(done.stdout)
    assert actual["personal"] is False
    assert actual["env"]["HOME"] != str(operator)
    assert actual["shell"]["HOME"] == actual["env"]["HOME"]
    assert actual["env"]["LOOPER_PROVIDER_API_KEY"] == pi_provider().api_key
    assert actual["env"]["PROVIDER_SECRET"] == "private-key"
    assert actual["env"]["PROVIDER_SETTING"] == "explicit"
    for env in (actual["env"], actual["shell"]):
        assert env["RUN_SETTING"] == "configured"
        assert env["PATH"] == server_environment["PATH"]
        assert not set(DROPPED) & env.keys()
        assert not set(child_env.RUNTIMES) & env.keys()
        assert not any(name.startswith("XDG_") for name in env)
    assert {name: actual["env"][name] for name in ("PI_OFFLINE", "PI_TELEMETRY", "PI_SKIP_VERSION_CHECK")} == {
        "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1"}
    assert not any(name.startswith(("PI_", "LOOPER_", "LOOPX_", "PROVIDER_")) for name in actual["shell"])
    assert os.environ["HOME"] == str(operator)
    assert all(path.read_bytes() == content for path, content in sentinels.items())


@pytest.mark.installed
def test_pi_bash_tool_scrubs_credentials_and_session_metadata(
    server_environment: dict[str, str], tmp_path: Path,
) -> None:
    from test_pi_runtime import _command_env

    executable, node = shutil.which("pi"), shutil.which("node")
    if not executable or not node:
        pytest.skip("installed pi and Node are required for the real bash tool check")
    package = next((p for p in Path(executable).resolve().parents if (p / "package.json").exists()), None)
    if package is None or json.loads((package / "package.json").read_text()).get("version") != "1.3.0":
        pytest.skip("the bash tool check targets installed pi 1.3.0")
    launch = _with_stored_env(pi_launch(pi_provider()), ("PROVIDER_SECRET", "provider-secret"))
    env = {**launch.environment(), **_command_env(pi_command(tmp_path, launch)),
           "LOOPER_AGENT_URL": "http://127.0.0.1:1", "LOOPER_AGENT_TOKEN": "bridge-secret"}
    script = tmp_path / "bash-tool.mjs"
    script.write_text('''
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [pkg, cwd, command] = process.argv.slice(2);
const { createBashToolDefinition } = await import(pathToFileURL(join(pkg, 'dist/core/tools/bash.js')));
const settings = JSON.parse(readFileSync(join(process.env.PI_CODING_AGENT_DIR, 'settings.json'), 'utf8'));
const tool = createBashToolDefinition(cwd, {commandPrefix: settings.shellCommandPrefix});
const result = await tool.execute('check-env', {command, timeout: 10}, undefined, undefined, {
  cwd, model: {provider: 'looper', id: 'selected'}, thinkingLevel: 'high',
  sessionManager: {getSessionId: () => 'private-session', getSessionFile: () => '/private/session.jsonl'},
});
if (result.isError) throw new Error(JSON.stringify(result));
console.log(result.structuredContent.output);
''')
    command = shlex.join([sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"])
    done = subprocess.run([node, str(script), str(package), str(tmp_path), command], env=env, cwd=tmp_path,
                          capture_output=True, text=True, check=True, timeout=20)
    nested = json.loads(done.stdout)
    assert nested["HOME"] == env["HOME"] != server_environment["HOME"]
    assert not any(name.startswith(("PI_", "LOOPER_", "LOOPX_", "PROVIDER_")) for name in nested)
    assert not set(DROPPED) & nested.keys()
