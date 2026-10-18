"""System prompt modes keep append launches stable and replace the runtime defaults explicitly."""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import tomllib

import pytest

from looper.agents import claude_cli
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, SystemPromptMode, adapter, replace_refusal
from looper.agents.runtimes import codex, grok, omp
from looper.agents.system_prompt import SystemPrompt


@pytest.fixture
def isolated_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """All home discovery, including the login symlink target, stays in the disposable directory."""
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.setenv("GROK_HOME", str(home / "grok"))
    return home


def _launch(runtime_type: str, mode: SystemPromptMode | None = None, *, oauth: bool = False) -> Launch:
    runtime = adapter(runtime_type)
    use_login = runtime_type == "grok" or oauth
    provider = ProviderSpec("provider", "test", AuthKind.OAUTH if use_login else AuthKind.API_KEY,
                            frozenset(ApiProtocol), runtime_type if use_login else None,
                            base_url="http://localhost.invalid/v1", api_key=None if use_login else "test-only")
    profile = AgentProfile("profile", "test", RuntimeSpec("runtime", "test", runtime_type,
                           capabilities=runtime.capabilities), provider, "test-model")
    if mode is not None:
        profile = replace(profile, system_prompt_mode=mode)
    return Launch(AgentSettings(profile=profile))


def _files(root: Path) -> dict[str, bytes | str]:
    return {str(p.relative_to(root)): os.readlink(p) if p.is_symlink() else p.read_bytes()
            for p in root.rglob("*") if p.is_symlink() or p.is_file()}


def _legacy_append_command(runtime_type: str, command_kind: str, prompt: Path, cwd: Path,
                           launch: Launch) -> list[str]:
    """The pre-mode command assembly: no mode is read and no new prompt parameter is passed."""
    runtime, local = adapter(runtime_type), prompt.parent.parent
    if runtime_type == "claude-code":
        flags = (["--input-format", "stream-json", "--include-partial-messages"]
                 if command_kind == "resident_command"
                 else ["--include-partial-messages", "--input-format", "stream-json"])
        return ["claude", "--print", "--verbose", "--output-format", "stream-json", *flags,
                "--replay-user-messages", *claude_cli.isolation_args(cwd),
                "--append-system-prompt-file", str(prompt), "--system-prompt-snapshot", "off",
                *runtime.launch_args(launch)]
    if runtime_type == "codex":
        home = codex.write_home(local, codex.config_toml(launch), codex.agents_md(prompt, launch))
        return ["env", f"CODEX_HOME={home}", "codex", "exec", *runtime.launch_args(launch),
                *(["--ephemeral", "-"] if command_kind == "run_command" else [])]
    if runtime_type == "grok":
        home = grok.write_home(local, grok.config_toml(launch, shell_home=grok._shell_home(local)),
                               grok.rules_md(prompt, launch), login=grok.grok_login.login_file())
        return ["env", f"GROK_HOME={home}", f"HOME={home}", "grok", *runtime.launch_args(launch)]
    visible = {**os.environ, **dict(launch.env)}
    mcp = omp.mcp_config(launch.library, visible)
    home = omp.write_home(local, omp.models_yml(launch),
                           omp.overlay(launch, outside_repository=omp.outside_repository(cwd)), mcp)
    project = omp.instructions.injected_section(runtime.instruction_files(cwd), runtime.label)
    written = omp.write_prompt(home, "\n\n---\n\n".join(
        part for part in (project, omp.runtime_notes(), prompt.read_text()) if part))
    return ["env", f"HOME={home}", "omp", *runtime.launch_args(launch),
            "--config", str(home / omp.OVERLAY), "--append-system-prompt", str(written),
            *(["--plugin-dir", str(home / "plugins" / omp.MCP_PLUGIN)] if mcp else []),
            *(["--no-session"] if command_kind == "run_command" else [])]


