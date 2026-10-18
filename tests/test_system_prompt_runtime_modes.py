"""System prompt modes keep append launches stable and replace the runtime defaults explicitly."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time
import tomllib

import pytest

from looper.agents import claude_cli
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, SystemPromptMode, adapter, replace_refusal
from looper.agents.runtimes import codex, grok, omp
from looper.agents.system_prompt import SystemPrompt, compose_system_prompt


@pytest.fixture
def isolated_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """All home discovery, including the login symlink target, stays in the disposable directory."""
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.setenv("GROK_HOME", str(home / "grok"))
    return home


@pytest.mark.parametrize("module", [
    "looper.agents.claude_cli", "looper.agents.serve", "looper.agents.runtimes",
    "looper.agents.profile_registry",
])
def test_runtime_modules_import_in_fresh_processes(module: str, isolated_operator: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-B", "-c", f"import {module}"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr


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


def _sent_looper_text(runtime_type: str, mode: SystemPromptMode, command: list[str]) -> str:
    """Read the actual input named by the command, excluding Grok's agent metadata."""
    if runtime_type == "claude-code":
        flag = "--system-prompt-file" if mode is SystemPromptMode.REPLACE else "--append-system-prompt-file"
        return Path(command[command.index(flag) + 1]).read_text()
    if runtime_type == "codex":
        home = Path(command[1].partition("=")[2])
        config = tomllib.loads((home / "config.toml").read_text())
        return (Path(config["model_instructions_file"]) if mode is SystemPromptMode.REPLACE
                else home / "AGENTS.md").read_text()
    if runtime_type == "grok":
        home = Path(command[1].partition("=")[2])
        if mode is SystemPromptMode.REPLACE:
            return (home / "agent.md").read_text().split("---\n\n", 1)[1]
        return (home / grok.RULES_FILE).read_text()
    flag = "--system-prompt" if mode is SystemPromptMode.REPLACE else "--append-system-prompt"
    return Path(command[command.index(flag) + 1]).read_text()


@pytest.mark.parametrize("runtime_type", ["claude-code", "codex", "grok", "omp"])
@pytest.mark.parametrize("mode", list(SystemPromptMode))
@pytest.mark.parametrize("command_kind", ["run_command", "resident_command"])
@pytest.mark.parametrize("custom_prompt", ["", "  Profile's identity: 自定义\n  "])
@pytest.mark.parametrize("project_layout", ["repository", "standalone"])
def test_prompt_blocks_match_real_launch_files(
    runtime_type: str, mode: SystemPromptMode, command_kind: str, custom_prompt: str,
    project_layout: str, tmp_path: Path, isolated_operator: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if project_layout == "repository":
        (tmp_path / ".git").mkdir()
    else:
        # Temporary files can live inside the checkout. Simulate a standalone directory's
        # discovery boundary without letting discovery read that checkout's instructions.
        monkeypatch.setattr(omp, "outside_repository", lambda cwd: True)
        monkeypatch.setattr(omp.instructions, "repository_root", lambda cwd: Path(cwd).absolute())
    (tmp_path / "AGENTS.md").write_text("Project instruction: report verification evidence.")
    runtime, launch = adapter(runtime_type), _launch(runtime_type, mode)
    launch = replace(launch, settings=replace(launch.settings,
                     profile=replace(launch.profile, custom_prompt=custom_prompt.strip())))
    baseline = compose_system_prompt("Role", harness="Harness", contract="Contract")
    text = compose_system_prompt("Role", harness="Harness", contract="Contract", custom_prompt=custom_prompt)
    prompt = SystemPrompt(text).write_file(tmp_path / "runtime")
    command = getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path, prompt_file=prompt,
                                             launch=launch)
    sent = _sent_looper_text(runtime_type, mode, command)
    blocks = runtime.prompt_blocks(launch, text, cwd=tmp_path)
    assert "".join(block.text or "" for block in blocks if block.owner == "looper") == sent
    assert all(block.text is None and block.note for block in blocks if block.owner == "runtime")
    assert all(block.text is not None for block in blocks if block.owner == "looper")
    if runtime_type == "omp":
        assert ("Project instruction: report verification evidence." in sent) == (project_layout == "standalone")
        if project_layout == "standalone":
            assert "# Project instructions: AGENTS.md" in sent
    if custom_prompt:
        assert text.startswith(custom_prompt.strip() + "\n\nHarness")
        assert sent.startswith(custom_prompt.strip() + "\n\n")
        assert sent.endswith("Harness\n\nRole\n\nContract")
    else:
        assert text == baseline
        baseline_file = SystemPrompt(baseline).write_file(tmp_path / "runtime")
        baseline_command = getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path,
                                                         prompt_file=baseline_file, launch=launch)
        assert command == baseline_command
        assert sent == _sent_looper_text(runtime_type, mode, baseline_command)


