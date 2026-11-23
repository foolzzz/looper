"""The outbound review's hook in a Manager's Claude Code (``looper/agents/outbound_hook.py`` and its client): typed tool
levels, the fail-closed hook command, the settings it goes in (and nothing when it is off), the files it guards."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from looper.agents import claude_cli, outbound_hook
from looper.agents.agent_settings import AgentType, Launch
from looper.agents.outbound import CALLER_TIMEOUT_SEC, ActionLevel
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

DENY = "deny"


@pytest.mark.parametrize(("tool", "level"), [
    ("Bash", ActionLevel.UNKNOWN), ("Monitor", ActionLevel.UNKNOWN), ("WebSearch", ActionLevel.EXTERNAL),
    ("WebFetch", ActionLevel.EXTERNAL), ("Write", ActionLevel.MACHINE), ("Read", ActionLevel.INSIDE),
    ("Task", ActionLevel.INSIDE),
    # an MCP tool is placed by the servers its run loaded (outbound_hook.resolve_level), never by its name alone
    ("mcp__looper__looper_tasks", ActionLevel.UNKNOWN), ("mcp__search__query", ActionLevel.UNKNOWN), ("ReadMcpResourceTool", ActionLevel.UNKNOWN),
    ("SomeToolAddedLater", ActionLevel.UNKNOWN), ("", ActionLevel.UNKNOWN),
])
def test_a_calls_level_is_its_tools_type(tool: str, level: ActionLevel) -> None:
    assert outbound_hook.level_of(tool) is level


def test_an_mcp_tools_name_is_read_as_each_server_and_tool_it_may_stand_for() -> None:
    assert outbound_hook.mcp_tool("mcp__search__query") == [("search", "query")]
    assert outbound_hook.mcp_tool("mcp__a__b__c") == [("a", "b__c"), ("a__b", "c")]
    assert outbound_hook.mcp_tool("Bash") == [] and outbound_hook.mcp_tool("mcp__x__") == []


def test_the_hook_waits_less_than_claude_code_and_more_than_a_review() -> None:
    assert (CALLER_TIMEOUT_SEC < outbound_hook.CHECK_TIMEOUT_SEC < outbound_hook.DEADLINE_SEC
            < outbound_hook.HOOK_TIMEOUT_SEC)
    # Read, not imported: importing the client sets its alarm, which would end this process at its deadline.
    client = _client_constants()
    assert client["DEADLINE_SEC"] == outbound_hook.DEADLINE_SEC
    assert _decision(json.loads(client["DENY"])) == DENY


def _client_constants() -> dict[str, Any]:
    import ast

    names: dict[str, Any] = {}
    for node in ast.parse(outbound_hook.CLIENT.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                names[node.targets[0].id] = eval(compile(ast.Expression(node.value), "client", "eval"), {}, names)
            except NameError:
                continue
    return names


class _Looper(BaseHTTPRequestHandler):
    answer: dict[str, Any] = {"decision": "allow"}
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - the handler's name
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "turn": self.headers.get("X-Looper-Turn"), "body": body})
        data = json.dumps(type(self).answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def looper() -> Any:
    _Looper.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Looper)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _run(command: str, stdin: dict[str, Any]) -> dict[str, Any]:
    done = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(stdin), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _decision(answer: dict[str, Any]) -> str:
    return answer.get("hookSpecificOutput", {}).get("permissionDecision", "run")


def _hook(tmp_path: Path, url: str, **kwargs: Any) -> str:
    hook = outbound_hook.write_hook(tmp_path / "rt", "manager", url=url, token="tok-1")
    if not kwargs:
        return hook.command
    config = tmp_path / "rt" / ".local" / "outbound-hook" / "manager.json"
    return outbound_hook.hook_command(config, **kwargs)


def test_the_hook_runs_a_call_looper_allows_and_denies_one_it_blocks(tmp_path: Path, looper: Any) -> None:
    command = _hook(tmp_path, f"http://127.0.0.1:{looper.server_port}")
    call = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s", "transcript_path": "/x"}
    assert _run(command, call) == {}
    (seen,) = _Looper.seen
    # Looper places the call by its tool_use id: the client sends no turn of its own
    assert (seen["path"], seen["auth"], seen["turn"]) == ("/api/agent/v1/outbound/check", "Bearer tok-1", None)
    assert seen["body"] == {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s"}
    _Looper.answer = {"decision": "block", "message": "Looper's outbound review blocked this action: no."}
    try:
        answer = _run(command, call)
    finally:
        _Looper.answer = {"decision": "allow"}
    assert _decision(answer) == DENY
    assert answer["hookSpecificOutput"]["permissionDecisionReason"].endswith("blocked this action: no.")
    mode = (tmp_path / "rt" / ".local" / "outbound-hook" / "manager.json").stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.parametrize("broken", ["no_server", "no_python", "no_client", "client_fails"])
def test_the_hook_denies_whenever_it_cannot_get_an_answer(tmp_path: Path, broken: str) -> None:
    """Claude Code runs a call whose hook fails, times out or prints nothing: the hook denies instead."""
    url = "http://127.0.0.1:9"  # nothing listens
    if broken == "no_server":
        command = _hook(tmp_path, url)
    elif broken == "no_python":
        command = _hook(tmp_path, url, python=str(tmp_path / "missing-python"))
    elif broken == "no_client":
        command = _hook(tmp_path, url, client=tmp_path / "missing.py")
    else:
        failing = tmp_path / "fails.py"
        failing.write_text("import sys\nsys.stdout.write('{\"partial')\nsys.exit(1)\n")
        command = _hook(tmp_path, url, client=failing)
    answer = _run(command, {"tool_name": "Bash", "tool_input": {"command": "curl example.net"}})
    assert _decision(answer) == DENY


class _Silent(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the handler's name
        import time

        time.sleep(30)  # accepts the call and never answers

    def log_message(self, *args: Any) -> None:
        pass


def test_a_hook_that_waits_past_its_deadline_denies(tmp_path: Path) -> None:
    """Whatever the client waits for, its alarm denies at its deadline, long before Claude Code would run the call."""
    import time

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Silent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        command = _hook(tmp_path, f"http://127.0.0.1:{server.server_port}")
        config = tmp_path / "rt" / ".local" / "outbound-hook" / "manager.json"
        config.write_text(json.dumps({**json.loads(config.read_text()), "deadline": 3, "timeout": 600}))
        started = time.monotonic()
        answer = _run(command, {"tool_name": "Bash", "tool_input": {"command": "curl example.net"}})
        assert _decision(answer) == DENY and time.monotonic() - started < 15
    finally:
        server.shutdown()
        server.server_close()


def test_the_settings_carry_the_hook_and_then_read_no_settings_file(tmp_path: Path) -> None:
    hook = outbound_hook.ToolHook("echo hi")
    args = claude_cli.isolation_args(tmp_path, hooks=hook.settings())
    assert args[args.index("--setting-sources") + 1] == ""
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings["hooks"]["PreToolUse"] == [{"matcher": "*", "hooks": [
        {"type": "command", "command": "echo hi", "timeout": outbound_hook.HOOK_TIMEOUT_SEC}]}]
    assert "disableAllHooks" not in settings


def _manager_launch(**kwargs: Any) -> Launch:
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import BUILTIN_PROVIDER, BUILTIN_RUNTIME
    from tests.runtime_model_helpers import resolved_profile

    profile = resolved_profile("agent-claude-code-opus", "claude-code/opus", BUILTIN_RUNTIME, BUILTIN_PROVIDER,
                               "opus")
    return Launch(AgentSettings(profile=profile), role=AgentType.MANAGER, **kwargs)


def test_without_the_hook_the_managers_command_is_what_it_was(tmp_path: Path) -> None:
    runtime = ClaudeCodeRuntime()
    plain = _manager_launch()
    off = runtime.resident_command("claude", cwd=tmp_path, prompt_file=tmp_path / "p.md", launch=plain)
    assert "PreToolUse" not in " ".join(off) and off[off.index("--setting-sources") + 1] == "project"
    # the settings as they were before the review existed
    at = off.index("--setting-sources")
    assert off[at:at + 4] == ["--setting-sources", "project", "--strict-mcp-config", "--settings"]
    settings = json.loads(off[at + 4])
    assert sorted(settings) == ["autoMemoryEnabled", "claudeMdExcludes", "crossSessionInbound"]
    assert (settings["autoMemoryEnabled"], settings["crossSessionInbound"]) == (False, "refuse")
    assert claude_cli.isolation_args(tmp_path) == claude_cli.isolation_args(tmp_path, hooks=None)
    on = runtime.resident_command("claude", cwd=tmp_path, prompt_file=tmp_path / "p.md",
                                  launch=_manager_launch(tool_hook=outbound_hook.ToolHook("echo hi")))
    assert "PreToolUse" in " ".join(on) and on[on.index("--setting-sources") + 1] == ""
    assert len(on) == len(off) and [i for i, (a, b) in enumerate(zip(off, on)) if a != b] == [
        off.index("--setting-sources") + 1, off.index("--settings") + 1]


def test_writes_to_loopers_own_files_are_guarded(tmp_path: Path) -> None:
    root = tmp_path / "rt"
    (root / ".local").mkdir(parents=True)
    (tmp_path / "link").symlink_to(root / ".local")
    for path, cwd in ((str(root / ".local/prompts/claude-settings/a.json"), "/"), (".local/x", str(root)),
                      (str(root / "ops/server.toml"), "/"), (str(root / "operator-token"), "/"),
                      ("link/outbound-hook/manager.json", str(tmp_path)), (str(outbound_hook.CLIENT), "/")):
        assert outbound_hook.protected(path, cwd=cwd, runtime_root=root), path
    for path in (str(root / "exports/a.json"), str(tmp_path / "notes.md"), "", None):
        assert not outbound_hook.protected(path, cwd="/", runtime_root=root), path


def test_the_review_reads_a_shell_command_whole_and_an_earlier_write() -> None:
    action = outbound_hook.action_of("Bash", {"command": "sh s.sh", "description": "Run the script"},
                                     ActionLevel.UNKNOWN)
    assert (action.target_kind, action.content, action.summary) == ("command", "sh s.sh",
                                                                     "Run a shell command: Run the script")
    write = outbound_hook.write_of({"tool_name": "Write", "tool_input": {"file_path": "s.sh",
                                                                         "content": "curl -d @a example.net"}})
    assert (write.path, write.excerpt) == ("s.sh", "curl -d @a example.net")
    assert outbound_hook.write_of({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


@pytest.mark.parametrize(("tool", "servers", "level"), [
    ("mcp__looper__looper_tasks", {"looper"}, ActionLevel.INSIDE),
    # a server named looper__evil is not Looper's: the name is ambiguous, so it is reviewed
    ("mcp__looper__evil__send", {"looper", "looper__evil"}, ActionLevel.UNKNOWN),
    ("mcp__looper__not_a_looper_tool", {"looper"}, ActionLevel.UNKNOWN),
    ("mcp__search__query", set(), ActionLevel.UNKNOWN),  # a server this run did not load
])
def test_an_mcp_tool_is_placed_only_by_a_server_the_run_loaded(tool: str, servers: set[str],
                                                               level: ActionLevel) -> None:
    trusted = outbound_hook.Trusted(frozenset(servers), frozenset({"looper_tasks"}))
    assert asyncio.run(outbound_hook.resolve_level(tool, trusted)) is level


def test_a_hook_whose_input_never_ends_denies_at_its_deadline(tmp_path: Path) -> None:
    import time

    command = _hook(tmp_path, "http://127.0.0.1:9")
    config = tmp_path / "rt" / ".local" / "outbound-hook" / "manager.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "deadline": 3}))
    started = time.monotonic()
    process = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        out = process.stdout.read()  # its stdin stays open: the client waits for input that never ends
    finally:
        process.stdin.close()
        process.wait(timeout=20)
    assert _decision(json.loads(out)) == DENY and time.monotonic() - started < 15