@pytest.mark.parametrize("runtime_type", ["claude-code", "codex", "grok", "omp"])
@pytest.mark.parametrize("command_kind", ["run_command", "resident_command"])
def test_explicit_append_is_byte_identical_to_default_and_legacy_home(
    runtime_type: str, command_kind: str, tmp_path: Path, isolated_operator: Path,
) -> None:
    prompt = SystemPrompt("Looper contract\nwith Unicode: 契约\n").write_file(tmp_path / "runtime")
    runtime = adapter(runtime_type)
    default = _launch(runtime_type)
    legacy = _legacy_append_command(runtime_type, command_kind, prompt, tmp_path, default)
    before = _files(prompt.parent.parent)
    command = getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path, prompt_file=prompt,
                                             launch=default)
    assert command == legacy
    assert _files(prompt.parent.parent) == before
    explicit = getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path, prompt_file=prompt,
                                              launch=_launch(runtime_type, SystemPromptMode.APPEND))
    assert explicit == command
    assert _files(prompt.parent.parent) == before
    # Pin the original append mechanisms, bytes and content-addressed home layouts too: comparing two new
    # launch paths alone would miss an accidental change shared by both default and explicit append.
    if runtime_type == "claude-code":
        expected = ["--append-system-prompt-file", str(prompt), "--system-prompt-snapshot", "off"]
        assert claude_cli.system_prompt_args(prompt) == expected
        assert command[command.index(expected[0]):command.index(expected[0]) + 4] == expected
    elif runtime_type == "codex":
        home = Path(command[1].partition("=")[2])
        config, agents = codex.config_toml(default), codex.agents_md(prompt, default)
        assert (home / "config.toml").read_bytes() == config.encode()
        assert (home / "AGENTS.md").read_bytes() == agents.encode()
        assert "model_instructions_file" not in config
        assert home.name == hashlib.sha256(f"{codex.HOME_LAYOUT_ID}\n{config}\0{agents}".encode()).hexdigest()
    elif runtime_type == "grok":
        home = Path(command[1].partition("=")[2])
        config = grok.config_toml(default, shell_home=grok._shell_home(prompt.parent.parent))
        rules, login = grok.rules_md(prompt, default), grok.grok_login.login_file()
        assert (home / "config.toml").read_bytes() == config.encode()
        assert (home / grok.RULES_FILE).read_bytes() == rules.encode()
        assert not (home / "agent.md").exists() and "--agent" not in command
        assert home.name == hashlib.sha256(f"{grok.HOME_LAYOUT_ID}\n{config}\0{rules}\0{login}".encode()).hexdigest()
    else:
        assert "--system-prompt" not in command
        written = Path(command[command.index("--append-system-prompt") + 1])
        assert written.read_text() == omp.runtime_notes() + "\n\n---\n\n" + prompt.read_text()


