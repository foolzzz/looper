"""The OMP runtime (``looper/agents/runtimes/omp.py``).

Its events are checked against output recorded from omp 18.4.3 through a
local CLIProxyAPI (``tests/fixtures/omp_json``, local paths replaced); its
launch, its home and what it makes of the installed items (RFC P9) against the
files it writes and the command it builds; its resident sessions against a
stand-in ``omp`` that keeps a session across processes the way omp does.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.profile_registry import AgentProfile, CatalogModel, ProviderSpec, RuntimeSpec
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.mcp import write_config
from looper.agents.run_usage import Usage, request_context, run_usage
from looper.agents.runtimes import (
    ROLE_CAPABILITIES, ApiProtocol, AuthKind, Capability, ReadOnly, RuntimeUnsupported, adapter,
)
from looper.agents.runtimes.omp import (
    KEY_ENV, MCP_PLUGIN, OVERLAY, RESTORED_VARS, SHELL_PREFIX_VAR, OmpRuntime, ResidentOmp, edited_paths, write_prompt,
)
from looper.agents.system_prompt import SystemPrompt

FIXTURES = Path(__file__).parent / "fixtures" / "omp_json"
OMP: OmpRuntime = adapter("omp")  # type: ignore[assignment]
KEY = "sk-test-omp-0123456789"


def _provider(**fields: Any) -> ProviderSpec:
    values: dict[str, Any] = {"id": "prov-cpa", "name": "cpa", "auth": AuthKind.API_KEY,
                              "protocols": frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT}),
                              "base_url": "http://127.0.0.1:8317/v1", "api_key": KEY}
    return ProviderSpec(**{**values, **fields})


def _launch(*, executable: str = "omp", library: AgentLibrary | None = None,
            knowledge: AgentKnowledge | None = None, provider: ProviderSpec | None = None, effort: str = "low",
            env: tuple[tuple[str, str], ...] = (), budget: float | None = None) -> Launch:
    profile = AgentProfile("agent-omp", "omp/gpt-5.5",
                           RuntimeSpec("rt-omp", "omp", "omp", executable, OMP.capabilities),
                           provider or _provider(), "gpt-5.5", effort or None)
    return Launch(AgentSettings(profile=profile, env=env, max_budget_usd=budget),
                  library or AgentLibrary(),
                  knowledge or AgentKnowledge())


# --- Events ---


async def _parse_async(lines: bytes, model: str | None) -> list[dict[str, Any]]:
    reader = asyncio.StreamReader()
    reader.feed_data(lines)
    reader.feed_eof()
    return [event async for event in OMP.parse(reader, model=model)]


def _events(name: str, model: str | None = "gpt-5.5") -> list[dict[str, Any]]:
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


def test_a_run_becomes_init_tool_calls_and_a_result_with_each_requests_tokens() -> None:
    events = _events("tools")
    init = events[0]
    assert (init["type"], init["subtype"], init["session_id"], init["model"]) == (
        "system", "init", "01a139e4-3a93-762c-869f-392bbaf8fa33", "gpt-5.5")
    assert all(e.get("session_id") == init["session_id"] for e in events)
    calls, results = _blocks(events, "tool_use"), _blocks(events, "tool_result")
    assert [c["name"] for c in calls] == ["bash", "read"]
    assert [OMP.tool_summary(c["name"], c["input"]) for c in calls] == [
        "Running: echo hello > a.txt && ls", "Reading: a.txt"]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [(c["id"], False) for c in calls]
    assert results[1]["content"] == "[a.txt#5BF9]\n1:hello"
    # Each model request reports its own tokens (omp's input excludes the cached part), in order.
    requests = [_tokens(6159, 84, 0), _tokens(633, 24, 5632), _tokens(677, 15, 5632)]
    assert _usages(events) == requests
    # The assistant events carry the request's usage, so the context size is known.
    assert [request_context(e) for e in events if e["type"] == "assistant"] == [6159, 633 + 5632, 677 + 5632]
    result = _result(events)
    assert result is not None and result["subtype"] == "success" and not result["is_error"]
    assert result["result"] == "HERON-1\n\nEND-OF-REPLY"
    total = requests[0].plus(requests[1]).plus(requests[2])
    assert run_usage(result) == total and list(result["modelUsage"]) == ["gpt-5.5"]


def test_a_resumed_run_names_the_same_session_and_reports_only_its_own_tokens() -> None:
    first, resumed = _events("tools"), _events("resumed")
    assert resumed[0]["session_id"] == first[0]["session_id"]
    assert run_usage(_result(resumed)) == _tokens(666, 92, 5632)
    assert "HERON-2" in _result(resumed)["result"]  # type: ignore[index]


def test_an_mcp_call_is_a_tool_of_its_own_name() -> None:
    events = _events("mcp")
    (call,), (result,) = _blocks(events, "tool_use"), _blocks(events, "tool_result")
    assert (call["name"], OMP.tool_summary(call["name"], call["input"])) == (
        "mcp__demo_lib_spike_secret_word", "Tool: mcp__demo_lib_spike_secret_word")
    assert (result["content"], result["is_error"]) == ("The secret word is FOXTROT-91.", False)
    assert _result(events)["result"] == "FOXTROT-91"  # type: ignore[index]


def test_a_subagents_tokens_come_with_its_tools_result() -> None:
    events = _events("subagent")
    (subagent,) = [e for e in events if e["type"] == "usage" and e.get("parent_tool_use_id")]
    task = [c for c in _blocks(events, "tool_use") if c["name"] == "task"][-1]
    assert subagent["parent_tool_use_id"] == task["id"]
    assert Usage.from_dict(subagent["usage"]) == _tokens(8435, 77, 15360)
    # The result counts the subagent's tokens beside the run's own requests.
    result = _result(events)
    assert result is not None and result["result"] == "SUB-OK"
    total = Usage()
    for usage in _usages(events):
        total = total.plus(usage)
    assert run_usage(result) == total


def test_a_failed_request_is_a_failed_result_and_an_interrupted_run_has_none() -> None:
    failed = _events("failed")
    result = _result(failed)
    assert result is not None and result["is_error"] and result["subtype"] == "error_during_execution"
    assert result["errors"] == ["401 Invalid API key"] and _usages(failed) == []
    interrupted = _events("interrupted")
    assert _result(interrupted) is None
    assert [r["content"] for r in _blocks(interrupted, "tool_result")] == ["Command aborted"]


def test_compactions_are_kept_and_one_that_ran_is_a_compact_boundary() -> None:
    from looper.agents.run_usage import compaction
    from looper.agents.runtimes.omp import OmpStream

    events = _events("compaction")
    # The recorded one was skipped (the last turn alone was too large): kept as omp events, with the notice.
    kept = [e["event"]["type"] for e in events if e.get("subtype") == "omp_event"]
    assert kept == ["auto_compaction_start", "auto_compaction_end", "notice"]
    assert _result(events)["result"] == "DONE"  # type: ignore[index]
    # One that ran (omp's CompactionResult) is what the record counts as a compaction.
    stream = OmpStream(model="gpt-5.5", session_id="s")
    (boundary,) = stream.translate({"type": "auto_compaction_end", "action": "context-full", "aborted": False,
                                    "willRetry": False, "result": {"summary": "…", "tokensBefore": 180_000,
                                                                   "firstKeptEntryId": "e1"}})
    assert compaction(boundary) == {"trigger": "auto", "pre_tokens": 180_000}


def test_the_files_an_edit_names() -> None:
    assert edited_paths("write", {"path": "a.txt", "content": "x"}) == ["a.txt"]
    assert edited_paths("ast_edit", {"paths": ["b.py", "c.py"], "ops": []}) == ["b.py", "c.py"]
    patch = "[greet.py#A1B2]\nPUT 1*:\n+def greet():\n[docs/PLAN.md#3C4D]\nPUT >2:\n+- task\n"
    assert edited_paths("edit", {"input": patch}) == ["greet.py", "docs/PLAN.md"]
    assert OMP.tool_summary("edit", {"input": patch}) == "Editing: greet.py, docs/PLAN.md"
    assert edited_paths("read", {"path": "a.txt"}) == []


# --- The launch and its home ---


def _plugin(root: Path) -> Path:
    """A Claude Code plugin with a skill, a hook, a command and an MCP server of its own."""
    plugin = root / "library" / "demo-lib"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "demo-lib"}')
    (plugin / "skills" / "hello").mkdir(parents=True)
    (plugin / "skills" / "hello" / "SKILL.md").write_text("---\nname: hello\ndescription: Says hello\n---\nHello.\n")
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text("{}")
    (plugin / "commands").mkdir()
    (plugin / "commands" / "greet.md").write_text("Greet.")
    (plugin / ".mcp.json").write_text('{"mcpServers": {"spike": {"command": "spike-mcp"}}}')
    return plugin


def _library(root: Path) -> AgentLibrary:
    servers = [
        ("notes", {"type": "stdio", "command": "notes-mcp", "args": ["--ro"], "env": {"NOTES_TOKEN": "t0k"}}),
        ("web", {"type": "http", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer h"}}),
        ("feed", {"type": "sse", "url": "https://mcp.example.test/sse", "headers": {}}),
    ]
    items = (LoadedItem(ItemKind.PLUGIN, "demo-lib", "a" * 64, "demo-lib"),
             *(LoadedItem(ItemKind.MCP, name, "b" * 64, "") for name, _ in servers))
    return AgentLibrary(items, (str(_plugin(root)),), mcp_config=str(write_config(root, servers)), role="worker")


def _prompt_file(root: Path, text: str = "LOOPER PROMPT: the contract comes last.") -> Path:
    return SystemPrompt(text).write_file(root)


def _flag(command: list[str], name: str) -> list[str]:
    return [command[i + 1] for i, arg in enumerate(command) if arg == name]


def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The operator's own omp and other personal configuration, which no run may read."""
    home = tmp_path / "operator"
    (home / ".omp" / "agent").mkdir(parents=True)
    (home / ".omp" / "agent" / "config.yml").write_text("enabledModels: [operator-model]\n")
    (home / ".omp" / "agent" / "models.yml").write_text("providers: {}\n")
    (home / ".agents" / "skills" / "mine").mkdir(parents=True)
    (home / ".env").write_text("OPERATOR_SECRET=1\n")
    # The machine's own values of the other restored variables (CI images set XDG_CONFIG_HOME, for one) would
    # otherwise come back too and make the expected environment depend on where the test runs.
    for name in RESTORED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".omp" / "agent"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    return home


