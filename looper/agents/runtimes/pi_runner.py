"""Every Pi role uses the SDK runner (one runner, 32 sessions) unless the ops file sets cli."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ... import child_env
from ..agent_settings import AgentType
from ..system_prompt_mode import SystemPromptMode
from . import ADAPTERS, PromptBlock, RuntimeUnsupported, instructions, runtime_prompt_text
from .pi import (
    API_NAMES, KEY_ENV, PiRuntime, _isolated_env, _private_dir, _write_private,
    _session_file, provider_api, runtime_notes,
)
from .channel import Fork
from .runner.channel import RunnerChannel
from .runner import pi_command
from .runner.session import Descriptor, RunnerSession
from .runner.supervisor import RunnerSupervisor

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from .api.loop import TurnTools
    from .runner.turn import TurnLimits

logger = logging.getLogger(__name__)
_RESIDENT_ROLES = (AgentType.MANAGER, AgentType.PLANNER)
_ROLES = (*_RESIDENT_ROLES, AgentType.WORKER, AgentType.REVIEWER)
RUN_MAX_REQUESTS = 1000
RUN_TOOL_TIMEOUT_SEC = 86400
_NO_CLI = ('the Pi runner hosts this run; there is no Pi CLI command under [runtimes] pi = "runner"')
_PROVIDER_ENV = ("the Pi runner cannot apply a provider's environment variables; clear them or use "
                 'the CLI path ([runtimes] pi = "cli")')
# Every Pi role shares this runner. 32 covers a few concurrent tasks; an offline
# spike ran 50 sessions at about 147 MiB. Past this, acquisition still fails with CAPACITY.
SESSION_CAP = 32
_PI_PACKAGE = "@earendil-works/pi-coding-agent"
_PI_VERSION = re.compile(r"^1\.3\.\d+$")


def _prompt_text(looper_prompt: str, launch: Launch, *, cwd: str | Path | None = None) -> str:
    project = (instructions.injected_section(launch.runtime.instruction_files(cwd), launch.runtime.label)
               if cwd is not None else "")
    notes = runtime_notes().rsplit("\n", 1)[0] + (
        "\n- Looper's platform tools are named looper_* (for example looper_tasks).")
    return runtime_prompt_text(launch, looper_prompt, project, notes, keep_empty=False)


class PiRunnerRuntime(PiRuntime):
    hosts_runs = True
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

    def _check_executable(self, executable: str) -> None:
        resolved = shutil.which(executable)
        if resolved is None or Path(resolved).resolve() != self.pi:
            raise RuntimeUnsupported(f"the Pi runner uses {self.pi}; the launch executable {executable} "
                                     f"resolves to {resolved}; restart with the same pi executable")

    def _runner_prompt(self, prompt_file: Path, launch: Launch, cwd: str | Path) -> Path:
        text = _prompt_text(Path(prompt_file).read_text(encoding="utf-8"), launch, cwd=cwd)
        local = Path(prompt_file).absolute().parent.parent
        directory = local / "pi-runner" / "prompts"
        for path in (local / "pi-runner", directory):
            _private_dir(path)
        target = directory / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"
        _write_private(target, text)
        return target

    def _descriptor(self, launch: Launch, prompt_path: Path, **fields) -> Descriptor:
        provider = launch.profile.provider
        model = launch.settings.resolved_model.model
        protocol = next(protocol for protocol, name in API_NAMES if name == provider_api(provider))
        return Descriptor(
            kind="pi", role=launch.role.value, provider_id=provider.id, protocol=protocol,
            base_url=provider.base_url, model=launch.model, effort=launch.effort,
            context_window=model.context_window, system_prompt_file=str(prompt_path),
            reasoning=model.efforts != (),
            system_prompt_mode="replace" if launch.system_prompt_mode is SystemPromptMode.REPLACE else "append",
            builtin=("read", "grep", "find", "ls") if launch.read_only else ("read", "bash", "edit", "write"),
            add_dirs=(*launch.knowledge.add_dirs, *launch.read_dirs), read_only=launch.read_only,
            **fields,
        )

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        if self.unavailable:
            raise RuntimeUnsupported(self.unavailable)
        if launch.role not in _RESIDENT_ROLES:
            raise RuntimeUnsupported("the Pi runner supports Manager and Planner resident sessions only")
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
            raise RuntimeUnsupported("the Pi runner hosts Worker, Reviewer and consult runs only")
        self._check_executable(executable)
        env = self.environment(launch)
        prompt = self._runner_prompt(prompt_file, launch, cwd)
        state_dir, fork_from = session_dir, None
        if fork is not None:
            source = _session_file(fork.session_dir, fork.session_id) if fork.session_dir is not None else None
            if source is None:
                raise RuntimeUnsupported("pi's saved session has no unique file in the run's directory")
            _private_dir(fork.fork_dir.parent)
            fork.fork_dir.mkdir(mode=0o700)
        try:
            if fork is not None:
                copied = fork.fork_dir / source.name
                shutil.copyfile(source, copied)
                copied.chmod(0o400)
                state_dir, fork_from = fork.fork_dir, fork.session_id
            descriptor = self._descriptor(launch, prompt,
                max_requests=max_turns if max_turns > 0 else RUN_MAX_REQUESTS,
                tool_timeout_s=timeout_s if timeout_s > 0 else RUN_TOOL_TIMEOUT_SEC, fork_from=fork_from)
            return RunnerChannel(
                RunnerSession(f"{launch.role.value}-run", self.supervisor, "pi", owned=False, state_dir=state_dir),
                descriptor, cwd=str(cwd), env=env, tools=tools, provider_id=launch.profile.provider.id,
                model=launch.model, price=launch.price, limits=limits)
        except BaseException:
            if fork is not None:
                shutil.rmtree(fork.fork_dir, ignore_errors=True)
            raise

    def failure_detail(self, result_event: dict[str, Any] | None, *, launch: Launch,
                       command: list[str], pid: int | None = None) -> str | None:
        errors = (result_event or {}).get("errors")
        return "; ".join(errors)[:500] if errors else None

    def resident_session(self, label: str) -> RunnerSession:
        return RunnerSession(label, self.supervisor, "pi")


def _opt_out(cause: str) -> str:
    return (f"the Pi runner cannot start: {cause}; install the supported version, "
            'or set [runtimes] pi = "cli" in the ops file; restart Looper')


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
    """Install the adapter once at serve startup, retaining failures without a CLI fallback."""
    node, pi = shutil.which("node"), shutil.which("pi")
    if not node or not pi:
        unavailable = _opt_out("node 22.19+ and pi are not both on PATH")
    else:
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=2, check=True)
            version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if version is None or tuple(map(int, version.groups())) < (22, 19, 0):
                unavailable = _opt_out(f"node {result.stdout.strip() or 'unknown'} is older than 22.19")
            else:
                unavailable = None
        except (OSError, subprocess.SubprocessError):
            unavailable = _opt_out("node --version did not finish within 2 seconds")
        if unavailable is None:
            found = _package_version(pi, _PI_PACKAGE)
            if found is not None and _PI_VERSION.fullmatch(found) is None:
                unavailable = _opt_out(f"{_PI_PACKAGE} 1.3.x is required (found {found or 'not inside ' + _PI_PACKAGE})")
    if unavailable:
        ADAPTERS["pi"] = PiRunnerRuntime(None, pi=None, unavailable=unavailable)
        return None
    resolved = Path(pi).resolve()
    supervisor = RunnerSupervisor(runtime_root, {"pi": pi_command.command(node, pi)},
                                  env={"pi": pi_command.ENV}, session_cap=SESSION_CAP,
                                  startup_error=_opt_out, connect=pi_command.connect)
    ADAPTERS["pi"] = PiRunnerRuntime(supervisor, pi=resolved)
    logger.info("Pi runner enabled for every Pi role (one runner, %s sessions)", SESSION_CAP)
    return supervisor