@pytest.mark.parametrize("runtime_type,mode,owners,notes", [
    ("claude-code", SystemPromptMode.APPEND, ["runtime", "runtime", "runtime", "looper"],
     ["billing header (fixed)", "identity line (fixed; changes with append vs replace)",
      "Claude Code's default prompt (not shown)"]),
    ("claude-code", SystemPromptMode.REPLACE, ["runtime", "runtime", "looper"],
     ["billing header (fixed)", "identity line (fixed; changes with append vs replace)"]),
    ("codex", SystemPromptMode.APPEND, ["runtime", "runtime", "looper", "runtime"],
     ["base instructions (not shown)", "skills/permissions notes (not shown)", "environment (not shown)"]),
    ("codex", SystemPromptMode.REPLACE, ["looper", "runtime", "runtime"],
     ["skills/permissions notes (not shown)", "environment (not shown)"]),
    ("grok", SystemPromptMode.APPEND, ["runtime", "looper"], ["Grok's default prompt (not shown)"]),
    ("grok", SystemPromptMode.REPLACE, ["looper"], []),
    ("omp", SystemPromptMode.APPEND, ["runtime", "looper"], ["OMP's default prompt (not shown)"]),
    ("omp", SystemPromptMode.REPLACE, ["looper"], []),
])
def test_prompt_blocks_preserve_runtime_order(
    runtime_type: str, mode: SystemPromptMode, owners: list[str], notes: list[str],
) -> None:
    blocks = adapter(runtime_type).prompt_blocks(_launch(runtime_type, mode), "Looper prompt")
    assert [block.owner for block in blocks] == owners
    assert [block.note for block in blocks if block.owner == "runtime"] == notes


@pytest.mark.parametrize("runtime_type", ["codex", "grok", "omp"])
def test_empty_prompt_preserves_adapter_note_separator(runtime_type: str) -> None:
    launch = _launch(runtime_type)
    text = "".join(block.text or "" for block in launch.runtime.prompt_blocks(launch, ""))
    if runtime_type == "codex":
        expected = codex.runtime_notes(codex.materialize(launch.library)) + "\n\n---\n\n"
    elif runtime_type == "grok":
        expected = grok.runtime_notes([]) + "\n\n---\n\n"
    else:
        expected = omp.runtime_notes()
    assert text == expected


@pytest.mark.parametrize("command_kind", ["run_command", "resident_command"])
def test_claude_command_construction_does_not_read_prompt_file(
    command_kind: str, tmp_path: Path, isolated_operator: Path,
) -> None:
    launch = _launch("claude-code")
    path = tmp_path / "not-written.md"
    command = getattr(launch.runtime, command_kind)("claude", cwd=tmp_path, prompt_file=path, launch=launch)
    assert command[command.index("--append-system-prompt-file") + 1] == str(path)


