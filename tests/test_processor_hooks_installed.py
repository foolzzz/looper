"""With the real Claude Code (``LOOPER_TEST_CLAUDE``): no hook of a project's settings or of the user's runs in a
processor launch, where both run in an ordinary one. No model is called: the CLI has no login in the test's own
configuration directory, and a UserPromptSubmit hook runs before any request."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents.agent_settings import AgentType, Launch, resolve
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.runtimes import CLAUDE_CODE

pytestmark = [pytest.mark.installed,
              pytest.mark.skipif(not os.environ.get("LOOPER_TEST_CLAUDE"), reason="LOOPER_TEST_CLAUDE is not set")]

TURN = '{"type": "user", "message": {"role": "user", "content": "ping"}}\n'


def _hook(settings: Path, fired: Path) -> None:
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "cat > %s"}]}]}}'
                        % fired)


@pytest.mark.parametrize("processor", [False, True])
def test_a_processor_launch_runs_no_hook_of_a_project_or_the_user(tmp_path: Path, processor: bool) -> None:
    project, config = tmp_path / "project", tmp_path / "claude-config"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    _hook(project / ".claude" / "settings.json", tmp_path / "project-hook")
    _hook(config / "settings.json", tmp_path / "user-hook")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Reply with one word.")
    launch = Launch(replace(resolve(AgentType.PROCESSOR, {}), profile=builtin_resolved_profile()),
                    role=AgentType.PROCESSOR)
    command = CLAUDE_CODE.run_command(os.environ["LOOPER_TEST_CLAUDE"], cwd=project, prompt_file=prompt, launch=launch)
    if not processor:  # the control: the same command loading the user's and the project's settings, hooks on
        at = command.index("--setting-sources")
        command[at + 1] = "user,project"
        command[command.index("--settings") + 1] = "{}"
    subprocess.run(command, cwd=project, input=TURN, capture_output=True, text=True, timeout=120,
                   env={**os.environ, "CLAUDE_CONFIG_DIR": str(config)})
    assert [(tmp_path / name).exists() for name in ("project-hook", "user-hook")] == [not processor] * 2
