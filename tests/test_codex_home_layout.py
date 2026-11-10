"""Home layout substitution and the original Codex command/configuration contract."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import (
    Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.runtimes import ApiProtocol, SystemPromptMode, VendorType
from looper.agents.runtimes.codex import CodexRuntime


def _launch(*, login: bool = True) -> Launch:
    vendor = VendorType.CODEX_LOGIN if login else VendorType.CUSTOM
    provider = ProviderSpec("provider", "Example", vendor, vendor.auth_kind, vendor.oauth_runtime_type,
                            api_key=None if login else "test-key")
    model = ModelSpec("model", provider.id, "gpt-5.5", ApiProtocol.OPENAI_RESPONSES)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol,
                            "" if login else "https://api.example.test/v1"))
    profile = ResolvedAgentProfile("profile", "Example", RuntimeSpec(
        "runtime", "Codex", "codex", "codex", CodexRuntime.capabilities), model.id, "low", resolved_model=resolved)
    return Launch(AgentSettings(profile=profile), AgentLibrary(), AgentKnowledge())


def _prompt(root: Path) -> Path:
    path = root / "runtime" / ".local" / "prompts" / "prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("The prompt.")
    return path


@pytest.fixture
def fake_runtime(tmp_path: Path) -> CodexRuntime:
    from looper.agents.runtimes.codex import CodexHome

    login = ModuleType("fake_login")
    login.LOGIN_FILE = "st/auth.json"
    login.SHELL_HOME = "shell-home"
    login.CONFIG_MARKER = 'cli_auth_credentials_store = "file"'
    login.ENV_UNSET = ("FAKE_TOKEN",)
    target = tmp_path / "operator" / "auth.json"
    target.parent.mkdir()
    target.write_text("synthetic login")
    login.login_file = lambda: target

    def link_login(home: Path, target: Path) -> None:
        link = home / login.LOGIN_FILE
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(target)

    login.link_login = link_login
    login.unlink_login = lambda home: (home / login.LOGIN_FILE).unlink(missing_ok=True)

    def inspect_homes(root: Path, target: Path) -> list[str]:
        assert root == tmp_path / "runtime" / ".local" / "fake"
        assert target == login.login_file()
        return ["forked login"]

    async def record_problems(problems: list[str]) -> str:
        assert problems == ["forked login"]
        return "login unavailable"

    async def launch_problem() -> str:
        return "not logged in"

    login.inspect_homes = inspect_homes
    login.record_problems = record_problems
    login.launch_problem = launch_problem

    class FakeCodex(CodexRuntime):
        label = "Fake Codex"
        home = CodexHome("FAKE_HOME", "fake.toml", "st", "fake", "fake-home/v1", login, "fake",
                         top_config=("x = 1",), features=("y = false",))

    return FakeCodex()


def test_a_subclass_writes_its_own_home_and_login_config(fake_runtime: CodexRuntime, tmp_path: Path) -> None:
    command = fake_runtime.run_command("fake", cwd=tmp_path, prompt_file=_prompt(tmp_path), launch=_launch())
    home = Path(command[1].removeprefix("FAKE_HOME="))
    assert command[:3] == ["env", f"FAKE_HOME={home}", "fake"]
    assert home.parent == tmp_path / "runtime" / ".local" / "fake" / "runs"
    config = (home / "fake.toml").read_text()
    agents = (home / "AGENTS.md").read_text()
    assert home.name == hashlib.sha256(
        f"fake-home/v1\n{config}\0{agents}\0{fake_runtime.home.login.login_file()}".encode()).hexdigest()
    assert f"{fake_runtime.home.login.CONFIG_MARKER}\nx = 1\n" in config
    parsed = tomllib.loads(config)
    assert parsed["x"] == 1 and parsed["features"] == {"plugins": False, "apps": False, "y": False}
    shell = tmp_path / "runtime" / ".local" / "fake" / "shell-home"
    assert parsed["shell_environment_policy"]["set"] == {"FAKE_HOME": str(shell)}
    assert shell.is_dir() and list(shell.iterdir()) == []
    assert agents.endswith("The prompt.")
    assert (home / "st" / "auth.json").is_symlink()
    assert os.readlink(home / "st" / "auth.json") == str(fake_runtime.home.login.login_file())
    assert not (home / "config.toml").exists()


def test_a_subclass_carries_its_layout_into_events_and_resident_turns(
    fake_runtime: CodexRuntime, tmp_path: Path,
) -> None:
    async def events() -> list[dict]:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"thread.started","thread_id":"thread"}\n')
        reader.feed_eof()
        return [event async for event in fake_runtime.parse(reader)]

    assert asyncio.run(events()) == [{"type": "system", "subtype": "init", "session_id": "thread",
                                     "runtime": "fake"}]
    session = fake_runtime.resident_session("manager")
    command = fake_runtime.resident_command("fake", cwd=tmp_path, prompt_file=_prompt(tmp_path), launch=_launch())
    session._base, session._session, _ = session._split(command)
    argv = session._turn_argv()
    home = tmp_path / "runtime" / ".local" / "fake" / "sessions" / "manager"
    assert argv == ["env", f"FAKE_HOME={home}", *command[2:], "-"]
    assert (home / "fake.toml").is_file() and (home / "st" / "auth.json").is_symlink()
    assert session.runtime_label == fake_runtime.label
    assert session._stream().translate({"type": "thread.started", "thread_id": "thread"})[0]["runtime"] == "fake"
    with pytest.raises(ValueError, match="FAKE_HOME"):
        session._split(["env", "CODEX_HOME=wrong", "fake", "exec"])


def test_a_subclass_uses_its_login_checks_and_environment(fake_runtime: CodexRuntime, tmp_path: Path) -> None:
    launch = _launch()
    assert fake_runtime.bind_provider(launch.profile.provider, launch.model, launch.effort).unset == ("FAKE_TOKEN",)
    command = fake_runtime.run_command("fake", cwd=tmp_path, prompt_file=_prompt(tmp_path), launch=launch)
    assert asyncio.run(fake_runtime.login_problem(launch.profile.provider)) == "not logged in"
    assert asyncio.run(fake_runtime.after_run(launch, command)) == ["login unavailable"]


def test_a_subclass_probes_in_empty_homes_named_by_its_variable(fake_runtime: CodexRuntime, tmp_path: Path) -> None:
    from looper.agents.runtimes import Capability

    executable = fake_claude(tmp_path / "fake", """\
