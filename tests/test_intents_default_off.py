"""Intent replies add no model-facing prompt, tool or launch-signature content.

These baselines pin main's prompts, tools, command and signature. The routing
suite separately exercises the absent-setting branch through a real Manager turn.
"""

import ast
import hashlib
import inspect
import json
import textwrap

from looper.agents import agent_settings
from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_default_prompts_and_tool_list_equal_the_main_baseline():
    prompts = [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT,
               worker.DEFAULT_SYSTEM_PROMPT, agent_settings.DEFAULT_HARNESS_PROMPT]
    tools = [(tool.name, tool.description, tool.schema) for tool in (
        *service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS,
        *planner.ROLE_TOOLS, *room.TOOLS,
    )]
    assert _digest(prompts) == "113dcfc6ecb667fd2d45c4ba855505e86ded3941ffafa669091bed02b221ff28"
    assert _digest(tools) == "76a6006374afaae65cb1525742bf74c3c99dc0a7954e441d51844e7668655346"


# Source snapshots use the same AST parser as the implementation on every supported
# Python version. They were read back from main; adding a preparation step cannot
# alter the resident command or the inputs that restart its process.
_MAIN_COMMAND = """\
def _claude_command(self, prompt_file: Path, launch: Launch, *, resume: bool = True) -> list[str]:
    \"\"\"The resident process's command (``RuntimeAdapter.resident_command``), resuming the saved session.\"\"\"
    runtime = launch.runtime
    command = runtime.resident_command(launch.executable(self._config.claude_bin), cwd=self._config.work_dir,
                                       prompt_file=prompt_file, launch=launch)
    if resume and self._session_id:
        command.extend(runtime.resume_args(self._session_id))
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


def test_the_manager_launch_command_and_signature_equal_the_main_baseline():
    command = ast.parse(textwrap.dedent(inspect.getsource(manager.ManagerAgent._claude_command)))
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

    assert ast.dump(command, include_attributes=False) == ast.dump(ast.parse(_MAIN_COMMAND), include_attributes=False)
    assert assignments(preparation) == assignments(ast.parse(_MAIN_SIGNATURE))