def test_a_run_gets_a_home_of_its_own_with_only_the_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _operator(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    provider = _provider(catalog=(CatalogModel("gpt-5.5", context_window=272_000),))
    launch = _launch(provider=provider, env=(("FOO", "bar"),))
    command = OMP.run_command("omp", cwd=work, prompt_file=_prompt_file(root), launch=launch)

    assert command[:3] == ["env", command[1], "omp"] and command[-1] == "--no-session"
    home = Path(command[1].removeprefix("HOME="))
    assert home.parent == (root / ".local" / "omp" / "homes").absolute()
    assert stat.S_IMODE(home.parent.parent.stat().st_mode) == 0o700
    # Only what Looper wrote, the shared native library and the prompt; nothing of the operator's.
    files = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file() or p.is_symlink())
    assert files[:3] == [".omp/agent/models.yml", ".omp/natives", OVERLAY] and files[3].startswith("prompts/")
    assert (home / ".omp" / "natives").resolve() == (root / ".local" / "omp" / "natives").resolve()
    assert all(stat.S_IMODE((home / name).stat().st_mode) == 0o600 for name in (".omp/agent/models.yml", OVERLAY))
    # Print mode with JSON events, the model as the provider's, the effort, the overlay and the prompt.
    assert command[3:7] == ["-p", "--mode", "json", "--no-title"]
    assert _flag(command, "--model") == ["looper/gpt-5.5"] and _flag(command, "--thinking") == ["low"]
    assert _flag(command, "--config") == [str(home / OVERLAY)]
    (prompt,) = _flag(command, "--append-system-prompt")
    notes, _, text = Path(prompt).read_text().partition("\n\n---\n\n")
    assert notes.startswith("# Runtime notes (OMP)") and "final message" in notes
    assert text == "LOOPER PROMPT: the contract comes last."

    models = json.loads((home / ".omp" / "agent" / "models.yml").read_text())
    assert models == {"providers": {"looper": {
        "baseUrl": "http://127.0.0.1:8317/v1", "apiKey": KEY_ENV, "api": "openai-responses",
        "models": [{"id": "gpt-5.5", "name": "gpt-5.5", "reasoning": True, "input": ["text"], "contextWindow": 272_000,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}
    settings = json.loads((home / OVERLAY).read_text())
    assert settings["enabledModels"] == ["looper/gpt-5.5"]
    assert set(settings["modelRoles"].values()) == {"looper/gpt-5.5"}
    assert settings["tools"] == {"approvalMode": "yolo", "xdev": False} and settings["async"] == {"enabled": False}
    assert settings["retry"] == {"modelFallback": False} and settings["dev"] == {"autoqa": False}
    assert "disabledProviders" not in settings  # in a repository, its own content loads
    # The key only in the environment, under the name models.yml gives; none of the operator's configuration.
    environment = launch.environment()
    assert environment[KEY_ENV] == KEY and environment["FOO"] == "bar"
    assert not {"PI_CODING_AGENT_DIR", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"} & set(environment)
    generated = "".join(p.read_text() for p in home.rglob("*") if p.is_file())
    assert KEY not in generated and KEY not in " ".join(command)
    assert str(operator) not in generated and "operator-model" not in generated
    # The agent's shell commands get the operator's directories back, without the key.
    prefix = environment[SHELL_PREFIX_VAR]
    assert f"export HOME={shlex.quote(str(operator))};" in prefix
    assert f"export XDG_CACHE_HOME={shlex.quote(str(operator / '.cache'))};" in prefix
    assert prefix.endswith(f"unset {KEY_ENV};")
    # The same settings share the home; another prompt gets another prompt file in it.
    again = OMP.run_command("omp", cwd=work, prompt_file=_prompt_file(root, "OTHER"), launch=launch)
    assert again[1] == command[1] and _flag(again, "--append-system-prompt") != [prompt]


def test_outside_a_repository_the_sources_that_climb_are_off(tmp_path: Path) -> None:
    work = tmp_path / "plain"
    work.mkdir()
    command = OMP.run_command("omp", cwd=work, prompt_file=_prompt_file(tmp_path), launch=_launch())
    settings = json.loads(Path(_flag(command, "--config")[0]).read_text())
    assert settings["disabledProviders"] == ["native", "claude", "agents", "agents-md", "claude-md"]


@pytest.mark.parametrize(("protocols", "api", "base_url"), [
    ({ApiProtocol.OPENAI_CHAT}, "openai-completions", "https://api.openai.com/v1"),
    ({ApiProtocol.ANTHROPIC_MESSAGES}, "anthropic-messages", "https://api.anthropic.com"),
])
def test_the_provider_api_follows_its_protocols(protocols: set[ApiProtocol], api: str, base_url: str,
                                                tmp_path: Path) -> None:
    command = OMP.run_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path),
                              launch=_launch(provider=_provider(protocols=frozenset(protocols), base_url=""),
                                             effort=""))
    home = Path(command[1].removeprefix("HOME="))
    provider = json.loads((home / ".omp" / "agent" / "models.yml").read_text())["providers"]["looper"]
    assert (provider["api"], provider["baseUrl"]) == (api, base_url)
    assert "--thinking" not in command


@pytest.mark.parametrize(("provider", "error"), [
    (_provider(api_key=None), "has no API key"),
    (_provider(auth=AuthKind.OAUTH, oauth_runtime_type="omp"), "API key provider only"),
])
def test_a_provider_omp_cannot_use_fails_the_launch(provider: ProviderSpec, error: str, tmp_path: Path) -> None:
    with pytest.raises(RuntimeUnsupported, match=error):
        OMP.run_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=_launch(provider=provider))


def test_plugins_load_natively_and_their_own_mcp_servers_and_hooks_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _operator(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    knowledge = AgentKnowledge(index="## Project knowledge", documents=1, add_dirs=(str(tmp_path / "knowledge"),))
    launch = _launch(library=_library(root), knowledge=knowledge)
    command = OMP.run_command("omp", cwd=tmp_path, prompt_file=_prompt_file(root), launch=launch)
    home = Path(command[1].removeprefix("HOME="))
    # The installed plugin loads as it is (its skills, commands and agents); Looper's MCP servers come in a plugin
    # of the home. Knowledge is read by the index's paths: no --add-dir.
    assert _flag(command, "--plugin-dir") == [launch.library.plugin_dirs[0], str(home / "plugins" / "looper")]
    assert "--add-dir" not in command
    plugin = home / "plugins" / "looper"
    assert json.loads((plugin / ".claude-plugin" / "plugin.json").read_text()) == {"name": "looper"}
    restored = {"HOME": str(operator), "XDG_CACHE_HOME": str(operator / ".cache"),
                "CLAUDE_CONFIG_DIR": str(operator / ".claude")}
    assert json.loads((plugin / ".mcp.json").read_text()) == {"mcpServers": {
        "notes": {"type": "stdio", "command": "notes-mcp", "args": ["--ro"],
                  "env": {**restored, "NOTES_TOKEN": "t0k"}},
        "web": {"type": "http", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer h"}},
        "feed": {"type": "sse", "url": "https://mcp.example.test/sse", "headers": {}},
    }}
    # The plugin's own MCP server is turned off, and the record says what did not load.
    settings = json.loads((home / OVERLAY).read_text())
    assert settings["disabledExtensions"] == ["mcp:demo-lib:spike"]
    assert launch.record()["runtime_catalog"] == {"runtime": "omp", "skipped": [
        "plugin demo-lib: hooks (OMP cannot load them)",
        "MCP server demo-lib:spike: a plugin's own (Looper's MCP servers come from the catalog)"]}


def test_a_plugin_manifests_mcp_servers_are_turned_off_too(tmp_path: Path) -> None:
    plugin = tmp_path / "lib" / "dir-name"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "named", "mcpServers": {"inline": {"command": "x"}}}')
    (plugin / ".mcp.json").write_text('{"ignored": {"command": "y"}}')  # the manifest replaces it
    other = tmp_path / "lib" / "flat"
    other.mkdir()
    (other / ".mcp.json").write_text('{"flat": {"url": "https://mcp.example.test"}}')
    library = AgentLibrary((), (str(plugin), str(other)), role="worker")
    command = OMP.run_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=_launch(library=library))
    settings = json.loads(Path(_flag(command, "--config")[0]).read_text())
    assert settings["disabledExtensions"] == ["mcp:named:inline", "mcp:flat:flat"]


def test_a_run_without_installed_items_has_no_plugin_and_no_record(tmp_path: Path) -> None:
    launch = _launch()
    command = OMP.run_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)
    assert "--plugin-dir" not in command and "runtime_catalog" not in launch.record()
    home = Path(command[1].removeprefix("HOME="))
    assert not (home / "plugins").exists()
    assert "disabledExtensions" not in json.loads((home / OVERLAY).read_text())


def test_a_run_takes_one_prompt_and_no_later_turn() -> None:
    async def _run() -> tuple[bytes, bool]:
        process = await asyncio.create_subprocess_exec(
            "cat", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        assert process.stdin is not None
        await OMP.write_turn(process.stdin, "the prompt")
        refused = False
        try:
            await OMP.write_turn(process.stdin, "a steer")
        except RuntimeError:
            refused = True
        out, _ = await process.communicate()
        return out, refused

    assert asyncio.run(_run()) == (b"the prompt", True)


def test_writing_a_new_prompt_removes_the_homes_idle_prompts_and_logs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    idle, busy = write_prompt(home, "A"), write_prompt(home, "B")
    logs = home / ".omp" / "logs"
    logs.mkdir(parents=True)
    old_log, new_log = logs / "omp.1.log", logs / "omp.2.log"
    old_log.write_text("x")
    new_log.write_text("y")
    then = time.time() - 25 * 3600
    for path in (idle, busy, old_log):
        os.utime(path, (then, then))
    busy.touch()
    assert write_prompt(home, "B") == busy and idle.is_file()  # an existing prompt removes nothing
    write_prompt(home, "C")
    assert not idle.exists() and busy.is_file() and not old_log.exists() and new_log.is_file()


# --- Resident sessions ---

# A stand-in omp that keeps sessions the way omp does: a session file in --session-dir per session, resumed by id.
# Each turn answers with the first line of Looper's prompt and the message, and reports two model requests of 60
# uncached and 40 cached input tokens and 5 output tokens each, the first one calling a tool.
_FAKE_OMP = """\
import json, os, sys, time
args = sys.argv[1:]
def flag(name):
    return args[args.index(name) + 1] if name in args else None
home = os.environ['HOME']
open(os.path.join(home, 'argv.jsonl'), 'a').write(json.dumps({'args': args, 'key': os.environ.get('LOOPER_PROVIDER_API_KEY')}) + '\\n')
assert args[:3] == ['-p', '--mode', 'json'], args
message = sys.stdin.read()
sessions = flag('--session-dir')
session = flag('--resume')
if session is not None:
    found = [n for n in os.listdir(sessions) if n.endswith('_' + session + '.jsonl')]
    if not found:
        print(f'Error: Session "{session}" not found.', file=sys.stderr)
        sys.exit(1)
    path = os.path.join(sessions, found[0])
    turns = json.load(open(path))['turns'] + 1
else:
    session = 'sess-%d' % (len([n for n in os.listdir(sessions) if n.endswith('.jsonl')]) + 1) if sessions else 'eph'
    path = os.path.join(sessions, '2026-10-14T00-00-00-000Z_%s.jsonl' % session) if sessions else None
    turns = 1
    if sessions:
        os.mkdir(path[:-len('.jsonl')])
if path:
    json.dump({'turns': turns}, open(path, 'w'))
prompt = open(flag('--append-system-prompt')).read().partition('\\n\\n---\\n\\n')[2].splitlines()[0]
usage = {'input': 60, 'output': 5, 'cacheRead': 40, 'cacheWrite': 0}
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'session', 'version': 3, 'id': session, 'cwd': os.getcwd()})
emit({'type': 'agent_start'})
emit({'type': 'message_end', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': message}]}})
emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse', 'model': 'gpt-5.5', 'usage': usage,
      'content': [{'type': 'toolCall', 'id': 'call_1', 'name': 'bash', 'arguments': {'command': 'true'}}]}})
emit({'type': 'message_end', 'message': {'role': 'toolResult', 'toolCallId': 'call_1', 'toolName': 'bash',
      'isError': False, 'content': [{'type': 'text', 'text': ''}]}})
time.sleep(float(os.environ.get('FAKE_OMP_PAUSE') or 0))
if os.environ.get('FAKE_OMP_SECOND'):  # the turn's second request starts
    open(os.environ['FAKE_OMP_SECOND'], 'w').close()
emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop', 'model': 'gpt-5.5', 'usage': usage,
      'content': [{'type': 'text', 'text': f'{prompt} | {message} | turn {turns}'}]}})
emit({'type': 'agent_end', 'messages': [], 'isTerminal': True, 'yielded': True})
"""
_TURN = Usage(input_tokens=120, output_tokens=10, cache_read_tokens=80, cache_creation_tokens=0)


def _resident_command(root: Path, executable: str, prompt: str) -> list[str]:
    return OMP.resident_command(executable, cwd=root, prompt_file=_prompt_file(root, prompt),
                                launch=_launch(executable=executable))


def test_a_resident_session_runs_each_turn_in_a_new_process_that_resumes_it(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    omp = fake_claude(tmp_path / "omp", _FAKE_OMP)
    sessions = root / ".local" / "omp" / "sessions" / "planner-task-1"

    async def _run() -> dict[str, Any]:
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        session = OMP.resident_session("planner-task-1")
        first = _resident_command(root, omp, "prompt A")
        await session.start(first, cwd=str(tmp_path), signature="a", env=_launch().environment())
        one = await session.run_turn("hello", on_event)
        two = await session.run_turn("again", on_event)
        saved = session.totals
        # The prompt changes: the owner starts over, resuming the saved session with its totals.
        await session.stop()
        second = _resident_command(root, omp, "prompt B") + OMP.resume_args("sess-1")
        assert not session.started
        await session.start(second, cwd=str(tmp_path), signature="b", env=_launch().environment(), totals=saved)
        three = await session.run_turn("third", on_event)
        return {"turns": [one, two, three], "events": seen, "first": first, "totals": session.totals}

    chat = asyncio.run(_run())
    assert [t.result_event["result"] for t in chat["turns"]] == [
        "prompt A | hello | turn 1", "prompt A | again | turn 2", "prompt B | third | turn 3"]
    assert all(t.session_started for t in chat["turns"])
    assert {e.get("session_id") for e in chat["events"]} == {"sess-1"}
    # Each turn ran in the run home, with the owner's session directory; the first one new, the others resuming.
    home = Path(chat["first"][1].removeprefix("HOME="))
    runs = [json.loads(line) for line in (home / "argv.jsonl").read_text().splitlines()]
    assert [_flag(r["args"], "--session-dir") for r in runs] == [[str(sessions)]] * 3
    assert [_flag(r["args"], "--resume") for r in runs] == [[], ["sess-1"], ["sess-1"]]
    assert all("--no-session" not in r["args"] and r["key"] == KEY for r in runs)
    # Each turn's own tokens, from its requests' usage events; the session's totals add them up.
    assert [t.usage for t in chat["turns"]] == [_TURN] * 3
    request = {"input_tokens": 60, "output_tokens": 5, "cache_read_tokens": 40, "cache_creation_tokens": 0}
    assert [(e["model"], e["usage"]) for e in chat["events"] if e["type"] == "usage"] == [("gpt-5.5", request)] * 6
    assert chat["totals"] == _TURN.plus(_TURN).plus(_TURN)


def test_a_new_session_removes_the_owners_earlier_ones(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    omp = fake_claude(tmp_path / "omp", _FAKE_OMP)
    sessions = root / ".local" / "omp" / "sessions" / "manager"

    async def _run() -> None:
        session = OMP.resident_session("manager")
        await session.start(_resident_command(root, omp, "p"), cwd=str(tmp_path), signature="s",
                            env=_launch().environment())
        await session.run_turn("one", lambda event: asyncio.sleep(0))
        await session.stop()  # the owner moved on: its next session is a new one
        await session.start(_resident_command(root, omp, "p"), cwd=str(tmp_path), signature="s",
                            env=_launch().environment())
        await session.run_turn("two", lambda event: asyncio.sleep(0))

    asyncio.run(_run())
    assert sorted(p.name for p in sessions.iterdir()) == [
        "2026-10-14T00-00-00-000Z_sess-2", "2026-10-14T00-00-00-000Z_sess-2.jsonl"]


def test_a_session_omp_no_longer_has_starts_no_session(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    omp = fake_claude(tmp_path / "omp", _FAKE_OMP)

    async def _run() -> Any:
        session = OMP.resident_session("manager")
        command = _resident_command(root, omp, "prompt") + OMP.resume_args("gone")
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
        session = ResidentOmp(label="manager")
        await session.start(_resident_command(tmp_path, str(tmp_path / "missing-omp"), "p"), cwd=str(tmp_path),
                            signature=None)

    with pytest.raises(OSError, match="was not found"):
        asyncio.run(_run())


def test_a_resident_turn_is_priced_and_stops_at_the_soft_cap_between_requests(tmp_path: Path) -> None:
    from looper.agents.claude_cli import CapStop, soft_cap_result
    from looper.agents.runtimes.pricing import RunSpend, pricer

    root = tmp_path / "runtime"
    omp = fake_claude(tmp_path / "omp", _FAKE_OMP)
    provider = _provider()
    second = tmp_path / "second-request"
    # gpt-5.5 at the built-in prices: each request's 60 uncached and 40 cached input tokens and 5 output tokens.
    request_cost = (60 * 5.00 + 40 * 0.50 + 5 * 30.00) / 1_000_000

    async def _run() -> tuple[Any, Usage, list[str]]:
        session = OMP.resident_session("planner-task-2")
        await session.start(_resident_command(root, omp, "prompt"), cwd=str(tmp_path), signature="s",
                            env={**_launch().environment(), "FAKE_OMP_PAUSE": "30", "FAKE_OMP_SECOND": str(second)},
                            price=pricer(provider))
        kinds: list[str] = []
        spend = RunSpend(provider, "gpt-5.5", cap=request_cost / 2)  # capped below one request

        async def on_event(event: dict[str, Any]) -> None:
            kinds.append(event["type"])
            spend.add(event)

        def stop() -> CapStop | None:
            if not spend.exceeded:
                return None
            return CapStop(soft_cap_result(spend.usage.cost_usd or 0.0, spend.cap or 0.0), spend.usage)

        capped = await session.run_turn("two", on_event, stop=stop)
        # Stopped during the pause after the first request (30 s, far longer than stopping takes on a slow
        # machine): the second request never started.
        assert not second.exists()
        return capped, session.totals, kinds

    capped, totals, kinds = asyncio.run(_run())
    # The first request's usage event took the turn over its cap: it ends there, before the tool's result.
    assert kinds == ["system", "assistant", "usage"]
    assert OMP.budget_error(capped.result_event) == (
        f"Looper stopped the run at its per-run spend cap: {capped.result_event['errors'][0]}")
    assert capped.usage.cost_usd == pytest.approx(request_cost)
    assert (totals.output_tokens, totals.cost_usd) == (5, pytest.approx(request_cost))


def test_the_probe_reads_only_the_version_with_an_empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    omp = fake_claude(tmp_path / "omp", (
        "import os, sys\n"
        "assert sys.argv[1:] == ['--version'] and os.listdir(os.environ['HOME']) == []\n"
        "assert 'PI_CODING_AGENT_DIR' not in os.environ\n"
        "print('omp/18.4.3')\n"
    ))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "operator"))
    probe = OMP.probe(omp)
    assert (probe.version, probe.error) == ("omp/18.4.3", None)
    assert OMP.probe(str(tmp_path / "nothing")).error == f"{str(tmp_path / 'nothing')!r} was not found"


def test_its_capabilities_cover_every_role() -> None:
    assert all(needs <= OMP.capabilities for needs in ROLE_CAPABILITIES.values())
    assert OMP.protocols == {ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}


@pytest.mark.parametrize(("version", "read_tools"), [
    ("omp/18.4.3", True),
    ("omp/19.0.0", True),
    ("omp/18.4.2", False),
    ("omp/17.9.9", False),
    (None, False),
    ("weird", False),
])
def test_only_verified_versions_can_restrict_the_tools(version: str | None, read_tools: bool) -> None:
    assert (Capability.READ_TOOLS in OMP.capabilities_for(version)) is read_tools


def test_a_run_that_keeps_its_session_names_its_directory_instead_of_no_session(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    keep = tmp_path / "runtime" / ".local" / "omp" / "run-sessions" / "task-1" / "worker-1"
    plain = OMP.run_command("omp", cwd=work, prompt_file=_prompt_file(tmp_path), launch=_launch())
    kept = OMP.run_command("omp", cwd=work, prompt_file=_prompt_file(tmp_path), launch=_launch(), session_dir=keep)
    assert plain[-1] == "--no-session" and "--no-session" not in kept
    assert kept[-2:] == ["--session-dir", str(keep)] and keep.is_dir() and kept[:-2] == plain[:-1]


@pytest.mark.parametrize("role", [AgentType.PLANNER, None, AgentType.WORKER])
def test_only_a_planner_launch_limits_its_tools(tmp_path: Path, role: AgentType | None) -> None:
    launch = replace(_launch(), role=role)
    command = OMP.resident_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)
    assert OMP.read_only is ReadOnly.TOOLS
    if role is AgentType.PLANNER:
        assert _flag(command, "--tools") == ["read,glob,grep"]
        assert launch.record()["read_only"] == "tools"
    else:
        assert "--tools" not in command
        assert "read_only" not in launch.record()


@pytest.mark.parametrize(("read_tools", "mode"), [(False, ReadOnly.CHECK), (True, ReadOnly.TOOLS)])
def test_a_planners_read_only_mode_uses_its_probed_capabilities(
    tmp_path: Path, read_tools: bool, mode: ReadOnly,
) -> None:
    launch = _launch()
    capabilities = launch.profile.runtime.capabilities - {Capability.READ_TOOLS}
    if read_tools:
        capabilities |= {Capability.READ_TOOLS}
    runtime = replace(launch.profile.runtime, capabilities=capabilities)
    profile = replace(launch.profile, runtime=runtime)
    launch = replace(launch, settings=replace(launch.settings, profile=profile), role=AgentType.PLANNER)
    command = OMP.resident_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)

    assert launch.read_only_mode is mode
    assert launch.record()["read_only"] == mode.value
    if read_tools:
        assert _flag(command, "--tools") == ["read,glob,grep"]
    else:
        assert "--tools" not in command


def test_a_planner_keeps_its_mcp_plugin(tmp_path: Path) -> None:
    launch = replace(_launch(library=_library(tmp_path)), role=AgentType.PLANNER)
    command = OMP.resident_command("omp", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)
    home = Path(command[1].removeprefix("HOME="))
    plugin = home / "plugins" / MCP_PLUGIN
    assert str(plugin) in _flag(command, "--plugin-dir")
    assert _flag(command, "--tools") == ["read,glob,grep"]
    servers = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]
    assert servers["notes"]["command"] == "notes-mcp"


def test_a_consult_resumes_a_copy_of_the_runs_session_with_only_the_reading_tools(tmp_path: Path) -> None:
    from dataclasses import replace

    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    kept = tmp_path / "runtime" / ".local" / "omp" / "run-sessions" / "task-1" / "worker-1"
    kept.mkdir(parents=True)
    (kept / "session.jsonl").write_text("{}\n")
    fork = tmp_path / "runtime" / ".local" / "omp" / "consult-sessions" / "task-1" / "consult-1"
    command = OMP.consult_command("omp", cwd=work, prompt_file=_prompt_file(tmp_path),
                                  launch=replace(_launch(), consult=True), session_id="s-1", session_dir=kept,
                                  fork_dir=fork)
    assert _flag(command, "--tools") == ["read,glob,grep"]
    assert command[-4:] == ["--session-dir", str(fork), "--resume", "s-1"] and (fork / "session.jsonl").is_file()
    assert "--tools" not in OMP.run_command("omp", cwd=work, prompt_file=_prompt_file(tmp_path), launch=_launch())
