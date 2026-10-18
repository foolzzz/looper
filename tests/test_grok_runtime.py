"""The Grok runtime (``looper/agents/runtimes/grok.py``).

Its events are checked against output recorded from Grok 1.0.62 with its own
login (``tests/fixtures/grok_stream``, local paths replaced); its launch, its
home and what it makes of the installed items (RFC P9) against the files it
writes and the command it builds; its resident sessions against a stand-in
``grok`` that keeps a session in its home across processes the way Grok does.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.mcp import write_config
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.run_usage import Usage, request_context, run_usage
from looper.agents.runtimes import ROLE_CAPABILITIES, ApiProtocol, AuthKind, RuntimeUnsupported, adapter, grok_login
from looper.agents.runtimes import CostSource, ReadOnly
from looper.agents.runtimes.grok import (
    COMPAT_VARS, DIRECTORY_VARS, RESTORED_VARS, RULES_FILE, GrokRuntime, GrokStream, ResidentGrok, write_home,
)
from looper.agents.runtimes.instructions import Delivery
from looper.agents.system_prompt import SystemPrompt

GROK: GrokRuntime = adapter("grok")  # type: ignore[assignment]
LOGIN = ProviderSpec("prov-grok-login", "grok-login", AuthKind.OAUTH, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                     "grok")


def _launch(*, executable: str = "grok", library: AgentLibrary | None = None, provider: ProviderSpec = LOGIN,
            effort: str = "low", env: tuple[tuple[str, str], ...] = ()) -> Launch:
    profile = AgentProfile("agent-grok", "grok/grok-4.7",
                           RuntimeSpec("rt-grok", "grok", "grok", executable, GROK.capabilities),
                           provider, "grok-4.7", effort or None)
    return Launch(AgentSettings(profile=profile, env=env), library or AgentLibrary(), AgentKnowledge())


def _read_only_launch() -> Launch:
    return dataclasses.replace(_launch(), role=AgentType.PLANNER)


def _prompt_file(root: Path, text: str = "LOOPER PROMPT: the contract comes last.") -> Path:
    return SystemPrompt(text).write_file(root)


def _home(command: list[str]) -> Path:
    return Path(command[1].removeprefix("GROK_HOME="))


def _flag(command: list[str], name: str) -> list[str]:
    return [command[i + 1] for i, arg in enumerate(command) if arg == name]


def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The operator's own Grok home with its login file, which a run links to and never copies."""
    directory = tmp_path / "operator-grok"
    directory.mkdir()
    (directory / "config.toml").write_text('[models]\ndefault = "operator-model"\n')
    login = directory / "auth.json"
    login.write_text("stand-in login")
    monkeypatch.setenv("GROK_HOME", str(directory))
    return login


FIXTURES = Path(__file__).parent / "fixtures" / "grok_stream"


# --- Events ---


async def _parse_async(lines: bytes, model: str | None) -> list[dict[str, Any]]:
    reader = asyncio.StreamReader()
    reader.feed_data(lines)
    reader.feed_eof()
    return [event async for event in GROK.parse(reader, model=model)]


def _events(name: str, model: str | None = "grok-4.7") -> list[dict[str, Any]]:
    return asyncio.run(_parse_async((FIXTURES / f"{name}.jsonl").read_bytes(), model))


