"""The Codex runtime (``looper/agents/runtimes/codex.py``).

Its events are checked against output recorded from Codex CLI 0.162.0
(``tests/fixtures/codex_exec``, local paths replaced); its launch, its
``CODEX_HOME`` and what it makes of the installed items (RFC P9) against the
files it writes; its resident sessions against a stand-in ``codex`` that keeps
a session across processes the way Codex does.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.mcp import write_config
from looper.agents.run_usage import Usage, run_usage
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported, adapter
from looper.agents.runtimes.codex import KEY_ENV, CodexRuntime, ResidentCodex
from looper.agents.system_prompt import SystemPrompt

FIXTURES = Path(__file__).parent / "fixtures" / "codex_exec"
CODEX: CodexRuntime = adapter("codex")  # type: ignore[assignment]
KEY = "sk-test-codex-0123456789"


def _provider(**fields: Any) -> ProviderSpec:
    values: dict[str, Any] = {"id": "prov-cpa", "name": "cpa", "auth": AuthKind.API_KEY,
                              "protocols": frozenset({ApiProtocol.OPENAI_RESPONSES}),
                              "base_url": "http://127.0.0.1:8317/v1", "api_key": KEY}
    return ProviderSpec(**{**values, **fields})


def _launch(*, executable: str = "codex", library: AgentLibrary | None = None,
            knowledge: AgentKnowledge | None = None, provider: ProviderSpec | None = None, effort: str = "low",
            env: tuple[tuple[str, str], ...] = ()) -> Launch:
    profile = AgentProfile("agent-codex", "codex/gpt-5.5",
                           RuntimeSpec("rt-codex", "codex", "codex", executable, CODEX.capabilities),
                           provider or _provider(), "gpt-5.5")
    return Launch(AgentSettings(profile=profile, effort=effort, env=env), library or AgentLibrary(),
                  knowledge or AgentKnowledge())


# --- Events ---


async def _parse_async(lines: bytes, model: str | None) -> list[dict[str, Any]]:
    reader = asyncio.StreamReader()
    reader.feed_data(lines)
    reader.feed_eof()
    return [event async for event in CODEX.parse(reader, model=model)]


def _events(name: str, model: str | None = "gpt-5.5") -> list[dict[str, Any]]:
    return asyncio.run(_parse_async((FIXTURES / f"{name}.jsonl").read_bytes(), model))


def _blocks(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [block for e in events if e["type"] in ("assistant", "user")
            for block in e["message"]["content"] if block["type"] == kind]


def _result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((e for e in events if e["type"] == "result"), None)


def test_a_run_becomes_init_text_tool_calls_and_a_result_with_its_tokens() -> None:
    events = _events("tools")
    init = events[0]
    assert (init["type"], init["subtype"], init["session_id"]) == ("system", "init", "01a1399d-cd15-7c73-bef6-b1ae36294dbf")
    assert all(e.get("session_id") == init["session_id"] for e in events)
    # Each command and patch is a tool call, then its result; the activity feed reads the calls.
    calls, results = _blocks(events, "tool_use"), _blocks(events, "tool_result")
    assert [(c["name"], c["id"]) for c in calls] == [("shell", "item_1"), ("apply_patch", "item_3")]
    assert [CODEX.tool_summary(c["name"], c["input"]) for c in calls] == [
        "Running: echo hello > a.txt && ls", "Editing: /tmp/looper-spike/work/b.txt"]
    assert [(r["tool_use_id"], r["content"], r["is_error"]) for r in results] == [
        ("item_1", "a.txt\n", False), ("item_3", "add /tmp/looper-spike/work/b.txt", False)]
    # Every message is text; the turn's last one is its reply.
    assert len(_blocks(events, "text")) == 3
    result = _result(events)
    assert result is not None and result["subtype"] == "success" and not result["is_error"]
    assert result["result"] == "Done. The code word is HERON-4471.\n\nEND-OF-REPLY."
    # Codex counts the cached input inside input_tokens; Looper counts it apart, as Claude Code does. No cost.
    tokens = Usage(input_tokens=49403 - 33920, output_tokens=208, cache_read_tokens=33920, cache_creation_tokens=0)
    assert run_usage(result) == tokens and list(result["modelUsage"]) == ["gpt-5.5"]
    # The turn's own tokens come first as a usage event, for the per-run spend cap.
    assert events[-2] == {"type": "usage", "model": "gpt-5.5", "usage": tokens.to_dict(),
                          "session_id": init["session_id"]}


def test_an_mcp_call_is_named_like_claude_codes_and_carries_its_text() -> None:
    events = _events("mcp")
    (call,), (result,) = _blocks(events, "tool_use"), _blocks(events, "tool_result")
    assert (call["name"], call["input"]) == ("mcp__spike__secret_word", {})
    assert (result["content"], result["is_error"]) == ("The secret word is FOXTROT-91.", False)
    assert _result(events)["result"] == "The secret word is `FOXTROT-91`."  # type: ignore[index]


def test_a_resumed_turn_reports_the_sessions_running_totals() -> None:
    events = _events("resumed")
    assert events[0]["session_id"] == "01a1399e-6d52-7a33-917b-946e69c73b9e"
    totals = run_usage(_result(events))
    # 16,284 input tokens on the first turn, 16,317 on this one: the result reports both.
    assert totals.input_tokens is not None and totals.cache_read_tokens is not None
    assert totals.input_tokens + totals.cache_read_tokens == 32601
    first = Usage(input_tokens=16284 - 2432, output_tokens=8, cache_read_tokens=2432, cache_creation_tokens=0)
    own = run_usage(_result(events), before=first)
    assert own.input_tokens + own.cache_read_tokens == 32601 - 16284  # type: ignore[operator]
    assert own.output_tokens == 28 - 8


def test_a_failed_turn_is_a_failed_result_and_its_retries_are_kept() -> None:
    events = _events("failed")
    retries = [e for e in events if e.get("subtype") == "codex_error"]
    assert len(retries) == 6 and all("401 Unauthorized" in e["message"] for e in retries)
    result = _result(events)
    assert result is not None and result["is_error"] and result["subtype"] == "error_during_execution"
    assert "401 Unauthorized" in result["errors"][0]


def test_a_run_cut_short_has_no_result_and_its_last_call_no_output() -> None:
    events = _events("interrupted")
    assert _result(events) is None
    assert [c["input"]["command"] for c in _blocks(events, "tool_use")] == ["/bin/zsh -lc 'sleep 30'"]
    assert _blocks(events, "tool_result") == []


def test_reasoning_and_a_warning_are_kept_and_change_no_reply() -> None:
    reasoning = _events("reasoning")
    assert [b["thinking"] for b in _blocks(reasoning, "thinking")] == ["**Clarifying plan tool availability**"]
    assert _result(reasoning)["result"] == "391"  # type: ignore[index]
    notice = _events("compaction_notice")
    (warning,) = [e for e in notice if e.get("subtype") == "codex_warning"]
    assert "compactions" in warning["message"]
    assert _result(notice)["result"] == "OK"  # type: ignore[index]


# --- The launch and its home ---


def _plugin(root: Path) -> Path:
    """A Claude Code plugin with a skill, a hook and a command."""
    plugin = root / "library" / "demo-lib"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "demo-lib"}')
    (plugin / "skills" / "hello").mkdir(parents=True)
    (plugin / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: Says hello\n  in two lines.\n---\nSay hello.\n")
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text("{}")
    (plugin / "commands").mkdir()
    (plugin / "commands" / "greet.md").write_text("Greet.")
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


def test_a_run_gets_a_home_of_its_own_with_only_the_generated_config_and_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The operator's own Codex configuration, which no run may read.
    home = tmp_path / "operator"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "operator-model"\n')
    (home / ".codex" / "AGENTS.md").write_text("OPERATOR INSTRUCTIONS\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    root = tmp_path / "runtime"
    knowledge = AgentKnowledge(index="## Project knowledge", documents=1, add_dirs=(str(tmp_path / "knowledge"),))
    launch = _launch(library=_library(root), knowledge=knowledge, env=(("FOO", "bar"),))
    command = CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(root), launch=launch)

    assert command[:4] == ["env", command[1], "codex", "exec"] and command[-2:] == ["--ephemeral", "-"]
    codex_home = Path(command[1].removeprefix("CODEX_HOME="))
    assert codex_home.parent == (root / ".local" / "codex" / "runs").absolute()
    assert sorted(p.name for p in codex_home.iterdir()) == ["AGENTS.md", "config.toml"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in codex_home.iterdir())
    assert stat.S_IMODE(codex_home.parent.parent.stat().st_mode) == 0o700
    # Every run flag, the model and the effort; the knowledge and the plugin with skills are mounted.
    assert {"--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"} <= set(command)
    assert command[command.index("-m") + 1] == "gpt-5.5"
    assert 'model_reasoning_effort="low"' in command
    mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--add-dir"]
    assert mounts == [str(tmp_path / "knowledge"), launch.library.plugin_dirs[0]]

    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["model_provider"] == "looper"
    assert config["model_providers"]["looper"] == {"name": "cpa", "base_url": "http://127.0.0.1:8317/v1",
                                                   "env_key": KEY_ENV, "wire_api": "responses"}
    # Codex's own skill list (with $HOME/.agents/skills) and plugin sync are off; the key stays out of shells.
    assert config["skills"] == {"include_instructions": False} and config["features"] == {"plugins": False}
    assert config["shell_environment_policy"] == {"exclude": [KEY_ENV]}
    # MCP servers in Codex's terms; Codex has no SSE.
    assert config["mcp_servers"] == {
        "notes": {"command": "notes-mcp", "args": ["--ro"], "env": {"NOTES_TOKEN": "t0k"}},
        "web": {"url": "https://mcp.example.test/mcp", "http_headers": {"Authorization": "Bearer h"}},
    }
    # The key is only in the environment, under the name the config gives; nothing of the operator's is used.
    environment = launch.environment()
    assert environment[KEY_ENV] == KEY and environment["FOO"] == "bar"
    generated = (codex_home / "config.toml").read_text() + (codex_home / "AGENTS.md").read_text()
    assert KEY not in generated and KEY not in " ".join(command)
    assert "operator-model" not in generated and "OPERATOR INSTRUCTIONS" not in generated
    # The same launch reuses its home; another prompt gets another.
    assert CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(root), launch=launch) == command
    other = CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(root, "OTHER"), launch=launch)
    assert other[1] != command[1]


def test_writing_a_new_run_home_removes_the_idle_ones(tmp_path: Path) -> None:
    from looper.agents.runtimes.codex import RUN_HOME_IDLE_SEC, write_home

    local = tmp_path / ".local"
    idle, busy = write_home(local, "# a\n", "A"), write_home(local, "# b\n", "B")
    (busy / "state_5.sqlite").write_bytes(b"written by a running Codex")
    then = time.time() - RUN_HOME_IDLE_SEC - 60
    for home in (idle, busy):
        for path in (home, *home.iterdir()):
            os.utime(path, (then, then))
    (busy / "state_5.sqlite").touch()  # still being written
    assert write_home(local, "# b\n", "B") == busy and idle.is_dir()  # an existing home removes nothing
    fresh = write_home(local, "# c\n", "C")
    assert sorted(p.name for p in fresh.parent.iterdir()) == sorted([busy.name, fresh.name])


def test_without_a_base_url_the_provider_is_openais_own_endpoint(tmp_path: Path) -> None:
    command = CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(tmp_path),
                                launch=_launch(provider=_provider(base_url=""), effort=""))
    config = tomllib.loads((Path(command[1].removeprefix("CODEX_HOME=")) / "config.toml").read_text())
    assert config["model_providers"]["looper"]["base_url"] == "https://api.openai.com/v1"
    assert not any(arg.startswith("model_reasoning_effort") for arg in command)


@pytest.mark.parametrize(("provider", "error"), [
    (_provider(api_key=None), "has no API key"),
    (_provider(protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES})), "openai-responses"),
    (_provider(auth=AuthKind.OAUTH, oauth_runtime_type="codex"), "API key provider only"),
])
def test_a_provider_codex_cannot_use_fails_the_launch(provider: ProviderSpec, error: str, tmp_path: Path) -> None:
    with pytest.raises(RuntimeUnsupported, match=error):
        CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=_launch(provider=provider))


def test_skills_become_documents_and_what_codex_cannot_load_is_recorded(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    launch = _launch(library=_library(root))
    command = CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(root), launch=launch)
    agents_md = (Path(command[1].removeprefix("CODEX_HOME=")) / "AGENTS.md").read_text()
    notes, _, prompt = agents_md.partition("\n\n---\n\n")
    # The runtime notes come first, then Looper's prompt as its file holds it.
    assert notes.startswith("# Runtime notes (Codex)") and prompt == "LOOPER PROMPT: the contract comes last."
    assert "last message" in notes
    skill_md = Path(launch.library.plugin_dirs[0]) / "skills" / "hello" / "SKILL.md"
    assert f"- demo-lib:hello: Says hello in two lines. ({skill_md})" in notes.splitlines()
    record = launch.record()
    assert record["runtime_catalog"] == {
        "runtime": "codex", "skills_as_documents": ["demo-lib:hello"],
        "skipped": ["plugin demo-lib: commands, hooks (Codex cannot load them)",
                    "MCP server feed: Codex has no sse transport"],
    }


def test_a_run_without_installed_items_says_nothing_of_skills(tmp_path: Path) -> None:
    launch = _launch()
    command = CODEX.run_command("codex", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)
    agents_md = (Path(command[1].removeprefix("CODEX_HOME=")) / "AGENTS.md").read_text()
    assert "skill" not in agents_md.lower()
    assert "runtime_catalog" not in launch.record() and "--add-dir" not in command
    config = tomllib.loads((Path(command[1].removeprefix("CODEX_HOME=")) / "config.toml").read_text())
    assert "mcp_servers" not in config


def test_a_run_takes_one_prompt_and_no_later_turn(tmp_path: Path) -> None:
    async def _run() -> tuple[bytes, bool]:
        process = await asyncio.create_subprocess_exec(
            "cat", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        assert process.stdin is not None
        await CODEX.write_turn(process.stdin, "the prompt")
        refused = False
        try:
            await CODEX.write_turn(process.stdin, "a steer")
        except RuntimeError:
            refused = True
        out, _ = await process.communicate()
        return out, refused

    # stdin closes after the prompt, which is how Codex knows it ended; a second turn is refused.
    assert asyncio.run(_run()) == (b"the prompt", True)


# --- Resident sessions ---

# A stand-in codex that keeps a session the way Codex does: a thread per new session, resumed by id, its running
# totals carried on in its CODEX_HOME. Each turn answers with the first line of the home's AGENTS.md prompt and
# the message, and reports 100 input tokens (40 of them cached) and 10 output tokens.
_FAKE_CODEX = """\
import json, os, sys
home = os.environ['CODEX_HOME']
args = sys.argv[1:]
open(os.path.join(home, 'argv.jsonl'), 'a').write(json.dumps(args) + '\\n')
assert args[0] == 'exec' and args[-1] == '-', args
message = sys.stdin.read()
state_path = os.path.join(home, 'threads.json')
threads = json.load(open(state_path)) if os.path.exists(state_path) else {}
if 'resume' in args:
    thread = args[args.index('resume') + 1]
    if thread not in threads:
        print(f'Error: thread/resume failed: no rollout found for thread id {thread}', file=sys.stderr)
        sys.exit(1)
