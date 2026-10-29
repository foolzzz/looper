"""Ops-file opt-in for every DSH role (one runner, 20 sessions)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import hashlib
import logging
from pathlib import Path
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

from ... import child_env
from ..agent_settings import AgentType
from ..library import ItemKind
from ..profile_registry import RegistryError
from . import ADAPTERS, ApiProtocol, PromptBlock, RuntimeUnsupported, instructions, runtime_prompt_text
from . import dsh
from .api.adapter import KEY_ENV
from .channel import Fork
from .dsh import DshRuntime
from .runner import dsh_command
from .runner.channel import RunnerChannel
from .runner.session import Descriptor, RunnerSession
from .runner.supervisor import RunnerSupervisor

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from .api.loop import TurnTools
    from .runner.turn import TurnLimits

logger = logging.getLogger(__name__)
_RESIDENT_ROLES = (AgentType.MANAGER, AgentType.PLANNER)
_ROLES = (*_RESIDENT_ROLES, AgentType.WORKER, AgentType.REVIEWER)
READ_ONLY_TOOLS = ("read", "glob", "grep")
RUN_TOOLS = (*READ_ONLY_TOOLS, "write", "edit", "bash")
RUN_MAX_REQUESTS = 1000
RUN_TOOL_TIMEOUT_SEC = 86400
_NO_CLI = ('the DSH runner hosts this run; there is no DSH CLI command under [runtimes] dsh = "runner"')
_PROVIDER_ENV = ("the DSH runner cannot apply a provider's environment variables; clear them or use "
                 'the CLI path ([runtimes] dsh = "cli")')


def _prompt_text(looper_prompt: str, launch: Launch, *, cwd: str | Path | None = None) -> str:
    project = (instructions.injected_section(launch.runtime.instruction_files(cwd), launch.runtime.label)
               if cwd is not None else "")
    notes = dsh.runtime_notes() + "\n- Looper's platform tools are named looper_* (for example looper_tasks)."
    return runtime_prompt_text(launch, looper_prompt, notes, project, keep_empty=False)


class DshRunnerRuntime(DshRuntime):
    hosts_runs = platform_tools_in_process = can_interrupt = True
    isolation_id = "dsh-runner/v1"

    def __init__(self, supervisor: RunnerSupervisor | None, *, dsh: Path | None,
                 unavailable: str | None = None):
        self.supervisor, self.dsh, self.unavailable = supervisor, dsh, unavailable

    def environment(self, launch: Launch) -> dict[str, str]:
        if launch.role not in _ROLES:
            return super().environment(launch)
        try:
            provider = launch.profile.provider
        except RegistryError as exc:
            raise RuntimeUnsupported(str(exc)) from None
        if provider.env:
            raise RuntimeUnsupported(_PROVIDER_ENV)
        if dsh._web_search(launch):
            raise RuntimeUnsupported("the DSH runner has no web search; unset DSH_ALLOW_WEB_SEARCH or use the CLI path")
        dsh.bind_provider(provider, launch.model, launch.effort)
        return {**{k: v for k, v in dsh._external_environment(
                    {**child_env.inherited(), **dict(launch.env)}).items()
                   if not k.startswith(("LOOPER_", "LOOPX_"))}, KEY_ENV: provider.api_key}

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        if launch.role not in _ROLES:
            return super().prompt_blocks(launch, looper_prompt, cwd=cwd)
        return [PromptBlock("Default prompt", "runtime", None, "DSH's default prompt (not shown; append only)"),
                PromptBlock("Looper instructions", "looper", _prompt_text(looper_prompt, launch, cwd=cwd),
                            "literal system prompt section (order 20000)")]

    def _check_executable(self, executable: str) -> None:
        resolved = shutil.which(executable)
        if resolved is None or Path(resolved).resolve() != self.dsh:
            raise RuntimeUnsupported(f"the DSH runner uses {self.dsh}; the launch executable {executable} "
                                     f"resolves to {resolved}; restart with the same dsh executable")

    def _runner_prompt(self, prompt_file: Path, launch: Launch, cwd: str | Path) -> Path:
        text = _prompt_text(Path(prompt_file).read_text(encoding="utf-8"), launch, cwd=cwd)
        local = Path(prompt_file).absolute().parent.parent
        directory = local / "dsh-runner" / "prompts"
        dsh._private_dir(directory, root=local)
        target = directory / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"
        dsh._write_private(target, text)
        return target

    def _descriptor(self, launch: Launch, prompt_path: Path, **fields) -> Descriptor:
        provider = launch.profile.provider
        policy = dsh.validate_provider(provider, launch.model, launch.effort)
        return Descriptor(
            kind="dsh", role=launch.role.value, provider_id=provider.id, protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            base_url=policy.base_url, model=launch.model, effort=policy.effort,
            context_window=launch.settings.resolved_model.model.context_window, system_prompt_file=str(prompt_path),
            reasoning=True, system_prompt_mode="append",
            builtin=READ_ONLY_TOOLS if launch.read_only else RUN_TOOLS,
            add_dirs=(*launch.knowledge.add_dirs, *launch.read_dirs), read_only=launch.read_only, **fields,
        )

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        if self.unavailable:
            raise RuntimeUnsupported(self.unavailable)
        if launch.role not in _RESIDENT_ROLES:
            raise RuntimeUnsupported("the DSH runner supports Manager and Planner resident sessions only")
        self._check_executable(executable)
        self.environment(launch)
        return self._descriptor(launch, self._runner_prompt(prompt_file, launch, cwd)).to_argv()

    def run_command(self, executable: str, **kwargs) -> list[str]:
        raise RuntimeUnsupported(_NO_CLI)

    def steer_command(self, executable: str, **kwargs) -> list[str]:
        raise RuntimeUnsupported(_NO_CLI)

    def consult_command(self, executable: str, **kwargs) -> list[str]:
        raise RuntimeUnsupported(_NO_CLI)

    def hosted_run(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                   max_turns: int, timeout_s: int, session_dir: Path | None, tools: TurnTools | None,
                   limits: Callable[[], Awaitable[TurnLimits]], fork: Fork | None = None) -> RunnerChannel:
        if self.unavailable:
            raise RuntimeUnsupported(self.unavailable)
        if launch.role not in (AgentType.WORKER, AgentType.REVIEWER):
            raise RuntimeUnsupported("the DSH runner hosts Worker, Reviewer and consult runs only")
        self._check_executable(executable)
        env = self.environment(launch)
        prompt = self._runner_prompt(prompt_file, launch, cwd)
        state_dir, fork_from = session_dir, None
        if fork is not None:
            dsh._snapshot_parent(fork.fork_dir.parent)
            fork.fork_dir.mkdir(mode=0o700)
        try:
            if fork is not None:
                # The runner forks its native store by id; never copy or write the source.
                state_dir, fork_from = fork.fork_dir, fork.session_id
            descriptor = self._descriptor(launch, prompt,
                max_requests=max_turns if max_turns > 0 else RUN_MAX_REQUESTS,
                tool_timeout_s=timeout_s if timeout_s > 0 else RUN_TOOL_TIMEOUT_SEC, fork_from=fork_from)
            return RunnerChannel(
                RunnerSession(f"{launch.role.value}-run", self.supervisor, "dsh", owned=False, state_dir=state_dir),
                descriptor, cwd=str(cwd), env=env, tools=tools, provider_id=launch.profile.provider.id,
                model=launch.model, price=launch.price, limits=limits)
        except BaseException:
            if fork is not None:
                shutil.rmtree(fork.fork_dir, ignore_errors=True)
            raise

    def resident_session(self, label: str) -> RunnerSession:
        return RunnerSession(label, self.supervisor, "dsh")

    def resume_args(self, session_id: str) -> list[str]:
        return ["--session", session_id]

    def failure_detail(self, result_event: dict[str, Any] | None, *, launch: Launch,
                       command: list[str], pid: int | None = None) -> str | None:
        errors = (result_event or {}).get("errors")
        return "; ".join(errors)[:500] if errors else None

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        record = {"model": launch.model, "isolation": self.isolation_id, "system_prompt_mode": "append",
                  "web_search": False, "runtime_limits": {"max_requests": "checked before every model request"}}
        skipped = [f"{item.kind.value} {item.name}: the DSH runner loads no catalog skills, plugins or MCP servers"
                   for item in launch.library.items if not (item.kind is ItemKind.MCP and item.name == "looper")]
        if skipped:
            record["runtime_catalog"] = {"runtime": "dsh", "skipped": skipped}
        return record


def enable(runtime_root: Path) -> RunnerSupervisor | None:
    """Install the adapter at startup, retaining failures without a CLI fallback."""
    node, dsh = shutil.which("node"), shutil.which("dsh")
    requirement = "the DSH runner requires node 22.19+ and dsh on PATH"
    unavailable = None
    if not node or not dsh:
        unavailable = requirement + "; install them and restart Looper"
    else:
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=2, check=True)
            version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if version is None or tuple(map(int, version.groups())) < (22, 19, 0):
                unavailable = requirement + "; upgrade node and restart Looper"
        except (OSError, subprocess.SubprocessError):
            unavailable = requirement + "; could not check node --version within 2 seconds; check node and restart Looper"
    if not unavailable:
        try:
            state = dsh_command.state_root(runtime_root)
            supervisor = RunnerSupervisor(runtime_root, {"dsh": dsh_command.command(node, dsh, state)},
                                          session_cap=20, connect=dsh_command.connect)
            resolved = Path(dsh).resolve()
        except (OSError, ValueError) as exc:
            unavailable = f"the DSH runner could not initialize: {exc}; check the installation and runtime root and restart Looper"
    if unavailable:
        ADAPTERS["dsh"] = DshRunnerRuntime(None, dsh=None, unavailable=unavailable)
        return None
    ADAPTERS["dsh"] = DshRunnerRuntime(supervisor, dsh=resolved)
    logger.info("DSH runner enabled for every DSH role (one runner, 20 sessions)")
    return supervisor