def _blocks(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [block for e in events if e["type"] in ("assistant", "user")
            for block in e["message"]["content"] if block["type"] == kind]


def _result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((e for e in events if e["type"] == "result"), None)


def _usages(events: list[dict[str, Any]]) -> list[Usage]:
    return [Usage.from_dict(e["usage"]) for e in events if e["type"] == "usage"]


def _tokens(uncached: int, output: int, cached: int) -> Usage:
    return Usage(input_tokens=uncached, output_tokens=output, cache_read_tokens=cached, cache_creation_tokens=0)


def test_a_turn_is_init_its_reply_and_a_result_with_groks_cost() -> None:
    events = _events("turn")
    init = events[0]
    assert (init["type"], init["subtype"], init["runtime"], init["model"], init["apiKeySource"]) == (
        "system", "init", "grok", "grok-4.7", "oauth")
    session = init["session_id"]
    assert all(e.get("session_id") == session for e in events)
    # The rules file and the project's AGENTS.md (a trusted folder) both reached the model.
    result = _result(events)
    assert result is not None and (result["subtype"], result["is_error"], result["result"]) == (
        "success", False, "FALCON MARTEN HERON")
    # The response's tokens are a usage event, and the assistant event gives the context size.
    assert _usages(events) == [_tokens(4495, 120, 10112)]
    assert [request_context(e) for e in events if e["type"] == "assistant"] == [4495 + 10112]
    # The run's cost is Grok's figure; its tokens are under the model Grok names in modelUsage.
    assert run_usage(result) == Usage(cost_usd=0.014766, **_tokens(4495, 120, 10112).to_dict())
    assert list(result["modelUsage"]) == ["grok-4.7-build"]


def test_a_resumed_turn_names_the_same_session_and_reports_only_its_own_tokens_and_cost() -> None:
    first, resumed = _events("turn"), _events("resumed")
    assert resumed[0]["session_id"] == first[0]["session_id"]
    result = _result(resumed)
    assert result is not None and result["result"] == "HERON"  # it remembered its last reply
    assert run_usage(result) == Usage(cost_usd=0.013864, **_tokens(3179, 35, 14592).to_dict())


def test_tool_calls_are_summarized_and_their_results_made_readable() -> None:
    events = _events("tools")
    calls, results = _blocks(events, "tool_use"), _blocks(events, "tool_result")
    assert [c["name"] for c in calls] == ["run_terminal_command", "read_file"]
    assert [GROK.tool_summary(c["name"], c["input"]) for c in calls] == [
        'Running: echo hello > a.txt; echo "HOME=$HOME GROK_HOME=$GROK_HOME"', "Reading: /work/repo-tools/a.txt"]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [(c["id"], False) for c in calls]
    # A command's output, which Grok gives as byte values, is text; the agent's shell had the operator's HOME.
    shell = json.loads(results[0]["content"])
    assert shell["output"] == "HOME=/Users/operator GROK_HOME=/work/shell-home\n" and shell["exit_code"] == 0
    assert _blocks(events, "thinking") and _usages(events) == [
        _tokens(15507, 75, 1152), _tokens(1176, 88, 16640), _tokens(124, 170, 17792)]
    assert _result(events)["result"] == "HOME=/Users/operator GROK_HOME=/work/shell-home"  # type: ignore[index]


def test_an_mcp_tool_is_found_with_search_tool_and_called_with_use_tool() -> None:
    events = _events("mcp")
    calls = _blocks(events, "tool_use")
    assert [(c["name"], c["input"].get("tool_name")) for c in calls] == [("search_tool", None),
                                                                          ("use_tool", "probe__ping")]
    assert GROK.tool_summary("use_tool", calls[1]["input"]) == "Tool: probe__ping"
    assert "PONG-OSPREY" in _blocks(events, "tool_result")[1]["content"]
    assert _result(events)["result"] == "PONG-OSPREY"  # type: ignore[index]


def test_an_effort_the_model_does_not_offer_fails_the_run_before_any_request() -> None:
    events = _events("bad_effort")
    result = _result(events)
    assert result is not None and result["is_error"] and result["subtype"] == "error_during_execution"
    assert "unknown effort level 'max'" in result["errors"][0] and _usages(events) == []
    assert result["total_cost_usd"] == 0  # nothing spent, so the 0 stands


def test_a_run_ended_by_a_signal_has_no_result() -> None:
    events = _events("killed")
    assert [e["type"] for e in events] == ["system"] and _result(events) is None


@pytest.mark.parametrize("models", [None, {}], ids=["missing", "empty"])
@pytest.mark.parametrize("seen", [Usage(), _tokens(1, 2, 0)], ids=["no-responses", "with-responses"])
def test_a_result_without_model_usage_keeps_its_own_tokens(models: dict[str, Any] | None, seen: Usage) -> None:
    stream = GrokStream(model="grok-4.7", usage=seen)
    raw = {"type": "result", "usage": {"input_tokens": 123, "output_tokens": 45}}
    if models is not None:
        raw["modelUsage"] = models
    (result,) = stream.translate(raw)
    usage = run_usage(result)
    assert (usage.input_tokens, usage.output_tokens) == (123, 45)


@pytest.mark.parametrize("token", ["inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"])
def test_zero_cost_with_tokens_only_in_model_usage_is_unknown(token: str) -> None:
    (result,) = GrokStream().translate({"type": "result", "total_cost_usd": 0,
                                       "usage": {"input_tokens": 0, "output_tokens": 0},
                                       "modelUsage": {"grok-4.7": {token: 123}}})
    assert "total_cost_usd" not in result and run_usage(result).cost_usd is None


# --- The launch and its home ---


def test_a_run_gets_a_home_of_its_own_with_the_generated_files_and_a_link_to_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = _operator(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(root), launch=_launch(), max_turns=7)

    home = _home(command)
    # Its home is both its GROK_HOME and its HOME (what Grok finds through HOME stays out).
    assert command[:4] == ["env", f"GROK_HOME={home}", f"HOME={home}", "grok"]
    assert home.parent == (root / ".local" / "grok" / "runs").absolute()
    assert stat.S_IMODE(home.parent.parent.stat().st_mode) == 0o700
    # Only what Looper wrote: the config, the rules file, the link to the operator's login (never a copy) and the
    # links to the bundled skills and user guide every home shares.
    entries = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file() or p.is_symlink())
    assert entries == ["auth.json", "bundled", "config.toml", "docs", RULES_FILE]
    assert os.readlink(home / "auth.json") == str(login) and login.read_text() == "stand-in login"
    shared = root.absolute() / ".local" / "grok" / "shared"
    assert [Path(os.readlink(home / name)) for name in ("bundled", "docs")] == [shared / "bundled", shared / "docs"]
    assert all(stat.S_IMODE((home / name).stat().st_mode) == 0o600 for name in ("config.toml", RULES_FILE))
    # Headless: the prompt from stdin, Claude Code's stream-json, every tool, the project trusted; model and effort.
    assert command[4:13] == ["--prompt-file", "/dev/stdin", "--output-format", "streaming-messages-json",
                             "--always-approve", "--trust", "--no-auto-update", "-m", "grok-4.7"]
    assert _flag(command, "--effort") == ["low"] and _flag(command, "--max-turns") == ["7"]
    notes, _, text = (home / RULES_FILE).read_text().partition("\n\n---\n\n")
    assert notes.startswith("# Runtime notes (Grok)") and "final message" in notes and "use_tool" not in notes
    assert text == "LOOPER PROMPT: the contract comes last."
    config = (home / "config.toml").read_text()
    assert grok_login.CONFIG_MARKER in config.splitlines() and "operator-model" not in config
    # The same settings and prompt share the home; another prompt gets another one.
    again = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(root), launch=_launch())
    other = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(root, "OTHER"), launch=_launch())
    assert again[1] == command[1] and other[1] != command[1] and "--max-turns" not in again


