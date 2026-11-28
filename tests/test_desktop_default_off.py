"""With the desktop capability left off, nothing a default installation serves changes.

The listener is not started by the service. Prompts, tool names, launch
arguments, the OpenAPI document, and task operation JSON gain no desktop
surface. The frozen prompt and tool catalog is the existing baseline.
"""

import json
import uuid
from pathlib import Path

from looper.agents import agent_settings, api_v1
from looper.agents.browser.desktop.client import DesktopClient
from looper.agents.browser.desktop.fake_driver import FakeDriver
from looper.agents.browser.desktop.transport import DisconnectLog, Endpoint
from looper.agents.ops import clients, planner, room, service
from looper.agents.task_manager_sandbox import launch_args
from looper.agents.tiers import manager, planner as planner_tier, worker
from tests.test_intents_default_off import assert_default_prompts_and_tools

_SURFACES = (
    "looper/agents/serve.py",
    "looper/agents/system_prompt.py",
    "looper/agents/ops/service.py",
    "looper/agents/api_v1.py",
    "looper/agents/mcp_server.py",
    "looper/agents/tiers/manager.py",
    "looper/agents/tiers/worker.py",
    "looper/agents/tiers/planner.py",
)


def _socket() -> Path:
    path = Path("/tmp") / f"looper-desktop-{uuid.uuid4().hex[:8]}.sock"
    if path.parent != Path("/tmp") or not path.name.startswith("looper-desktop-") or not path.name.endswith(".sock"):
        raise RuntimeError(path)
    return path


def _unlink(path: Path) -> None:
    if path.parent != Path("/tmp") or not path.name.startswith("looper-desktop-") or not path.name.endswith(".sock"):
        raise RuntimeError(path)
    path.unlink(missing_ok=True)


def test_the_default_listener_is_off_and_creates_no_socket():
    path = _socket()
    endpoint = Endpoint(path, FakeDriver().handle, DisconnectLog())
    try:
        assert endpoint.enabled is False
        endpoint.start()
        assert not path.exists()
        assert DesktopClient(path).open_session().code == "desktop_unavailable"
        assert not path.exists()
    finally:
        endpoint.close()
        _unlink(path)


def test_default_prompts_tools_argv_openapi_and_task_json_omit_desktop():
    assert_default_prompts_and_tools()
    prompts = "\n".join((
        manager.DEFAULT_SYSTEM_PROMPT,
        planner_tier.DEFAULT_SYSTEM_PROMPT,
        worker.DEFAULT_SYSTEM_PROMPT,
        agent_settings.DEFAULT_HARNESS_PROMPT,
    ))
    assert "desktop" not in prompts
    names = [tool.name for group in (
        service.MANAGER_TOOLS, clients.CLIENT_TOOLS, planner.TOOLS, planner.ROLE_TOOLS, room.TOOLS,
    ) for tool in group]
    assert names and all("desktop" not in name for name in names)
    for path, item in api_v1.document()["paths"].items():
        assert "desktop" not in path
        for operation in item.values():
            assert "desktop" not in operation["operationId"]
    task_json = json.dumps([
        {"id": operation.operation_id, "request": operation.request, "result": operation.result}
        for operation in api_v1.operations() if operation.operation_id.startswith("task")
    ], sort_keys=True)
    assert task_json and "desktop" not in task_json
    argv = launch_args()
    assert argv and all("desktop" not in part for part in argv)
    root = Path(__file__).resolve().parents[1]
    for relative in _SURFACES:
        text = (root / relative).read_text()
        assert "browser.desktop" not in text
        assert "agents.browser" not in text
