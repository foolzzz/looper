"""Pi's TS bridge names the turn its call belongs to (``X-Looper-Turn``), read from the binding file at each call.

Unlike ``test_pi_platform_tools``, nothing stands in for the bridge's header here: the call reaches ``ManagerOps``
with whatever the real Pi process sent.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from looper.agents import chat_settings
from looper.agents.library import AgentLibrary
from looper.agents.ops import Turn
from looper.agents.ops.service import TURN_FILE_ENV, URL_ENV, library_with_server
from tests import test_pi_platform_tools as pi_tools
from tests.test_pi_platform_tools import PI, PREFIX, _launch, _running, _tool_messages

platform, provider = pi_tools.platform, pi_tools.provider  # the real-Pi fixtures


async def _run_manager_pi(platform: SimpleNamespace, provider: SimpleNamespace, url: str
                          ) -> subprocess.CompletedProcess[str]:
    """Pi as the Manager, with the MCP entry the Manager's ops give it (the binding file's variable included)."""
    config = platform.manager.server_config()
    config["env"][URL_ENV] = url
    library = await library_with_server(AgentLibrary(), platform.root, config, who="manager")
    launch = _launch("manager", library, endpoint=provider.endpoint)
    prompt = platform.root / ".local" / "prompts" / "test.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Use the requested platform tool.")
    executable = shutil.which("pi")
    assert executable
    command = PI.run_command(executable, cwd=platform.root, prompt_file=prompt, launch=launch)
    env = {**launch.environment(), "TMPDIR": str(platform.root), "NO_PROXY": "*", "no_proxy": "*"}
    assert env[TURN_FILE_ENV] == str(platform.manager._turn_file)
    done = await asyncio.to_thread(subprocess.run, command, input="Do it.", text=True, capture_output=True,
                                   env=env, cwd=platform.root, timeout=25, check=False)
    assert not provider.errors, provider.errors
    return done


@pytest.mark.parametrize("enforce", [False, True])
def test_a_user_turns_write_call_through_pi_carries_its_turn(
    platform: SimpleNamespace, provider: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, enforce: bool,
) -> None:
    monkeypatch.setattr(chat_settings, "stop_enabled", lambda: enforce)
    platform.manager._turn_file = platform.root / ".local" / "manager-turn.json"
    assert TURN_FILE_ENV in platform.manager.server_config()["env"]
    seen: list[dict[str, str]] = []
    handle = platform.server._handle_agent_api

    async def recording(method, path, headers, body, writer):  # type: ignore[no-untyped-def]
        if method == "POST":
            seen.append(dict(headers))
        await handle(method, path, headers, body, writer)

    platform.server._handle_agent_api = recording

    async def check() -> None:
        async with _running(platform) as url:
            turn = Turn("web", "test-chat", message_id="request-delete")
            platform.manager.begin_turn(turn, binding="pi-turn-binding")
            platform.manager.publish_binding("pi-turn-binding")
            provider.calls = [(PREFIX + "looper_projects", {"action": "delete", "project": "disposable"})]
            done = await _run_manager_pi(platform, provider, url)
            assert done.returncode == 0, done.stderr
            answer = json.loads(_tool_messages(provider)[-1]["content"])
            assert answer["status"] == "needs_confirmation", answer
            assert seen and seen[-1].get("x-looper-turn") == "pi-turn-binding"
            assert seen[-1].get("x-looper-call")  # the runtime's own call id
            platform.deleted.assert_not_awaited()

    asyncio.run(check())