@pytest.mark.parametrize("tamper", ["overwrite", "remove"])
def test_the_next_launch_restores_a_cached_homes_generated_files(
    tamper: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    prompt = _prompt_file(tmp_path / "runtime")
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=prompt, launch=_launch())
    home = _home(command)
    expected = {name: (home / name).read_text() for name in ("config.toml", RULES_FILE)}
    for name in expected:
        if tamper == "overwrite":
            (home / name).write_text("tampered")
        else:
            (home / name).unlink()
    again = GROK.run_command("grok", cwd=tmp_path, prompt_file=prompt, launch=_launch())
    assert _home(again) == home
    assert {name: (home / name).read_text() for name in expected} == expected


@pytest.mark.parametrize(("table", "lines"), [
    ("cli", ["auto_update = false", "use_leader = false"]),
    ("memory", ["enabled = false"]),
    ("features", ["telemetry = false"]),
    ("telemetry", ["trace_upload = false"]),
    ("compat.claude", ["skills = false", "rules = false", "agents = false", "mcps = false", "hooks = false"]),
    ("compat.cursor", ["skills = false", "rules = false", "agents = false", "mcps = false", "hooks = false"]),
], ids=lambda value: value if isinstance(value, str) else "")
def test_the_config_keeps_the_operators_and_other_tools_directories_out(
    table: str, lines: list[str], tmp_path: Path,
) -> None:
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=_launch())
    config = (_home(command) / "config.toml").read_text().split("\n\n")
    (section,) = [block for block in config if block.splitlines()[-len(lines) - 1:][0] == f"[{table}]"]
    assert section.splitlines()[-len(lines):] == lines