import os, pathlib, sys
assert os.listdir(os.environ['FAKE_HOME']) == []
if sys.argv[1:] == ['--version']:
    print('fake-cli 0.162.0')
else:
    assert sys.argv[1:4] == ['app-server', 'generate-json-schema', '--out']
    output = pathlib.Path(sys.argv[4])
    output.mkdir()
    (output / 'ClientRequest.json').write_text('{"turn/steer": {}}')
""")
    probe = fake_runtime.probe(executable)
    assert probe.version == "fake-cli 0.162.0" and probe.error is None
    assert Capability.STEER in probe.capabilities


def test_a_subclass_consults_the_state_subdirectory_and_syncs_its_home(
    fake_runtime: CodexRuntime, tmp_path: Path,
) -> None:
    prompt = _prompt(tmp_path)
    kept = tmp_path / "kept"
    fork = tmp_path / "fork"
    launch = _launch()
    fake_runtime.run_command("fake", cwd=tmp_path, prompt_file=prompt, launch=launch, session_dir=kept)
    sessions = kept / "st" / "sessions"
    sessions.mkdir()
    (sessions / "rollout.jsonl").write_text("{}\n")
    (kept / "st" / "state.sqlite").write_text("state")
    command = fake_runtime.consult_command("fake", cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                          session_id="thread", session_dir=kept, fork_dir=fork)
    assert command[:3] == ["env", f"FAKE_HOME={fork}", "fake"]
    assert command[-3:] == ["resume", "thread", "-"]
    assert (fork / "st" / "sessions" / "rollout.jsonl").read_text() == "{}\n"
    assert (fork / "st" / "auth.json").is_symlink() and not (fork / "st" / "state.sqlite").exists()
    assert (fork / "fake.toml").read_bytes() == (kept / "fake.toml").read_bytes()
    steer = fake_runtime.steer_command("fake", cwd=tmp_path, prompt_file=prompt, launch=launch, session_dir=kept)
    assert steer[:4] == ["env", f"FAKE_HOME={kept}", "fake", "app-server"]
    fake_runtime.run_command("fake", cwd=tmp_path, prompt_file=prompt, launch=_launch(login=False), session_dir=kept)
    assert not (kept / "st" / "auth.json").is_symlink()


def test_instruction_cleanup_reads_the_subclass_config_name(fake_runtime: CodexRuntime, tmp_path: Path) -> None:
    from looper.agents.runtimes.codex import RUN_HOME_IDLE_SEC, _remove_idle_instructions

    launch = _launch()
    launch = replace(launch, role=AgentType.WORKER,
                     settings=replace(launch.settings, system_prompt_mode=SystemPromptMode.REPLACE))
    command = fake_runtime.run_command("fake", cwd=tmp_path, prompt_file=_prompt(tmp_path), launch=launch)
    home = Path(command[1].partition("=")[2])
    config = tomllib.loads((home / "fake.toml").read_text())
    instruction = Path(config["model_instructions_file"])
    assert instruction.parent == home.parent.parent / "instructions"
    assert instruction.read_text().endswith("The prompt.") and (home / "AGENTS.md").read_text() == ""
    old = time.time() - RUN_HOME_IDLE_SEC - 60
    os.utime(instruction, (old, old))
    _remove_idle_instructions(home.parent.parent, time.time(), home=fake_runtime.home)
    assert instruction.is_file()
    (home / "fake.toml").unlink()
    _remove_idle_instructions(home.parent.parent, time.time(), home=fake_runtime.home)
    assert not instruction.exists()


# Captured from the original adapter before extracting the home layout.
_KEY_CONFIG = """# Written by Looper for its Codex runs. Only this file and AGENTS.md come from Looper.
model_provider = "looper"
# A directory of the project without AGENTS.md: its CLAUDE.md instead.
project_doc_fallback_filenames = ["CLAUDE.md"]