@pytest.mark.parametrize("runtime_type", ["claude-code", "codex", "grok", "omp"])
@pytest.mark.parametrize("command_kind", ["run_command", "resident_command"])
def test_explicit_append_is_byte_identical_to_default_and_legacy_home(
    runtime_type: str, command_kind: str, tmp_path: Path, isolated_operator: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Independently transcribed from origin/main 10991b00. Never regenerate expectations with adapter helpers.
    baseline = json.loads((Path(__file__).parent / "fixtures" / "system_prompt_append.json").read_text())
    expected = baseline["runtimes"][runtime_type]
    local = tmp_path / "runtime" / ".local"
    substitutions = {
        "<TMP>": str(tmp_path), "<LOCAL>": str(local),
        "<SHELL_OPERATOR>": shlex.quote(str(isolated_operator)),
    }

    def render(text: str) -> str:
        for token, value in substitutions.items():
            text = text.replace(token, value)
        return text

    if runtime_type == "grok":
        # The old home identity includes absolute paths, so hash the literal baseline bytes after substitution.
        config = render(expected["files"]["grok/runs/<HOME_HASH>/config.toml"]["text"])
        rules = expected["files"]["grok/runs/<HOME_HASH>/rules/looper.md"]["text"]
        login = str(isolated_operator / "grok" / "auth.json")
        substitutions["<HOME_HASH>"] = hashlib.sha256(
            f"grok-home/v1\n{config}\0{rules}\0{login}".encode("utf-8")
        ).hexdigest()
    substitutions["<HOME>"] = render(expected.get("home", ""))
    expected_argv = [render(arg) for arg in expected["argv"][command_kind]]
    expected_env = {name: render(value) for name, value in expected["environment"].items()}
    expected_files = {
        render(name): (render(content["symlink"]) if "symlink" in content else render(content["text"]).encode("utf-8"))
        for name, content in expected["files"].items()
    }
    monkeypatch.setattr(os, "environ", {name: render(value) for name, value in baseline["server_environment"].items()})
    # Pin discovery inputs so neither parent repositories nor pytest's temporary-directory ancestors affect argv.
    (tmp_path / ".git").mkdir()
    if runtime_type == "claude-code":
        monkeypatch.setattr(claude_cli, "repository_root", lambda directory: Path("/"))
    prompt = SystemPrompt(baseline["prompt"]).write_file(tmp_path / "runtime")
    runtime = adapter(runtime_type)
    for mode in (None, SystemPromptMode.APPEND):
        launch = _launch(runtime_type, mode)
        command = getattr(runtime, command_kind)(runtime.default_executable, cwd=tmp_path, prompt_file=prompt,
                                                 launch=launch)
        assert command == expected_argv
        assert runtime.environment(launch) == expected_env
        assert _files(local) == expected_files


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


def test_codex_damaged_instructions_are_repaired_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local, text = tmp_path / "local", "Complete contract: 契约\n"
    target = codex.write_instructions(local, text)
    target.write_bytes(b"damaged\xff")
    target.chmod(0o644)
    replacements = []
    original = os.replace

    def record_replace(source: str, destination: Path) -> None:
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes() == text.encode("utf-8")
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        replacements.append(destination)
        original(source, destination)

    monkeypatch.setattr(codex.os, "replace", record_replace)
    assert codex.write_instructions(local, text) == target
    assert replacements == [target]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == target.stem
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.iterdir()) == [target]


def test_codex_cleanup_removes_old_instructions_after_their_run_home(tmp_path: Path) -> None:
    local = tmp_path / "local"
    abandoned = codex.write_instructions(local, "Abandoned contract")
    recent = codex.write_instructions(local, "Recent contract")
    source = codex.write_home(local, f'model_instructions_file = "{abandoned}"\n', "")
    then = time.time() - codex.RUN_HOME_IDLE_SEC - 60
    for path in (abandoned, source, *source.iterdir()):
        os.utime(path, (then, then))
    codex.write_home(local, "# New run\n", "")
    assert not source.exists()
    assert not abandoned.exists()
    assert recent.is_file()


@pytest.mark.parametrize("reference", ["run", "session", "new-run"])
def test_codex_cleanup_keeps_old_instructions_referenced_by_homes(tmp_path: Path, reference: str) -> None:
    local = tmp_path / "local"
    target = codex.write_instructions(local, "Still referenced")
    config = f'model_instructions_file = "{target}"\n'
    then = time.time() - codex.RUN_HOME_IDLE_SEC - 60
    if reference != "new-run":
        source = codex.write_home(local, config, "")
        if reference == "session":
            session = local / "codex" / "sessions" / "resident" / "home"
            codex._sync_home(source, session)
            # Even an idle session keeps its instructions after its original run is reclaimed.
            for home in (source, session):
                for path in (home, *home.iterdir()):
                    os.utime(path, (then, then))
    os.utime(target, (then, then))
    codex.write_home(local, config if reference == "new-run" else "# New run\n", "")
    assert target.read_text() == "Still referenced"
    if reference == "session":
        assert not source.exists()
        assert (session / "config.toml").read_text() == config


def test_codex_cleanup_preserves_instructions_when_session_config_is_damaged(tmp_path: Path) -> None:
    local = tmp_path / "local"
    target = codex.write_instructions(local, "Potential session reference")
    session = local / "codex" / "sessions" / "resident"
    session.mkdir(parents=True)
    (session / "config.toml").write_text('model_instructions_file = "unfinished')
    then = time.time() - codex.RUN_HOME_IDLE_SEC - 60
    os.utime(target, (then, then))
    codex.write_home(local, "# New run\n", "")
    assert target.read_text() == "Potential session reference"


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
