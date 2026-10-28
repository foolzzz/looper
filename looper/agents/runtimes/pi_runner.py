"""Ops-file opt-in for Pi Manager and Planner sessions on one shared runner."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

from ... import child_env
from ..agent_settings import AgentType
from ..system_prompt_mode import SystemPromptMode
from . import ADAPTERS, PromptBlock, RuntimeUnsupported, instructions, runtime_prompt_text
from .pi import (
    API_NAMES, KEY_ENV, PiRuntime, _isolated_env, _private_dir, _write_private,
    provider_api, runtime_notes,
)
from .runner import pi_command
from .runner.session import Descriptor, RunnerSession
from .runner.supervisor import RunnerSupervisor

if TYPE_CHECKING:
    from ..agent_settings import Launch

logger = logging.getLogger(__name__)
_ROLES = (AgentType.MANAGER, AgentType.PLANNER)
_PROVIDER_ENV = ("the Pi runner cannot apply a provider's environment variables; clear them or use "
                 'the CLI path ([runtimes] pi = "cli")')


def _prompt_text(looper_prompt: str, launch: Launch, *, cwd: str | Path | None = None) -> str:
    project = (instructions.injected_section(launch.runtime.instruction_files(cwd), launch.runtime.label)
               if cwd is not None else "")
    notes = runtime_notes().rsplit("\n", 1)[0] + (
        "\n- Looper's platform tools are named looper_* (for example looper_tasks).")
    return runtime_prompt_text(launch, looper_prompt, project, notes, keep_empty=False)


class PiRunnerRuntime(PiRuntime):
    platform_tools_in_process = True
    can_interrupt = True
    isolation_id = "pi-runner/v1"

    def __init__(self, supervisor: RunnerSupervisor | None, *, pi: Path | None,
                 unavailable: str | None = None):
        self.supervisor, self.pi, self.unavailable = supervisor, pi, unavailable

    def environment(self, launch: Launch) -> dict[str, str]:
        if launch.role not in _ROLES:
            return super().environment(launch)
        if launch.profile.provider.env:
            raise RuntimeUnsupported(_PROVIDER_ENV)
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return {**_isolated_env({**child_env.inherited(), **dict(launch.env)}),
                KEY_ENV: dict(binding.env)[KEY_ENV]}

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        if launch.role not in _ROLES:
            return super().prompt_blocks(launch, looper_prompt, cwd=cwd)
        block = PromptBlock("Looper instructions", "looper", _prompt_text(looper_prompt, launch, cwd=cwd),
                            "system prompt file")
        return ([block] if launch.system_prompt_mode is SystemPromptMode.REPLACE else
                [PromptBlock("Default prompt", "runtime", None, "pi's default prompt (not shown)"), block])

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        if self.unavailable:
            raise RuntimeUnsupported(self.unavailable)
        if launch.role not in _ROLES:
            raise RuntimeUnsupported("the Pi runner supports Manager and Planner resident sessions only")
        resolved = shutil.which(executable)
        if resolved is None or Path(resolved).resolve() != self.pi:
            raise RuntimeUnsupported(f"the Pi runner uses {self.pi}; the launch executable {executable} "
                                     f"resolves to {resolved}; restart with the same pi executable")
        self.environment(launch)  # Provider binding and process-level environment checks precede writes.
        text = _prompt_text(Path(prompt_file).read_text(encoding="utf-8"), launch, cwd=cwd)
        local = Path(prompt_file).absolute().parent.parent
        directory = local / "pi-runner" / "prompts"
        for path in (local / "pi-runner", directory):
            _private_dir(path)
        target = directory / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"
        _write_private(target, text)
        provider = launch.profile.provider
        model = launch.settings.resolved_model.model
        protocol = next(protocol for protocol, name in API_NAMES if name == provider_api(provider))
        return Descriptor(
            kind="pi", role=launch.role.value, provider_id=provider.id, protocol=protocol,
            base_url=provider.base_url, model=launch.model, effort=launch.effort,
            context_window=model.context_window, system_prompt_file=str(target),
            reasoning=model.efforts != (),
            system_prompt_mode="replace" if launch.system_prompt_mode is SystemPromptMode.REPLACE else "append",
            builtin=("read", "grep", "find", "ls") if launch.read_only else ("read", "bash", "edit", "write"),
            add_dirs=(*launch.knowledge.add_dirs, *launch.read_dirs), read_only=launch.read_only,
        ).to_argv()

    def resident_session(self, label: str) -> RunnerSession:
        return RunnerSession(label, self.supervisor, "pi")


def enable(runtime_root: Path) -> RunnerSupervisor | None:
    """Install the adapter once at serve startup, retaining failures without a CLI fallback."""
    node, pi = shutil.which("node"), shutil.which("pi")
    unavailable = None
    if not node or not pi:
        unavailable = "the Pi runner requires node 22.19+ and pi on PATH; install them and restart Looper"
    else:
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=2, check=True)
            version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if version is None or tuple(map(int, version.groups())) < (22, 19, 0):
                unavailable = "the Pi runner requires node 22.19 or newer; upgrade node and restart Looper"
        except (OSError, subprocess.SubprocessError):
            unavailable = "the Pi runner could not check node --version within 2 seconds; check node and restart Looper"
    if unavailable:
        ADAPTERS["pi"] = PiRunnerRuntime(None, pi=None, unavailable=unavailable)
        return None
    resolved = Path(pi).resolve()
    supervisor = RunnerSupervisor(runtime_root, {"pi": pi_command.command(node, pi)},
                                  env={"pi": pi_command.ENV}, session_cap=10, connect=pi_command.connect)
    ADAPTERS["pi"] = PiRunnerRuntime(supervisor, pi=resolved)
    logger.info("Pi runner enabled for Manager and Planner (one runner, 10 sessions)")
    return supervisor
