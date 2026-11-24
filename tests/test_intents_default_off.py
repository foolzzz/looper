"""Intent replies add no model-facing prompt, tool or launch-signature content.

Each existing prompt section and each existing tool is frozen on its own
(``fixtures/catalog_default_off.json``). A section or tool added since is not
compared. The routing suite separately exercises the absent-setting branch
through a real Manager turn.
"""

import ast
import hashlib
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from looper.agents.profile_registry import builtin_resolved_profile

from looper.agents import agent_settings
from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker
from tests.prompt_sections import assert_sections

_CATALOG = Path(__file__).parent / "fixtures" / "catalog_default_off.json"


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def assert_default_prompts_and_tools() -> None:
    """The four default prompts and the five tool catalogs still have every frozen item, in order."""
    baseline = json.loads(_CATALOG.read_text())
    prompts = {
        "manager": manager.DEFAULT_SYSTEM_PROMPT,
        "planner": planner_tier.DEFAULT_SYSTEM_PROMPT,
        "worker": worker.DEFAULT_SYSTEM_PROMPT,
        "harness": agent_settings.DEFAULT_HARNESS_PROMPT,
    }
    for name, text in prompts.items():
        assert_sections(text, baseline["prompts"][name], label=name)
    catalogs = {
        "manager": service.MANAGER_TOOLS,
        "client": clients.CLIENT_TOOLS,
        "planner": planner.TOOLS,
        "planner_roles": planner.ROLE_TOOLS,
        "room": room.TOOLS,
    }
    for name, tools in catalogs.items():
        frozen = baseline["tools"][name]
        current = {tool.name: _sha({"name": tool.name, "description": tool.description, "schema": tool.schema})
                   for tool in tools}
        assert [tool.name for tool in tools if tool.name in frozen["each"]] == frozen["order"], name
        assert {key: current.get(key) for key in frozen["each"]} == frozen["each"], name


def test_default_prompts_and_tool_list_equal_the_main_baseline():
    assert_default_prompts_and_tools()


# These snapshots were read back from main. Compare the command bytes on the off
# path, allowing a separate routed branch without changing the baseline. The AST
# comparison still pins the inputs that restart the resident process.
_MAIN_COMMAND = """\
def _claude_command(self, prompt_file: Path, launch: Launch, *, resume: bool = True) -> list[str]:
    \"\"\"The resident process's command (``RuntimeAdapter.resident_command``), resuming the saved session.\"\"\"
    session = self._session_now()
    runtime = launch.runtime
    command = runtime.resident_command(launch.executable(self._config.claude_bin), cwd=self._config.work_dir,
                                       prompt_file=prompt_file, launch=launch)
    if resume and session.session_id:
        command.extend(runtime.resume_args(session.session_id))
    return command
"""
_MAIN_SIGNATURE = """\
signature = (
    tuple(await token.step("claude_command", asyncio.to_thread(
        self._claude_command, prompt_file, launch, resume=False))),
    system_prompt.sha256, launch.env_sha256,
    launch.library.sha256, launch.binding_sha256,
    launch.llmrec.url if isinstance(launch.llmrec, LlmrecRoute) else None,
)
if self._has_templates or has_workflows or self.agent_id != DEFAULT_MANAGER_ID:
    listing = json.dumps(tools, sort_keys=True) if self.ops is not None else ""
    signature += (hashlib.sha256(listing.encode("utf-8")).hexdigest(),)
"""


@pytest.mark.parametrize("session_id,resume", [(None, True), ("saved-session", True), ("saved-session", False)])
def test_the_manager_launch_command_and_signature_equal_the_main_baseline(tmp_path, monkeypatch, session_id, resume):
    agent = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    agent._session_now().session_id = session_id
    monkeypatch.setattr(manager.session_routing, "_routing_enabled", False)
    monkeypatch.setattr(agent, "_session_work_dir", lambda: pytest.fail("the off path used routed workspace code"))
    launch = agent_settings.Launch(agent_settings.AgentSettings(profile=builtin_resolved_profile()))
    namespace = {"Path": Path, "Launch": agent_settings.Launch}
    exec(_MAIN_COMMAND, namespace)
    prompt_file = tmp_path / "system.md"
    expected = namespace["_claude_command"](agent, prompt_file, launch, resume=resume)
    assert agent._claude_command(prompt_file, launch, resume=resume) == expected
    preparation = ast.parse(textwrap.dedent(inspect.getsource(manager.ManagerAgent._prepare_turn)))

    def assignments(tree):
        signature = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AugAssign)):
                continue
            targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
            if any(isinstance(part, ast.Name) and part.id == "signature"
                   for target in targets for part in ast.walk(target)):
                signature.append(ast.dump(node, include_attributes=False))
        return signature

    assert assignments(preparation) == assignments(ast.parse(_MAIN_SIGNATURE))


@pytest.mark.parametrize("setting", [None, "false"])
def test_recognition_off_keeps_the_frozen_catalog_and_operator_tools(monkeypatch, setting):
    from looper.agents import leads_model
    from looper.agents.ops import Turn
    from looper.agents.ops import intents_recognize
    from tests.db_support import run_db

    async def test(pool):
        await pool.execute("INSERT INTO system_settings (key,value) VALUES ('intents.enabled','true')")
        if setting is not None:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ($1,$2)",
                               leads_model.INTENTS_ON_REQUEST_KEY, setting)
        ops = service.ManagerOps(service.Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
        listing = await ops.current_listing(Turn(channel="web", chat="c", message_id="m"))
        assert intents_recognize.TOOL_NAME not in {tool["name"] for tool in listing}
        assert not await intents_recognize.enabled()
        assert_default_prompts_and_tools()

    run_db(test, monkeypatch)