else:
    thread = f'thread-{len(threads) + 1}'
    threads[thread] = 0
threads[thread] += 1
json.dump(threads, open(state_path, 'w'))
turns = threads[thread]
prompt = open(os.path.join(home, 'AGENTS.md')).read().partition('\\n\\n---\\n\\n')[2]
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'thread.started', 'thread_id': thread})
emit({'type': 'turn.started'})
emit({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': f'{prompt} | {message}'}})
emit({'type': 'turn.completed', 'usage': {'input_tokens': 100 * turns, 'cached_input_tokens': 40 * turns,
                                          'cache_write_input_tokens': 0, 'output_tokens': 10 * turns,
                                          'reasoning_output_tokens': 0}})
"""


def _resident_command(root: Path, executable: str, prompt: str) -> list[str]:
    return CODEX.resident_command(executable, cwd=root, prompt_file=_prompt_file(root, prompt),
                                  launch=_launch(executable=executable))


def test_a_resident_session_runs_each_turn_in_a_new_process_that_resumes_it(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    codex = fake_claude(tmp_path / "codex", _FAKE_CODEX)
    session_home = root / ".local" / "codex" / "sessions" / "planner-task-1"

    async def _run() -> dict[str, Any]:
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        session = CODEX.resident_session("planner-task-1")
        first = _resident_command(root, codex, "prompt A")
        await session.start(first, cwd=str(tmp_path), signature="a", env=_launch().environment())
        one = await session.run_turn("hello", on_event)
        two = await session.run_turn("again", on_event)
        saved = session.totals
        # The prompt changes: the owner starts over, resuming the saved session with its totals.
        await session.stop()
        second = _resident_command(root, codex, "prompt B") + CODEX.resume_args("thread-1")
        assert session.stale("a") is False and not session.started
        await session.start(second, cwd=str(tmp_path), signature="b", env=_launch().environment(), totals=saved)
        three = await session.run_turn("third", on_event)
        return {"turns": [one, two, three], "events": seen, "first": first}

    chat = asyncio.run(_run())
    one, two, three = chat["turns"]
    assert [t.result_event["result"] for t in chat["turns"]] == [
        "prompt A | hello", "prompt A | again", "prompt B | third"]
    assert all(t.session_started for t in chat["turns"])
    assert {e.get("session_id") for e in chat["events"]} == {"thread-1"}
    # The turns ran in the session's own home, the first one new, the others resuming its thread.
    argv = [json.loads(line) for line in (session_home / "argv.jsonl").read_text().splitlines()]
    assert [a[a.index("resume") + 1] if "resume" in a else None for a in argv] == [None, "thread-1", "thread-1"]
    assert all("--ephemeral" not in a for a in argv)
    assert (session_home / "AGENTS.md").read_text().endswith("prompt B")
    # Each turn's own tokens, from the session's running totals, in the record and in its usage event.
    own = Usage(input_tokens=60, output_tokens=10, cache_read_tokens=40, cache_creation_tokens=0)
    assert [t.usage for t in chat["turns"]] == [own] * 3
    assert [(e["model"], e["usage"]) for e in chat["events"] if e["type"] == "usage"] == [("gpt-5.5", own.to_dict())] * 3
    # Nothing ran in the run home the command names, which holds only what the turns copy.
    run_home = Path(chat["first"][1].removeprefix("CODEX_HOME="))
    assert sorted(p.name for p in run_home.iterdir()) == ["AGENTS.md", "config.toml"]


def test_a_session_codex_no_longer_has_starts_no_session(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    codex = fake_claude(tmp_path / "codex", _FAKE_CODEX)

    async def _run() -> Any:
        session = CODEX.resident_session("manager")
        command = _resident_command(root, codex, "prompt") + CODEX.resume_args("gone")
        await session.start(command, cwd=str(tmp_path), signature="s", env=_launch().environment())

        async def on_event(event: dict[str, Any]) -> None:
            raise AssertionError(f"no event expected, got {event}")

        turn = await session.run_turn("hello", on_event)
        return turn, session.stale("s")

    turn, stale = asyncio.run(_run())
    # As the tiers read a session Claude Code no longer has: no result, no session started, Codex's exit code.
    assert (turn.result_event, turn.session_started, turn.exit_code) == (None, False, 1)
    assert stale


def test_a_resident_session_needs_its_executable(tmp_path: Path) -> None:
    async def _run() -> None:
        session = ResidentCodex(label="manager")
        await session.start(_resident_command(tmp_path, str(tmp_path / "missing-codex"), "p"), cwd=str(tmp_path),
                            signature=None)

    with pytest.raises(OSError, match="was not found"):
        asyncio.run(_run())


def test_the_probe_reads_only_the_version(tmp_path: Path) -> None:
    codex = fake_claude(tmp_path / "codex", (
        "import os, sys\n"
        "assert sys.argv[1:] == ['--version'] and os.listdir(os.environ['CODEX_HOME']) == []\n"
        "print('codex-cli 0.162.0')\n"
    ))
    probe = CODEX.probe(codex)
    assert (probe.version, probe.error) == ("codex-cli 0.162.0", None)
    assert probe.capabilities == CODEX.capabilities
    assert CODEX.probe(str(tmp_path / "nothing")).error == f"{str(tmp_path / 'nothing')!r} was not found"


def test_its_capabilities_cover_every_role() -> None:
    from looper.agents.runtimes import ROLE_CAPABILITIES

    assert all(needs <= CODEX.capabilities for needs in ROLE_CAPABILITIES.values())
    assert os.environ.get(KEY_ENV) is None


def test_a_resident_turn_is_priced_and_stops_at_the_soft_cap_once_it_ends(tmp_path: Path) -> None:
    from looper.agents.claude_cli import CapStop, soft_cap_result
    from looper.agents.runtimes.pricing import RunSpend, pricer

    root = tmp_path / "runtime"
    codex = fake_claude(tmp_path / "codex", _FAKE_CODEX)
    provider = _provider()
    # gpt-5.5 at the built-in prices: each turn's 60 uncached, 40 cached input and 10 output tokens.
    turn_cost = (60 * 5.00 + 40 * 0.50 + 10 * 30.00) / 1_000_000

    async def _run() -> tuple[Any, Any, Usage, list[str]]:
        session = CODEX.resident_session("planner-task-2")
        await session.start(_resident_command(root, codex, "prompt"), cwd=str(tmp_path), signature="s",
                            env=_launch().environment(), price=pricer(provider))
        kinds: list[str] = []
        spend = RunSpend(provider, "gpt-5.5", cap=turn_cost / 2)  # the second turn's record, capped below a turn

        async def on_event(event: dict[str, Any]) -> None:
            kinds.append(event["type"])
            spend.add(event)

        def stop() -> CapStop | None:
            if not spend.exceeded:
                return None
            return CapStop(soft_cap_result(spend.usage.cost_usd or 0.0, spend.cap or 0.0), spend.usage)

        first = await session.run_turn("one", lambda event: asyncio.sleep(0))
        second = await session.run_turn("two", on_event, stop=stop)
        return first, second, session.totals, kinds

    first, second, totals, kinds = asyncio.run(_run())
    assert first.result_event["subtype"] == "success" and first.usage.cost_usd == pytest.approx(turn_cost)
    # A turn's tokens arrive as its usage event, right before its result: over the cap there, the turn ends there.
    assert kinds == ["system", "assistant", "usage"]
    assert CODEX.budget_error(second.result_event) == (
        f"Looper stopped the run at its per-run spend cap: {second.result_event['errors'][0]}")
    assert second.usage.cost_usd == pytest.approx(turn_cost) and CODEX.budget_error(first.result_event) is None
    # The session's totals carry the capped turn too, so the next one counts only its own.
    assert (totals.output_tokens, totals.cost_usd) == (20, pytest.approx(2 * turn_cost))