@pytest.mark.parametrize("read_only", [False, True], ids=["writing", "read-only"])
def test_only_a_read_only_run_is_cut_down_to_the_reading_tools(read_only: bool, tmp_path: Path) -> None:
    launch = _read_only_launch() if read_only else _launch()
    assert launch.read_only is read_only and GROK.read_only is ReadOnly.TOOLS
    for command in (GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch),
                    GROK.resident_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)):
        # MCP tools are not in the list: Grok keeps them, through use_tool (probe, grok 1.0.62).
        assert _flag(command, "--tools") == (["read_file,grep,list_dir"] if read_only else [])
        assert "--always-approve" in command  # the tools are cut, not the approvals
    assert launch.record().get("read_only") == ("tools" if read_only else None)


def test_the_agents_shell_gets_the_operators_home_back_and_a_grok_home_without_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    for name in RESTORED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/Users/operator")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/Users/operator/.config")
    root = tmp_path / "runtime"
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(root), launch=_launch())
    shell_home = (root / ".local" / "grok" / "shell-home").absolute()
    assert (f'set = {{ GROK_HOME = "{shell_home}", HOME = "/Users/operator", '
            'XDG_CONFIG_HOME = "/Users/operator/.config" }') in (_home(command) / "config.toml").read_text()
    assert shell_home.is_dir() and list(shell_home.iterdir()) == []
    # Grok itself gets neither: its HOME is its home, and the XDG directory is not passed.
    assert command[2] == f"HOME={_home(command)}" and "XDG_CONFIG_HOME" not in GROK.environment(_launch())


@pytest.mark.parametrize(("variable", "value", "passed"), [
    ("GROK_HOME", "/operator/.grok", None),  # the run's home comes with the command
    ("GROK_CONFIG", '{"models": {}}', None),
    ("GROK_CONFIG_PATH", "/operator/overlay.toml", None),
    ("GROK_AGENT", "operator-agent", None),
    ("GROK_SANDBOX", "workspace", None),
    ("XAI_API_KEY", "xai-operator", None),  # Grok would bill the key once the login is gone
    ("GROK_CODE_XAI_API_KEY", "xai-operator", None),
    ("GROK_AUTH", "token", None),
    ("XDG_CONFIG_HOME", "/operator/.config", None),  # given back to the agent's shell only
    ("CLAUDE_CONFIG_DIR", "/operator/.claude", None),
    ("GROK_CLAUDE_SKILLS_ENABLED", "true", "false"),  # the variable wins over config.toml
    ("GROK_CURSOR_MCPS_ENABLED", "1", "false"),
    ("GROK_MEMORY", "1", "0"),
    ("GROK_DISABLE_AUTOUPDATER", "0", "1"),
    ("RUST_LOG", "warn", "warn"),
])
def test_the_environment_drops_what_points_grok_elsewhere(variable: str, value: str, passed: str | None,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(variable, value)
    assert GROK.environment(_launch()).get(variable) == passed


def test_every_compat_variable_is_off_and_owned_by_the_runtime() -> None:
    environment = GROK.environment(_launch(env=(("FOO", "bar"),)))
    assert len(COMPAT_VARS) == 10 and all(environment[name] == "false" for name in COMPAT_VARS)
    assert environment["FOO"] == "bar"
    assert {"GROK_HOME", "HOME", "XAI_API_KEY", *COMPAT_VARS} <= GROK.provider_env_owned


@pytest.mark.parametrize("provider", [
    ProviderSpec("prov-codex-login", "codex-login", AuthKind.OAUTH, frozenset({ApiProtocol.OPENAI_RESPONSES}), "codex"),
    ProviderSpec("prov-cpa", "cpa", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}), api_key="sk-test"),
], ids=["codex-login", "api-key"])
def test_any_provider_but_its_own_login_fails_the_launch(provider: ProviderSpec, tmp_path: Path) -> None:
    with pytest.raises(RuntimeUnsupported, match="its own login only"):
        GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=_launch(provider=provider))


