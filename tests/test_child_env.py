"""Every child starts from named variables, then receives only its run's additions."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from looper import child_env
from looper.agents import proc_tree, schedule_gate
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.channels import lark_cli
from looper.agents.profile_registry import ProviderSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported
from looper.agents.runtimes.grok import FIXED_ENV
from test_agent_settings import _profile as claude_profile
from test_codex_runtime import _launch as codex_launch, _login_provider, _operator_login
from test_grok_runtime import LOGIN, _launch as grok_launch, _operator
from test_omp_runtime import _launch as omp_launch

KEPT = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "SSL_CERT_FILE",
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK",
)
DROPPED = (
    "LOOPER_DATABASE_URL", "LOOPX_DATABASE_URL", "PGPASSWORD", "PGHOST", "LOOPER_TELEGRAM_BOT_TOKEN",
    "LOOPER_AGENT_TOKEN", "LOOPER_RUNTIME_ROOT", "LOOPER_PUBLIC_URL", "CLAUDECODE", "AWS_SECRET_ACCESS_KEY",
    "SOME_SECRET",
)
CASES = (
    ("claude-code", "login"), ("claude-code", "api-key"), ("claude-code", "endpoint"),
    ("codex", "login"), ("codex", "api-key"), ("grok", "login"), ("omp", "api-key"),
)


@pytest.mark.parametrize("name", KEPT)
def test_inherited_keeps_named_variables(name: str) -> None:
    mapping = {key: f"value-{key}" for key in (*KEPT, *DROPPED)}
    assert child_env.inherited(mapping)[name] == mapping[name]


@pytest.mark.parametrize("name", DROPPED)
def test_inherited_drops_other_variables(name: str) -> None:
    assert name not in child_env.inherited({"PATH": "/bin", name: "secret"})


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


def _launch(runtime: str, auth: str) -> Launch:
    protocol = ApiProtocol.ANTHROPIC_MESSAGES if runtime == "claude-code" else ApiProtocol.OPENAI_RESPONSES
    provider = ProviderSpec("provider-test", "test", AuthKind.API_KEY, frozenset({protocol}),
                            base_url="https://provider.example.invalid/v1" if auth == "endpoint" else "",
                            api_key="run-api-key")
    if runtime == "claude-code":
        profile = claude_profile("opus")
        launch = Launch(AgentSettings(profile=profile if auth == "login" else replace(profile, provider=provider)))
    elif runtime == "codex":
        launch = codex_launch(provider=_login_provider() if auth == "login" else provider)
    elif runtime == "grok":
        launch = grok_launch()
    else:
        launch = omp_launch(provider=provider)
    # Explicit settings and provider variables must still be added, even though neither is in the allowlist.
    profile = replace(launch.profile, provider=replace(launch.profile.provider, env=(("PROVIDER_SETTING", "bound"),)))
    return replace(launch, settings=replace(launch.settings, profile=profile, env=(("RUN_SETTING", "configured"),)))


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
    provider = replace(LOGIN, oauth_runtime_type="omp") if runtime == "omp" else ProviderSpec(
        "api", "api", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}), api_key="run-api-key")
    launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, provider=provider)))
    with pytest.raises(RuntimeUnsupported):
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