@pytest.mark.parametrize("runtime_type", ["claude-code", "codex", "grok", "omp"])
@pytest.mark.parametrize("mode", list(SystemPromptMode))
@pytest.mark.parametrize("command_kind", ["run_command", "resident_command"])
def test_runtime_system_prompt_mode_commands_and_files(
    runtime_type: str, mode: SystemPromptMode, command_kind: str, tmp_path: Path, isolated_operator: Path,
) -> None:
    launch, runtime = _launch(runtime_type, mode), adapter(runtime_type)
    prompt = SystemPrompt("Looper's complete contract.\n").write_file(tmp_path / "runtime")
    command = getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path, prompt_file=prompt,
                                             launch=launch)
    replacing = mode is SystemPromptMode.REPLACE
    assert runtime.can_replace_system_prompt and replace_refusal(runtime) is None
    assert launch.system_prompt_mode is mode
    assert launch.record()["system_prompt_mode"] == mode.value
    if runtime_type == "claude-code":
        flag = "--system-prompt-file" if replacing else "--append-system-prompt-file"
        assert command[command.index(flag) + 1] == str(prompt)
        assert command[command.index("--system-prompt-snapshot") + 1] == "off"
        assert ("--append-system-prompt-file" in command) is not replacing
    elif runtime_type == "codex":
        home = Path(command[1].partition("=")[2])
        config = tomllib.loads((home / "config.toml").read_text())
        assert ("model_instructions_file" in config) is replacing
        if replacing:
            instructions = Path(config["model_instructions_file"])
            content = codex.agents_md(prompt, launch)
            assert instructions.is_absolute()
            assert instructions == prompt.parent.parent / "codex" / "instructions" / (hashlib.sha256(content.encode()).hexdigest() + ".md")
            assert instructions.read_text() == content
            assert stat.S_IMODE(instructions.stat().st_mode) == 0o600
            assert (home / "AGENTS.md").read_bytes() == b""
            original = instructions.stat().st_mtime_ns
            getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path, prompt_file=prompt, launch=launch)
            assert instructions.stat().st_mtime_ns == original
        else:
            assert (home / "AGENTS.md").read_text() == codex.agents_md(prompt, launch)
    elif runtime_type == "grok":
        home = Path(command[1].partition("=")[2])
        assert "--system-prompt-override" not in command
        assert ("--agent" in command) is replacing
        if replacing:
            assert command[command.index("--agent") + 1] == str(home / "agent.md")
            assert (home / "agent.md").read_text() == "---\nname: looper\ndescription: Looper agent\nprompt_mode: full\n---\n\n" + grok.rules_md(prompt, launch)
            assert stat.S_IMODE((home / "agent.md").stat().st_mode) == 0o600
            assert (home / grok.RULES_FILE).read_bytes() == b""
        else:
            assert (home / grok.RULES_FILE).read_text() == grok.rules_md(prompt, launch)
    else:
        flag = "--system-prompt" if replacing else "--append-system-prompt"
        assert ("--append-system-prompt" in command) is not replacing
        written = Path(command[command.index(flag) + 1])
        assert written.read_text() == omp.runtime_notes() + "\n\n---\n\n" + prompt.read_text()


@pytest.mark.parametrize("oauth", [False, True], ids=["api-key", "login"])
def test_codex_replace_key_is_top_level_and_survives_resident_home_sync(
    oauth: bool, tmp_path: Path, isolated_operator: Path,
) -> None:
    launch = _launch("codex", SystemPromptMode.REPLACE, oauth=oauth)
    prompt = SystemPrompt("Replacement contract").write_file(tmp_path / "runtime")
    command = launch.runtime.resident_command("codex", cwd=tmp_path, prompt_file=prompt, launch=launch)
    source, home = Path(command[1].partition("=")[2]), tmp_path / "session"
    codex._sync_home(source, home)
    text = (home / "config.toml").read_text()
    config = tomllib.loads(text)
    assert text.index("model_instructions_file =") < text.index("[skills]")
    lines = text.splitlines()
    marker = codex.codex_login.CONFIG_MARKER if oauth else 'model_provider = "looper"'
    assert lines[lines.index(marker) + 1].startswith("model_instructions_file =")
    assert Path(config["model_instructions_file"]).read_text() == codex.agents_md(prompt, launch)
    assert (home / "AGENTS.md").read_bytes() == b""


def test_grok_replace_agent_is_hashed_restored_and_synced(tmp_path: Path, isolated_operator: Path) -> None:
    launch = _launch("grok", SystemPromptMode.REPLACE)
    prompt = SystemPrompt("Replacement contract").write_file(tmp_path / "runtime")
    command = launch.runtime.resident_command("grok", cwd=tmp_path, prompt_file=prompt, launch=launch)
    source, home = Path(command[1].partition("=")[2]), tmp_path / "session"
    expected = (source / "agent.md").read_bytes()
    (source / "agent.md").write_text("tampered")
    assert launch.runtime.resident_command("grok", cwd=tmp_path, prompt_file=prompt, launch=launch) == command
    assert (source / "agent.md").read_bytes() == expected
    grok._sync_home(source, home)
    assert (home / "agent.md").read_bytes() == expected
    changed = grok.write_home(prompt.parent.parent, (source / "config.toml").read_text(), "",
                              login=grok.grok_login.login_file(), agent="another contract")
    assert changed != source


def test_system_prompt_refusal_names_the_unsupported_runtime() -> None:
    class StandIn(codex.CodexRuntime):
        label = "Stand-in"
        can_replace_system_prompt = False

    assert replace_refusal(StandIn()) == "the Stand-in runtime cannot replace its default system prompt"