def test_without_an_effort_grok_picks_its_own(tmp_path: Path) -> None:
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=_launch(effort=""))
    assert "--effort" not in command and _flag(command, "-m") == ["grok-4.7"]


@pytest.mark.parametrize("effort", ["max", "minimal", "unsupported"])
def test_an_unsupported_effort_is_rejected_when_binding_the_configuration(effort: str) -> None:
    with pytest.raises(RuntimeUnsupported, match=f"Grok.*{effort!r}.*low, medium, high, xhigh"):
        GROK.bind_provider(LOGIN, "grok-4.7", effort)


def _library(root: Path) -> AgentLibrary:
    plugin = root / "library" / "demo-lib"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "demo-lib"}')
    (plugin / "skills" / "hello").mkdir(parents=True)
    (plugin / "skills" / "hello" / "SKILL.md").write_text("---\nname: hello\ndescription: Says hello\n---\nHello.\n")
    (plugin / "hooks").mkdir()
    (plugin / "commands").mkdir()
    servers = [
        ("looper", {"type": "stdio", "command": "python", "args": ["-m", "looper.cli", "mcp-server"],
                    "env": {"LOOPER_AGENT_TOKEN": "planner.t0k"}}),
        ("web", {"type": "http", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer h"}}),
        ("feed", {"type": "sse", "url": "https://mcp.example.test/sse", "headers": {}}),
    ]
    items = (LoadedItem(ItemKind.PLUGIN, "demo-lib", "a" * 64, "demo-lib"),
             *(LoadedItem(ItemKind.MCP, name, "b" * 64, "") for name, _ in servers))
    return AgentLibrary(items, (str(plugin),), mcp_config=str(write_config(root, servers)), role="worker")


def test_mcp_servers_and_skills_go_in_the_config_and_the_notes_say_how_to_call_the_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in RESTORED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/Users/operator")
    root = tmp_path / "runtime"
    root.mkdir()
    launch = _launch(library=_library(root))
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(root), launch=launch)
    home = _home(command)
    config = (home / "config.toml").read_text()
    # A stdio server gets the operator's HOME back, as Grok starts it with its own.
    assert ('[mcp_servers.looper]\ncommand = "python"\nargs = ["-m", "looper.cli", "mcp-server"]\n'
            'env = { HOME = "/Users/operator", LOOPER_AGENT_TOKEN = "planner.t0k" }') in config
    assert '[mcp_servers.web]\nurl = "https://mcp.example.test/mcp"\nheaders = { Authorization = "Bearer h" }' in config
    assert '[mcp_servers.feed]\nurl = "https://mcp.example.test/sse"\ntype = "sse"\n' in config
    skills = str(root / "library" / "demo-lib" / "skills")
    assert f'[skills]\npaths = ["{skills}"]' in config
    # The token is in the home (0700), not on the command line.
    assert "planner.t0k" not in " ".join(command)
    notes = (home / RULES_FILE).read_text().partition("\n\n---\n\n")[0]
    assert "`use_tool`" in notes and "`looper__looper_tasks`" in notes and "`feed`, `looper`, `web`" in notes
    assert launch.record()["runtime_catalog"] == {"runtime": "grok", "skill_dirs": [skills], "skipped": [
        "plugin demo-lib: commands, hooks (Grok does not get them)"]}


def test_a_run_without_installed_items_records_no_catalog(tmp_path: Path) -> None:
    assert _launch().record().get("runtime_catalog") is None


def test_writing_a_new_run_home_removes_the_idle_ones(tmp_path: Path) -> None:
    local = tmp_path / ".local"
    login = tmp_path / "auth.json"
    login.write_text("stand-in login")
    idle, busy = write_home(local, "a = 1\n", "A", login=login), write_home(local, "a = 2\n", "B", login=login)
    then = time.time() - 25 * 3600
    for path in (idle, *idle.rglob("*")):
        os.utime(path, (then, then), follow_symlinks=False)
    assert write_home(local, "a = 2\n", "B", login=login) == busy and idle.is_dir()  # an existing home: nothing
    write_home(local, "a = 3\n", "C", login=login)
    assert not idle.exists() and busy.is_dir() and login.read_text() == "stand-in login"  # the link went, not its target


@pytest.mark.parametrize(("files", "expected"), [
    ({"": ["AGENTS.md"]}, ["AGENTS.md"]),
    ({"": ["AGENTS.md", "CLAUDE.md"]}, ["CLAUDE.md", "AGENTS.md"]),  # both: Grok reads both
    ({"": ["CLAUDE.md"], "sub": ["AGENTS.md", "CLAUDE.local.md"]}, ["CLAUDE.md", "sub/CLAUDE.local.md", "sub/AGENTS.md"]),
    ({"": ["README.md"]}, []),
], ids=["agents", "both", "nested", "none"])
def test_the_instruction_files_are_every_name_grok_reads_from_the_root_down(
    files: dict[str, list[str]], expected: list[str], tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for directory, names in files.items():
        (repo / directory).mkdir(parents=True, exist_ok=True)
        for name in names:
            (repo / directory / name).write_text(name)
    work = repo / "sub"
    work.mkdir(exist_ok=True)
    given = GROK.instruction_files(work)
    assert [str(f.path.relative_to(repo)) for f in given] == expected
    assert all(f.delivery is Delivery.NATIVE for f in given)


def test_a_run_takes_one_prompt_and_no_later_turn() -> None:
    async def _run() -> tuple[bytes, bool]:
        process = await asyncio.create_subprocess_exec(
            "cat", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        assert process.stdin is not None
        await GROK.write_turn(process.stdin, "the prompt")
        refused = False
        try:
            await GROK.write_turn(process.stdin, "a steer")
        except RuntimeError:
            refused = True
        out, _ = await process.communicate()
        return out, refused

    assert asyncio.run(_run()) == (b"the prompt", True)


def test_the_probe_reads_only_the_version_with_an_empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    grok = fake_claude(tmp_path / "grok", (
        "import os, sys\n"
        "assert sys.argv[1:] == ['--version'] and os.listdir(os.environ['GROK_HOME']) == []\n"
        "assert 'GROK_CONFIG' not in os.environ\n"
        "print('grok 1.0.62 (1474f9cbdd91) [stable]')\n"
    ))
    for name in (*DIRECTORY_VARS, *RESTORED_VARS, *grok_login.ENV_UNSET):
        monkeypatch.setenv(name, f"operator-{name}")
    run = subprocess.run

    def isolated_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        home = env["GROK_HOME"]
        assert env == {**GROK.environment(_launch()), "GROK_HOME": home, "HOME": home}
        assert not set((*DIRECTORY_VARS[1:], *RESTORED_VARS[1:], *grok_login.ENV_UNSET)) & env.keys()
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", isolated_run)
    probe = GROK.probe(grok)
    assert (probe.version, probe.error) == ("grok 1.0.62 (1474f9cbdd91) [stable]", None)
    assert GROK.probe(str(tmp_path / "nothing")).error == f"{str(tmp_path / 'nothing')!r} was not found"


def test_its_capabilities_cover_every_role() -> None:
    assert all(needs <= GROK.capabilities for needs in ROLE_CAPABILITIES.values())
    assert GROK.protocols == {ApiProtocol.OPENAI_RESPONSES} and GROK.auth_kinds == {AuthKind.OAUTH}
    assert GROK.login_models[0] == "grok-4.7"
    # Its own figure is the run's cost, which it reports only when a run ends: no per-run cap stops a run.
    assert GROK.cost_source(LOGIN) is CostSource.RUNTIME and not GROK.runtime_cost_capped
    assert GROK.budget_error({"type": "result", "subtype": "error_max_budget_usd"}) is None


@pytest.mark.parametrize(("cost", "usage", "kept"), [
    (0.014766, {"input_tokens": 4495, "output_tokens": 120}, 0.014766),
    (0, {"input_tokens": 4495, "output_tokens": 120}, None),  # Grok did not know it
    (0, {"input_tokens": 0, "output_tokens": 0}, 0),  # nothing spent
], ids=["reported", "unknown", "nothing-spent"])
def test_the_results_cost_is_groks_unless_it_reads_0_for_tokens_spent(
    cost: float, usage: dict[str, int], kept: float | None,
) -> None:
    (result,) = GrokStream(model="grok-4.7").translate({"type": "result", "subtype": "success", "result": "x",
                                                        "total_cost_usd": cost, "usage": usage})
    assert result.get("total_cost_usd") == kept


# --- Resident sessions ---

# A stand-in grok that keeps sessions the way Grok does: in $GROK_HOME/sessions/<work directory>/<id>, resumed by
# id. Each turn answers with the first line of Looper's prompt (from the home's rules file) and the message, and
# reports one model response of 60 uncached and 40 cached input tokens and 5 output tokens.
_FAKE_GROK = """\
import json, os, sys, urllib.parse
args = sys.argv[1:]
def flag(name):
    return args[args.index(name) + 1] if name in args else None
home = os.environ['GROK_HOME']
log = os.path.join(os.path.dirname(os.path.dirname(home)), 'argv.jsonl')
open(log, 'a').write(json.dumps({'home': home, 'user_home': os.environ['HOME'], 'args': args,
                                  'key': os.environ.get('XAI_API_KEY')}) + '\\n')
assert flag('--prompt-file') == '/dev/stdin' and flag('--output-format') == 'streaming-messages-json', args
message = open('/dev/stdin').read()
group = os.path.join(home, 'sessions', urllib.parse.quote(os.getcwd(), safe=''))
session = flag('--resume')
if session is not None:
    path = os.path.join(group, session)
    if not os.path.isdir(path):
        print(f'Error: session {session} not found', file=sys.stderr)
        sys.exit(1)
    turns = json.load(open(os.path.join(path, 'summary.json')))['turns'] + 1
else:
    os.makedirs(group, exist_ok=True)
    session = '0199a000-0000-7000-8000-%012d' % (len(os.listdir(group)) + 1)
    path = os.path.join(group, session)
    os.mkdir(path)
    turns = 1
json.dump({'turns': turns}, open(os.path.join(path, 'summary.json'), 'w'))
prompt = open(os.path.join(home, 'rules', 'looper.md')).read().partition('\\n\\n---\\n\\n')[2].splitlines()[0]
usage = {'input_tokens': 60, 'output_tokens': 5, 'cache_read_input_tokens': 40, 'cache_creation_input_tokens': 0}
text = f'{prompt} | {message} | turn {turns}'
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'system', 'subtype': 'init', 'session_id': session, 'model': 'grok-4.7', 'cwd': os.getcwd()})
emit({'type': 'assistant', 'session_id': session, 'parent_tool_use_id': None, 'message': {
    'role': 'assistant', 'model': 'grok-4.7', 'content': [{'type': 'text', 'text': text}], 'usage': usage}})
emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text, 'session_id': session,
      'total_cost_usd': 0, 'usage': usage, 'modelUsage': {'grok-4.7': {'inputTokens': 60, 'outputTokens': 5,
      'cacheReadInputTokens': 40, 'costUSD': 0}}})
"""
_TURN = Usage(input_tokens=60, output_tokens=5, cache_read_tokens=40, cache_creation_tokens=0)


def _resident_command(root: Path, executable: str, prompt: str) -> list[str]:
    return GROK.resident_command(executable, cwd=root, prompt_file=_prompt_file(root, prompt),
                                 launch=_launch(executable=executable))


def test_a_resident_session_runs_each_turn_in_a_new_process_that_resumes_it_in_the_owners_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = _operator(tmp_path, monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "xai-operator")
    root = tmp_path / "runtime"
    grok = fake_claude(tmp_path / "grok", _FAKE_GROK)
    owner_home = (root / ".local" / "grok" / "sessions" / "planner-task-1").absolute()

    async def _run() -> dict[str, Any]:
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        session = GROK.resident_session("planner-task-1")
        first = _resident_command(root, grok, "prompt A")
        await session.start(first, cwd=str(tmp_path), signature="a", env=_launch().environment())
        one = await session.run_turn("hello", on_event)
        two = await session.run_turn("again", on_event)
        saved, session_id = session.totals, seen[0]["session_id"]
        # The prompt changes: the owner starts over, resuming the saved session with its totals.
        await session.stop()
        second = _resident_command(root, grok, "prompt B") + GROK.resume_args(session_id)
        await session.start(second, cwd=str(tmp_path), signature="b", env=_launch().environment(), totals=saved)
        three = await session.run_turn("third", on_event)
        return {"turns": [one, two, three], "events": seen, "totals": session.totals, "id": session_id}

    chat = asyncio.run(_run())
    assert [t.result_event["result"] for t in chat["turns"]] == [
        "prompt A | hello | turn 1", "prompt A | again | turn 2", "prompt B | third | turn 3"]
    assert all(t.session_started for t in chat["turns"])
    assert {e.get("session_id") for e in chat["events"] if e["type"] != "usage"} == {chat["id"]}
    # Every turn ran in the owner's home, given the run home's files and the login link; the first one new.
    runs = [json.loads(line) for line in (root / ".local" / "grok" / "argv.jsonl").read_text().splitlines()]
    assert {(r["home"], r["user_home"]) for r in runs} == {(str(owner_home), str(owner_home))}
    assert [_flag(r["args"], "--resume") for r in runs] == [[], [chat["id"]], [chat["id"]]]
    assert all(r["key"] is None for r in runs)  # no API key to fall back to
    assert os.readlink(owner_home / "auth.json") == str(login)
    assert (owner_home / RULES_FILE).read_text().endswith("prompt B")
    # Each turn's own tokens; the session's totals add them up. Grok's cost of 0 for tokens spent is unknown.
    assert [t.usage for t in chat["turns"]] == [_TURN] * 3
    assert chat["totals"] == _TURN.plus(_TURN).plus(_TURN)
    assert all("total_cost_usd" not in t.result_event for t in chat["turns"])
    assert [e["model"] for e in chat["events"] if e["type"] == "usage"] == ["grok-4.7"] * 3


def test_a_new_session_removes_the_owners_earlier_ones(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    grok = fake_claude(tmp_path / "grok", _FAKE_GROK)

    async def _run() -> None:
        session = GROK.resident_session("manager")
        for message in ("one", "two"):
            await session.start(_resident_command(root, grok, "p"), cwd=str(tmp_path), signature="s",
                                env=_launch().environment())
            await session.run_turn(message, lambda event: asyncio.sleep(0))
            await session.stop()  # the owner moved on: its next session is a new one

    asyncio.run(_run())
    (group,) = (root / ".local" / "grok" / "sessions" / "manager" / "sessions").iterdir()
    assert [p.name for p in group.iterdir()] == ["0199a000-0000-7000-8000-000000000002"]


def test_a_session_grok_no_longer_has_starts_no_session(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    grok = fake_claude(tmp_path / "grok", _FAKE_GROK)

    async def _run() -> Any:
        session = GROK.resident_session("manager")
        command = _resident_command(root, grok, "prompt") + GROK.resume_args("0199a000-0000-7000-8000-00000000dead")
        await session.start(command, cwd=str(tmp_path), signature="s", env=_launch().environment())

        async def on_event(event: dict[str, Any]) -> None:
            raise AssertionError(f"no event expected, got {event}")

        turn = await session.run_turn("hello", on_event)
        return turn, session.stale("s")

    turn, stale = asyncio.run(_run())
    assert (turn.result_event, turn.session_started, turn.exit_code) == (None, False, 1)
    assert stale


def test_a_resident_session_needs_its_executable(tmp_path: Path) -> None:
    async def _run() -> None:
        session = ResidentGrok(label="manager")
        await session.start(_resident_command(tmp_path, str(tmp_path / "missing-grok"), "p"), cwd=str(tmp_path),
                            signature=None)

    with pytest.raises(OSError, match="was not found"):
        asyncio.run(_run())