# Codex's own skill list always includes $HOME/.agents/skills; Looper lists the run's skills itself.
[skills]
include_instructions = false

# No plugin marketplace sync into this home.
[features]
plugins = false

# The agent's shell commands do not get the provider's key.
[shell_environment_policy]
exclude = ["LOOPER_PROVIDER_API_KEY"]

[model_providers.looper]
name = "Example"
base_url = "https://api.example.test/v1"
env_key = "LOOPER_PROVIDER_API_KEY"
wire_api = "responses"
"""
_LOGIN_CONFIG = """# Written by Looper for its Codex runs. Only this file, AGENTS.md and the auth.json link come from Looper.
# Codex's own login: auth.json here links to the operator's login file, never a copy.
cli_auth_credentials_store = "file"
# A directory of the project without AGENTS.md: its CLAUDE.md instead.
project_doc_fallback_filenames = ["CLAUDE.md"]

# Codex's own skill list always includes $HOME/.agents/skills; Looper lists the run's skills itself.
[skills]
include_instructions = false

# No plugin marketplace sync and no ChatGPT apps in this home.
[features]
plugins = false
apps = false

# The agent's shell commands get a CODEX_HOME without the login, so a codex command there cannot use it
# or log it out.
[shell_environment_policy]
set = { CODEX_HOME = "<tmp>/runtime/.local/codex/shell-home" }
"""
_AGENTS = """# Runtime notes (Codex)

Looper runs you through Codex. The instructions after these notes are Looper's, not a repository's AGENTS.md.

- Looper reads only your last message of each turn. When the instructions ask for a reply or a report in a given shape (such as a JSON block), put all of it in that last message.

---

The prompt."""
_EXEC = ["codex", "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
         "-m", "gpt-5.5", "-c", 'model_reasoning_effort="low"']


@pytest.mark.parametrize("login", [False, True])
def test_codex_commands_and_config_match_the_original_adapter(
    login: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = tmp_path / "operator"
    operator.mkdir()
    target = operator / "auth.json"
    target.write_text("synthetic login")
    monkeypatch.setenv("CODEX_HOME", str(operator))
    runtime = CodexRuntime()
    prompt = _prompt(tmp_path)
    launch = _launch(login=login)
    config = (_LOGIN_CONFIG if login else _KEY_CONFIG).replace("<tmp>", str(tmp_path))
    named = f"codex-home/v1\n{config}\0{_AGENTS}" + (f"\0{target}" if login else "")
    digest = hashlib.sha256(named.encode()).hexdigest()
    home = tmp_path / "runtime" / ".local" / "codex" / "runs" / digest
    command = runtime.run_command("codex", cwd=tmp_path, prompt_file=prompt, launch=launch)
    assert command == ["env", f"CODEX_HOME={home}", *_EXEC, "--ephemeral", "-"]
    assert (home / "config.toml").read_text() == config
    assert (home / "AGENTS.md").read_text() == _AGENTS
    assert runtime.steer_command("codex", cwd=tmp_path, prompt_file=prompt, launch=launch) == [
        "env", f"CODEX_HOME={home}", "codex", "app-server", "-c", 'model_reasoning_effort="low"']
    (home / "sessions").mkdir()
    fork = tmp_path / "fork"
    assert runtime.consult_command("codex", cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                   session_id="thread", session_dir=home, fork_dir=fork) == [
        "env", f"CODEX_HOME={fork}", *_EXEC[:4], "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
        *_EXEC[5:], "resume", "thread", "-"]
    session = runtime.resident_session("manager")
    session._base, session._session, _ = session._split(runtime.resident_command(
        "codex", cwd=tmp_path, prompt_file=prompt, launch=launch))
    resident_home = tmp_path / "runtime" / ".local" / "codex" / "sessions" / "manager"
    assert session._turn_argv() == ["env", f"CODEX_HOME={resident_home}", *_EXEC, "-"]
    assert (resident_home / "config.toml").read_text() == (fork / "config.toml").read_text() == config
