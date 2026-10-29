"""Every DSH role uses the SDK runner (one runner, 32 sessions) unless the ops file sets cli."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import hashlib
import json
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
# Every DSH role shares this runner. 32 covers a few concurrent tasks; an offline
# spike ran 50 sessions at about 85 MiB. Past this, acquisition still fails with CAPACITY.
SESSION_CAP = 32
_DSH_PACKAGE = "@deepseek-ai/dsh"
_DSH_VERSION = "0.2.1-rc.2"


def _prompt_text(looper_prompt: str, launch: Launch, *, cwd: str | Path | None = None) -> str:
    project = (instructions.injected_section(launch.runtime.instruction_files(cwd), launch.runtime.label)
               if cwd is not None else "")
    notes = dsh.runtime_notes()
    if not launch.consult:
        notes += "\n- Looper's platform tools are named looper_* (for example looper_tasks)."
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


def _opt_out(cause: str) -> str:
    return (f"the DSH runner cannot start: {cause}; install the supported version, "
            'or set [runtimes] dsh = "cli" in the ops file; restart Looper')


def _package_version(executable: str, package: str) -> str | None:
    """The installed package version, or None when ``executable`` is not a real file.

    A missing file skips this gate (unit-test stand-ins, and a PATH miss already
    reported). A real executable is walked up at most eight parents for that
    package's ``package.json``, the same walk the runner's loader uses. ``""``
    means the file exists and the package was not found.
    """
    path = Path(executable)
    try:
        if not path.is_file():
            return None
        root = path.resolve().parent
    except OSError:
        return ""
    for _depth in range(9):
        try:
            text = (root / "package.json").read_text(encoding="utf-8")
        except OSError:
            text = None
        if text is not None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("name") == package:
                version = data.get("version")
                return version if isinstance(version, str) else ""
        parent = root.parent
        if parent == root:
            break
        root = parent
    return ""


def enable(runtime_root: Path) -> RunnerSupervisor | None:
    """Install the adapter at startup, retaining failures without a CLI fallback."""
    node, dsh = shutil.which("node"), shutil.which("dsh")
    if not node or not dsh:
        unavailable = _opt_out("node 22.19+ and dsh are not both on PATH")
    else:
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=2, check=True)
            version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if version is None or tuple(map(int, version.groups())) < (22, 19, 0):
                unavailable = _opt_out(f"node 22.19+ is required (found {result.stdout.strip() or 'unknown'})")
            else:
                unavailable = None
        except (OSError, subprocess.SubprocessError):
            unavailable = _opt_out(
                "node 22.19+ and dsh could not be checked: node --version did not finish within 2 seconds")
        if unavailable is None:
            found = _package_version(dsh, _DSH_PACKAGE)
            if found is not None and found != _DSH_VERSION:
                unavailable = _opt_out(
                    f"node 22.19+ and {_DSH_PACKAGE} {_DSH_VERSION} are required "
                    f"(found {found or 'not inside ' + _DSH_PACKAGE})")
    if not unavailable:
        try:
            state = dsh_command.state_root(runtime_root)
            supervisor = RunnerSupervisor(runtime_root, {"dsh": dsh_command.command(node, dsh, state)},
                                          session_cap=SESSION_CAP, startup_error=_opt_out, connect=dsh_command.connect)
            resolved = Path(dsh).resolve()
        except (OSError, ValueError) as exc:
            unavailable = _opt_out(f"could not initialize ({exc}); node 22.19+ and dsh are required")
    if unavailable:
        ADAPTERS["dsh"] = DshRunnerRuntime(None, dsh=None, unavailable=unavailable)
        return None
    ADAPTERS["dsh"] = DshRunnerRuntime(supervisor, dsh=resolved)
    logger.info("DSH runner enabled for every DSH role (one runner, %s sessions)", SESSION_CAP)
    return supervisor
