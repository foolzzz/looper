"""Pi's four roles through Launch, the packaged TS bridge and the agent HTTP API.

The provider emits tool calls from the actual advertised names/schemas, never
tool results. Only persistence is stubbed: authorization, confirmation, tool
implementations and vault reads run normally. All files/listeners are disposable.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.http_server import AgentHTTPServer, HTTP_200, _json_response
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.ops import Turn
from looper.agents.ops.planner import PlannerOps, TOOLS as PLANNER_TOOLS
from looper.agents.ops.room import RoomOps, tools_for
from looper.agents.ops.service import MANAGER_TOOLS, ManagerOps, Platform, library_with_server, server_config
from looper.agents.ops.vault import TOOLS as VAULT_TOOLS
from looper.agents.profile_registry import LegacyProviderSpec as ProviderSpec, RuntimeSpec, servable_roles
from tests.runtime_model_helpers import resolved_profile
from looper.agents.room import MemberKind, RoomService, Seat
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability, RuntimeUnsupported
from looper.agents.runtimes.pi import PROBE_HELP_TOKENS, PiRuntime
from looper.agents.vault import VaultConfig

PI = PiRuntime()
ROLES = ["manager", "planner", "worker", "reviewer"]
CAPABILITIES = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME, Capability.STEER,
                          Capability.READ_TOOLS, Capability.COMPACTION_EVENTS, Capability.MCP})
PREFIX = "mcp__looper__"

pytestmark = pytest.mark.usefixtures("current_turn")


def _launch(role: str, library: AgentLibrary = AgentLibrary(), *, endpoint: str = "http://127.0.0.1:1/v1",
            consult: bool = False) -> Launch:
    provider = ProviderSpec("prov-pi-test", "test", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_CHAT}),
                            base_url=endpoint, api_key="sk-disposable-provider")
    profile = resolved_profile("agent-pi-test", "test", RuntimeSpec("rt-pi", "pi", "pi", capabilities=CAPABILITIES),
                           provider, "selected", "")
    return Launch(AgentSettings(profile=profile), library=library, role=AgentType(role), consult=consult)


def test_pi_advertises_only_the_rfc_capabilities_and_all_four_roles() -> None:
    assert PI.capabilities == CAPABILITIES
    assert servable_roles(PI.capabilities) == ROLES
    # The remaining RuntimeUnsupported cases validate input/version/session
    # boundaries; none of these execution entry points is a rollout placeholder.
    for method in ("run_command", "resident_session", "consult_command", "steer_command", "run_channel",
                   "write_turn", "parse"):
        assert method in type(PI).__dict__
    assert PI.resident_session("platform-tools").__class__.__name__ == "ResidentPi"


@pytest.mark.parametrize("version", ["1.3.0", "1.10.0", "2.0.0"])
def test_supported_probe_enables_the_final_capabilities(tmp_path: Path, version: str) -> None:
    executable = fake_claude(tmp_path / "pi", "import sys\n"
                             f"print({version!r} if sys.argv[1] == '--version' else {' '.join(PROBE_HELP_TOKENS)!r})\n")
    probe = PI.probe(executable)
    assert probe.error is None and probe.capabilities == CAPABILITIES


@pytest.mark.installed
def test_installed_pi_probes_in_private_directories_without_a_model() -> None:
    executable = shutil.which("pi")
    if executable is None:
        pytest.skip("Pi is not installed; its real probe was not run")
    probe = PI.probe(executable)
    assert probe.error is None, probe.error
    assert probe.capabilities == CAPABILITIES


@pytest.mark.parametrize(("version", "help_text"), [
    ("1.2.9", " ".join(PROBE_HELP_TOKENS)), ("unknown", " ".join(PROBE_HELP_TOKENS)),
    ("1.3.0", "--mode json"),
])
def test_failed_probe_never_enables_capabilities(tmp_path: Path, version: str, help_text: str) -> None:
    executable = fake_claude(tmp_path / "pi", "import sys\n"
                             f"print({version!r} if sys.argv[1] == '--version' else {help_text!r})\n")
    probe = PI.probe(executable)
    assert probe.error and not probe.capabilities


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("consult", [False, True])
def test_read_tools_keep_the_bridge_and_catalog_mcp_is_recorded_as_skipped(
    tmp_path: Path, role: str, consult: bool,
) -> None:
    async def check() -> None:
        catalog = tmp_path / "catalog.json"
        catalog.write_text(json.dumps({"mcpServers": {"generic": {"command": "must-not-start"}}}))
        item = LoadedItem(ItemKind.MCP, "generic", "test-sha", plugin="", scope="global")
        library = await library_with_server(AgentLibrary(items=(item,), mcp_config=str(catalog)), tmp_path,
                                            server_config("http://127.0.0.1:1", "scoped-test-token"), who=role)
        launch = _launch(role, library, consult=consult)
        assert launch.environment()["LOOPER_AGENT_TOKEN"] == "scoped-test-token"
        assert not launch.env  # binding must come from the materialized library
        args = launch.args()
        assert "-e" in args and Path(args[args.index("-e") + 1]).is_file()
        if role == "planner" or consult:
            assert args[args.index("--tools") + 1] == "read,grep,find,ls,mcp__looper__*"
        else:
            assert "--tools" not in args
        skipped = PI.launch_record(launch)["runtime_catalog"]["skipped"]
        assert len(skipped) == 1 and "MCP server generic:" in skipped[0]
        assert "must-not-start" not in json.dumps(args)

    asyncio.run(check())


@pytest.mark.parametrize("missing", ["LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN"])
def test_incomplete_bridge_binding_is_refused_before_launch(tmp_path: Path, missing: str) -> None:
    async def check() -> None:
        config = server_config("http://127.0.0.1:1", "test-token")
        del config["env"][missing]
        library = await library_with_server(AgentLibrary(), tmp_path, config, who="manager")
        with pytest.raises(RuntimeUnsupported, match="LOOPER_AGENT"):
            _launch("manager", library).environment()

    asyncio.run(check())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class _Server(AgentHTTPServer):
    """Observe real route answers; optionally corrupt discovery for negative tests."""

    listing_filter: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    after_listing: Callable[[], None] | None = None

    async def _handle_agent_api(self, method: str, path: str, headers: dict[str, str], body: bytes,
                                writer: Any) -> None:
        buffered = _Writer()
        await super()._handle_agent_api(method, path, headers, body, buffered)
        head, _, raw = buffered.data.partition(b"\r\n\r\n")
        answer = json.loads(raw)
        self.answers.append((method, path, int(head.split()[1]), answer))
        if method == "GET" and "tools" in answer:
            if self.listing_filter:
                answer["tools"] = self.listing_filter(answer["tools"])
            if self.after_listing:
                self.after_listing()
            writer.write(_json_response(HTTP_200, answer))
        else:
            writer.write(buffered.data)
        await writer.drain()


@pytest.fixture
def platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Storage boundaries only; production catalogs, permission checks and reads."""
    from looper.db import agent_setting_layers, manager_actions, managers, projects, schedules, system_settings, tasks
    from looper.agents import ops

    project = {"id": "proj-test", "name": "disposable", "description": "", "work_dir": str(tmp_path),
               "background": "", "created_at": None, "updated_at": None, "task_count": 0, "schedule_count": 0,
               "vault": "on", "agent_room": "on", "planner_roles": "off"}
    task = {"id": "task-test", "project_id": project["id"], "status": "running"}
    monkeypatch.setattr(tasks, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(projects, "get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(projects, "find_project_by_name", AsyncMock(return_value=project))
    monkeypatch.setattr(projects, "list_projects", AsyncMock(return_value=[project]))
    deleted = AsyncMock()
    monkeypatch.setattr(projects, "delete_project", deleted)
    monkeypatch.setattr(schedules, "list_schedules", AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks, "project_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(managers, "template_using_project", AsyncMock(return_value=None))
    monkeypatch.setattr(manager_actions, "record", AsyncMock())
    monkeypatch.setattr(agent_setting_layers, "list_layers", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_setting_layers, "planner_profiles", AsyncMock(return_value=[]))
    monkeypatch.setattr(ops.planner, "choosable_profiles", AsyncMock(return_value=([], False)))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("disposable vault evidence\n")
    config = VaultConfig(vault.resolve(), (), None)
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={
        "knowledge_vault": json.dumps({"path": str(config.path), "writable": [], "rules_file": None}),
    }))
    room = RoomService()
    manager = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1")
    planner = PlannerOps(manager.platform, url="http://127.0.0.1:1")
    planner.room = room
    runs = RoomOps(room, url="http://127.0.0.1:1")
    tokens = {"manager": manager.server_config()["env"]["LOOPER_AGENT_TOKEN"],
              "planner": planner.issue(task["id"])}
    for role, kind in (("worker", MemberKind.WORK), ("reviewer", MemberKind.REVIEW)):
        tokens[role] = runs.issue(task["id"], Seat(f"{role}@s1", role, kind, run=role), vault=config)
    server = _Server(port=0, web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
                     supervisor=SimpleNamespace(), runtime_root=tmp_path, manager_ops=manager,
                     planner_ops=planner, room_ops=runs)
    server.answers = []
    return SimpleNamespace(server=server, manager=manager, planner=planner, runs=runs, tokens=tokens,
                           config=config, deleted=deleted, root=tmp_path)


@pytest.mark.parametrize("role", ROLES)
def test_isolated_agent_route_fixture_has_a_successful_scoped_read(platform: SimpleNamespace, role: str) -> None:
    """Positive control for the real route and storage fixture, even without sockets."""
    async def check() -> None:
        headers = {"authorization": f"Bearer {platform.tokens[role]}"}
        writer = _Writer()
        await platform.server._handle_agent_api("GET", "/api/agent/v1/tools", headers, b"", writer)
        listing = json.loads(writer.data.partition(b"\r\n\r\n")[2])["tools"]
        name, args = (("looper_projects", {"action": "list"}) if role == "manager" else
                      ("looper_task_profiles", {}) if role == "planner" else
                      ("looper_vault_read", {"path": "note.md"}))
        assert name in {tool["name"] for tool in listing}
        writer = _Writer()
        await platform.server._handle_agent_api("POST", f"/api/agent/v1/ops/{name}", headers,
                                                json.dumps(args).encode(), writer)
        assert json.loads(writer.data.partition(b"\r\n\r\n")[2])["status"] == "ok"

    asyncio.run(check())


@asynccontextmanager
async def _running(platform: SimpleNamespace):
    try:
        await platform.server.start()
    except PermissionError:
        pytest.skip("sandbox forbids local HTTP listeners; real Pi platform path was not run")
    try:
        yield f"http://127.0.0.1:{platform.server._port}"
    finally:
        await platform.server.stop()


@pytest.fixture
def provider() -> Iterator[SimpleNamespace]:
    """A non-model Chat Completions provider; results always come back from Pi."""
    if shutil.which("pi") is None or shutil.which("node") is None:
        pytest.skip("installed Pi and Node required for the real TS bridge")
    state = SimpleNamespace(requests=[], calls=[], errors=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            try:
                if state.calls:
                    name, arguments = state.calls.pop(0)
                    available = {tool["function"]["name"]: tool["function"] for tool in body.get("tools", [])}
                    tool = available[name]  # use exactly the actual registered name/schema
                    assert tool["parameters"]["type"] == "object"
                    args = arguments(body) if callable(arguments) else arguments
                    delta = {"tool_calls": [{"index": 0, "id": f"call-{len(state.requests)}", "type": "function",
                                              "function": {"name": tool["name"], "arguments": json.dumps(args)}}]}
                    reason = "tool_calls"
                else:
                    delta, reason = {"content": "finished"}, "stop"
            except Exception as exc:
                state.errors.append(str(exc))
                delta, reason = {"content": "provider assertion failed"}, "stop"
            chunks = [{"id": "completion-test", "object": "chat.completion.chunk", "created": 0,
                       "model": "selected", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "completion-test", "object": "chat.completion.chunk", "created": 0,
                       "model": "selected", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}]
            data = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("sandbox forbids local HTTP listeners; fake provider was not run")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _run_pi(platform: SimpleNamespace, provider: SimpleNamespace, role: str, url: str, *,
                  token: str | None = None, consult: bool = False) -> subprocess.CompletedProcess[str]:
    library = await library_with_server(AgentLibrary(), platform.root,
                                        server_config(url, token or platform.tokens[role]), who=role)
    launch = _launch(role, library, endpoint=provider.endpoint, consult=consult)
    prompt = platform.root / ".local" / "prompts" / "test.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Use the requested platform tool.")
    executable = shutil.which("pi")
    assert executable
    command = PI.run_command(executable, cwd=platform.root, prompt_file=prompt, launch=launch)
    env = {**launch.environment(), "TMPDIR": str(platform.root), "NO_PROXY": "*", "no_proxy": "*"}
    done = await asyncio.to_thread(subprocess.run, command, input="Read platform state.", text=True,
                                   capture_output=True, env=env, cwd=platform.root, timeout=120, check=False)
    assert not provider.errors, provider.errors
    assert platform.tokens[role] not in done.stdout + done.stderr
    return done


def _tool_messages(provider: SimpleNamespace) -> list[dict[str, Any]]:
    assert provider.requests, "the real Pi process never called the fake provider"
    return [message for message in provider.requests[-1]["messages"] if message["role"] == "tool"]


def _pi_tool_results(done: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    return [event["message"] for event in events if event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "toolResult"]


@pytest.mark.installed
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("consult", [False, True])
def test_each_role_discovers_only_its_tools_and_reads_the_real_service(
    platform: SimpleNamespace, provider: SimpleNamespace, role: str, consult: bool,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            name, args = (("looper_projects", {"action": "list"}) if role == "manager" else
                          ("looper_task_profiles", {}) if role == "planner" else
                          ("looper_vault_read", {"path": "note.md"}))
            provider.calls = [(PREFIX + name, args)]
            done = await _run_pi(platform, provider, role, url, consult=consult)
            assert done.returncode == 0, done.stderr
            listing = next(answer["tools"] for method, _, status, answer in platform.server.answers
                           if method == "GET" and status == 200)
            advertised = {tool["function"]["name"]: tool["function"] for tool in provider.requests[0]["tools"]}
            expected = {PREFIX + tool["name"] for tool in listing}
            assert {name for name in advertised if name.startswith(PREFIX)} == expected
            for tool in listing:
                registered = advertised[PREFIX + tool["name"]]
                assert registered["description"] == tool["description"]
                assert registered["parameters"] == tool["inputSchema"]
            builtins = set(advertised) - expected
            assert builtins == ({"read", "grep", "find", "ls"} if role == "planner" or consult else
                                {"read", "bash", "edit", "write"})
            if role == "manager":
                assert expected == {PREFIX + tool.name for tool in MANAGER_TOOLS}
                assert all("vault" not in name and "room" not in name for name in expected)
            else:
                kind = MemberKind.PLANNER if role == "planner" else (
                    MemberKind.WORK if role == "worker" else MemberKind.REVIEW)
                wanted = {PREFIX + tool.name for tool in (*tools_for(kind), *VAULT_TOOLS[:2])}
                if role == "planner":
                    wanted |= {PREFIX + tool.name for tool in PLANNER_TOOLS}
                assert expected == wanted
            answer = json.loads(_tool_messages(provider)[0]["content"])
            assert answer["status"] == "ok"
            results = _pi_tool_results(done)
            assert len(results) == 1 and results[0]["isError"] is False
            if role in ("worker", "reviewer"):
                assert answer["result"]["content"] == "disposable vault evidence\n"
            elif role == "manager":
                assert answer["result"]["projects"][0]["project"] == "disposable"
            else:
                assert set(answer["result"]["roles"]) == {"worker", "reviewer"}
            assert any(method == "POST" and path.endswith("/" + name) and status == 200
                       for method, path, status, _ in platform.server.answers)

    asyncio.run(check())


@pytest.mark.installed
@pytest.mark.parametrize("role", ROLES)
def test_wrong_token_stops_initialization_before_any_provider_request(
    platform: SimpleNamespace, provider: SimpleNamespace, role: str,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            wrong = "planner.invalid" if role == "planner" else "run.invalid" if role in (
                "worker", "reviewer") else "invalid"
            done = await _run_pi(platform, provider, role, url, token=wrong)
            assert done.returncode != 0
            assert not provider.requests
            assert platform.server.answers[-1][2] == 401

    asyncio.run(check())


@pytest.mark.installed
@pytest.mark.parametrize("role", ROLES)
def test_the_bridge_cannot_grant_an_operation_the_token_does_not_have(
    platform: SimpleNamespace, provider: SimpleNamespace, role: str,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            # Advertise a real tool from another role, then let the real API
            # reject it. A compromised catalog cannot turn a token into another.
            foreign = VAULT_TOOLS[1] if role == "manager" else next(
                tool for tool in MANAGER_TOOLS if tool.name == "looper_projects")
            platform.server.listing_filter = lambda tools: [*tools, foreign.listing()]
            provider.calls = [(PREFIX + foreign.name, {"path": "note.md"} if role == "manager" else
                               {"action": "delete", "project": "disposable"})]
            done = await _run_pi(platform, provider, role, url)
            assert done.returncode == 0, done.stderr
            answer = json.loads(_tool_messages(provider)[0]["content"])
            assert answer["status"] == "error"
            assert answer["error"]["code"] in ("invalid", "forbidden")
            assert _pi_tool_results(done)[0]["isError"] is True
            platform.deleted.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.installed
@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
def test_revoked_token_is_rechecked_when_a_discovered_tool_runs(
    platform: SimpleNamespace, provider: SimpleNamespace, role: str,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            platform.server.after_listing = (lambda: platform.planner.revoke(platform.tokens[role])) if (
                role == "planner") else lambda: platform.runs.revoke_run(role)
            provider.calls = [(PREFIX + "looper_vault_read", {"path": "note.md"})]
            done = await _run_pi(platform, provider, role, url)
            assert done.returncode == 0, done.stderr
            assert "HTTP 401" in _tool_messages(provider)[-1]["content"]
            assert platform.server.answers[-1][2] == 401

    asyncio.run(check())


@pytest.mark.installed
def test_a_read_only_vault_grant_cannot_be_upgraded_by_the_bridge(
    platform: SimpleNamespace, provider: SimpleNamespace,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            platform.server.listing_filter = lambda tools: [*tools, VAULT_TOOLS[2].listing()]
            provider.calls = [(PREFIX + "looper_vault_write", {"path": "note.md", "content": "overwritten",
                                                               "reason": "forged permission"})]
            done = await _run_pi(platform, provider, "worker", url)
            assert done.returncode == 0, done.stderr
            answer = json.loads(_tool_messages(provider)[-1]["content"])
            assert answer["error"]["code"] == "forbidden"
            assert (platform.config.path / "note.md").read_text() == "disposable vault evidence\n"

    asyncio.run(check())


@pytest.mark.installed
def test_confirmation_is_a_server_transition_and_cannot_be_bypassed(
    platform: SimpleNamespace, provider: SimpleNamespace,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            provider.calls = [(PREFIX + "looper_projects", {"action": "delete", "project": "disposable"})]
            await _run_pi(platform, provider, "manager", url)
            assert json.loads(_tool_messages(provider)[-1]["content"])["error"]["code"] == "needs_user_turn"
            platform.deleted.assert_not_awaited()
            turn = Turn("web", "test-chat", message_id="request-delete")
            platform.manager.begin_turn(turn)
            provider.requests.clear()
            provider.calls = [(PREFIX + "looper_projects", {"action": "delete", "project": "disposable"})]
            done = await _run_pi(platform, provider, "manager", url)
            pending = json.loads(_tool_messages(provider)[-1]["content"])
            assert pending["status"] == "needs_confirmation"
            assert _pi_tool_results(done)[0]["isError"] is False
            platform.deleted.assert_not_awaited()
            platform.manager.end_turn(turn)
            # Another chat cannot confirm a pending operation.
            other = Turn("web", "other-chat", message_id="forged-confirmation")
            platform.manager.begin_turn(other)
            provider.calls = [(PREFIX + "looper_confirm", {"confirmation_id": pending["confirmation_id"]})]
            await _run_pi(platform, provider, "manager", url)
            assert json.loads(_tool_messages(provider)[-1]["content"])["error"]["code"] == "not_found"
            platform.deleted.assert_not_awaited()
            platform.manager.end_turn(other)
            # Only the next server-authorized user turn in the original chat runs it.
            confirmed = Turn("web", "test-chat", message_id="user-confirmed")
            platform.manager.begin_turn(confirmed)
            provider.calls = [(PREFIX + "looper_confirm", {"confirmation_id": pending["confirmation_id"]})]
            done = await _run_pi(platform, provider, "manager", url)
            assert done.returncode == 0, done.stderr
            assert json.loads(_tool_messages(provider)[-1]["content"])["status"] == "ok"
            platform.deleted.assert_awaited_once_with("proj-test")
            provider.calls = [(PREFIX + "looper_confirm", {"confirmation_id": pending["confirmation_id"]})]
            await _run_pi(platform, provider, "manager", url)
            assert json.loads(_tool_messages(provider)[-1]["content"])["error"]["code"] == "not_found"
            assert platform.deleted.await_count == 1

    asyncio.run(check())


@pytest.mark.installed
def test_confirmation_in_the_requesting_turn_is_refused(
    platform: SimpleNamespace, provider: SimpleNamespace,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            platform.manager.begin_turn(Turn("web", "test-chat", message_id="request-delete"))
            def pending_id(body: dict[str, Any]) -> dict[str, str]:
                result = next(message for message in reversed(body["messages"]) if message["role"] == "tool")
                return {"confirmation_id": json.loads(result["content"])["confirmation_id"]}
            provider.calls = [(PREFIX + "looper_projects", {"action": "delete", "project": "disposable"}),
                              (PREFIX + "looper_confirm", pending_id)]
            done = await _run_pi(platform, provider, "manager", url)
            assert done.returncode == 0, done.stderr
            messages = _tool_messages(provider)
            assert json.loads(messages[0]["content"])["status"] == "needs_confirmation"
            assert json.loads(messages[1]["content"])["error"]["code"] == "expired"
            platform.deleted.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.installed
def test_illegal_schema_prevents_initialization_and_provider_requests(
    platform: SimpleNamespace, provider: SimpleNamespace,
) -> None:
    async def check() -> None:
        async with _running(platform) as url:
            def corrupt(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [*tools, {**tools[0], "name": "invalid_schema", "inputSchema": {"type": "bogus"}}]
            platform.server.listing_filter = corrupt
            done = await _run_pi(platform, provider, "manager", url)
            assert done.returncode != 0
            assert "illegal inputSchema" in done.stderr
            assert not provider.requests
            assert all(method != "POST" for method, _, _, _ in platform.server.answers)

    asyncio.run(check())


def test_missing_packaged_bridge_is_refused_before_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("looper.agents.runtimes.pi.files", lambda _: tmp_path)
    with pytest.raises(RuntimeUnsupported, match="packaged Looper tool bridge is missing"):
        _launch("manager").args()
