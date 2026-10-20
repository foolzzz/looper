"""The pi runtime's identity, provider binding, private homes and safe probe.

pi is a fifth runtime type (the pi runtime RFC, sections 1, 3 and 6). Registering
it discovers ``rt-pi`` when ``pi`` is on PATH. Launch commands use private,
content-addressed homes and separate session directories. Sessions, streams
and the tool bridge are not implemented yet, so capabilities stay empty.
There is no pi login and no schema change.

A probe runs only ``--version`` and ``--help``, in a temporary home, agent
directory and session directory, offline, with a 10 second deadline. 1.3.0 is
the oldest version that passes. An older version, a version that cannot be
read, or help that lacks a flag the launch needs is reported and gives the
runtime nothing it can do. Capabilities stay empty either way until the
launch, sessions, stream and Looper tool bridge exist. The later ``mcp``
capability is that bridge only, not a general MCP client.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

from ... import child_env
from ..system_prompt_mode import SystemPromptMode
from . import (
    ApiProtocol, AuthKind, Capability, Probe, PromptBlock, ProviderBinding, RunCost, RuntimeAdapter,
    RuntimeUnsupported,
)
from .instructions import InstructionFile

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..profile_registry import ProviderSpec
    from ..run_usage import Usage
    from . import ResidentSession

_PROBE_TIMEOUT_SEC = 10.0
_VERSION_MAX = 200
PROVIDER_ID = "looper"
KEY_ENV = "LOOPER_PROVIDER_API_KEY"
# Preference is independent of provider names and protocol declaration order.
API_NAMES: tuple[tuple[ApiProtocol, str], ...] = (
    (ApiProtocol.OPENAI_RESPONSES, "openai-responses"),
    (ApiProtocol.OPENAI_CHAT, "openai-completions"),
)
# pi 1.3's CLI accepts these; the selected model may clamp the actual level.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# The oldest pi whose --help has the flags a later launch uses.
MIN_VERSION = (1, 3, 0)
# Whole tokens. ``--session`` must not match ``--session-dir`` or ``--session-id``.
PROBE_HELP_TOKENS = (
    "-p",
    "--mode",
    "json",
    "rpc",
    "--offline",
    "--no-approve",
    "--no-context-files",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "-e",
    "--provider",
    "--model",
    "--thinking",
    "--system-prompt",
    "--append-system-prompt",
    "--tools",
    "--no-session",
    "--session-dir",
    "--session",
)
HOME_LAYOUT_ID = "pi-home/v1"
# Keep shells in the private HOME as well: nested runtimes must not discover personal logins.
_HIDDEN_DIRS = ("HOME", *(name for name in child_env.SYSTEM if name.startswith("XDG_")))
_PI_ENV = {
    "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1",
}
_PI_DIRS = ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR")
# pi's bash tool adds these after launch, even when none came from the parent environment.
_PI_SHELL_METADATA = ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL")
_PLATFORM_ENV = ("LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN", KEY_ENV)
_ISOLATION_FLAGS = (
    "--offline", "--no-approve", "--no-context-files", "--no-extensions", "--no-mcp",
    "--no-skills", "--no-prompt-templates", "--no-themes",
)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class _ProbeFailure(Exception):
    """``--version`` or ``--help`` did not finish; ``message`` is the probe error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PiRuntime(RuntimeAdapter):
    """pi's type; launch construction is available, execution capabilities remain gated."""

    type = "pi"
    label = "pi"
    default_executable = "pi"
    # Empty until the launch, sessions, stream and Looper tool bridge exist. Not a general MCP client.
    capabilities = frozenset()
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT})
    # API keys only. pi's own /login and OAuth are not providers Looper binds.
    auth_kinds = frozenset({AuthKind.API_KEY})
    isolation_id = HOME_LAYOUT_ID
    provider_env_owned: ClassVar[frozenset[str]] = frozenset({
        *_PI_DIRS, *_PI_ENV, *_PLATFORM_ENV, "PI_SHELL_PREFIX", "PI_CONFIG_FILES", "PI_CONFIG_DIR",
    })

    def capabilities_for(self, version: str | None) -> frozenset[Capability]:
        """Nothing is advertised yet, whatever ``version`` the probe read.

        The later set is run, a resident session, resume, steer, read tools,
        compaction events, and mcp for the Looper tool bridge only. That is
        not a general MCP client, and none of it is enabled until those paths
        exist.
        """
        del version
        return frozenset()

    def probe(self, executable: str) -> Probe:
        """``executable --version`` and, when that is new enough, ``--help``.

        Both run offline in a temporary home, agent directory and session
        directory, and each has 10 seconds. Nothing of the operator's pi
        configuration is read, and no model is called.
        """
        root = Path(tempfile.mkdtemp(prefix="looper-pi-probe-"))
        try:
            home, agent, session = root / "home", root / "home" / "agent", root / "session"
            for path in (home, agent, session):
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
            env = _probe_env(home, agent, session)
            try:
                done = _run(executable, "--version", env, home)
            except _ProbeFailure as failure:
                return Probe(None, self.capabilities_for(None), failure.message)
            line = _first_line(done.stdout or "") or _first_line(done.stderr or "")
            if done.returncode != 0 or not line:
                return Probe(None, self.capabilities_for(None), _version_failure(executable, done.returncode, line))
            shown = line[:_VERSION_MAX]
            found = _VERSION.search(line)
            if found is None:
                return Probe(shown, self.capabilities_for(shown), _unknown_version(executable, shown))
            if tuple(int(part) for part in found.groups()) < MIN_VERSION:
                return Probe(shown, self.capabilities_for(shown), _old_version(shown, executable))
            try:
                help_done = _run(executable, "--help", env, home)
            except _ProbeFailure as failure:
                return Probe(shown, self.capabilities_for(shown), f"{failure.message}; probe again")
            help_text = f"{help_done.stdout or ''}{help_done.stderr or ''}"
            if help_done.returncode != 0 or not help_text.strip():
                return Probe(shown, self.capabilities_for(shown), _help_failure(executable, help_done.returncode, help_text))
            missing = [token for token in PROBE_HELP_TOKENS if not _has_token(help_text, token)]
            if missing:
                return Probe(shown, self.capabilities_for(shown), _missing_flags(executable, missing))
            return Probe(shown, self.capabilities_for(shown))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _refuse(self) -> NoReturn:
        raise RuntimeUnsupported("pi does not implement this yet; only version and help probes run")

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        self._refuse()

    def isolation_args(self, cwd: str | Path) -> list[str]:
        return list(_ISOLATION_FLAGS)

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """Pin the private provider and selected model; the API key goes only in the child environment."""
        _check_provider(provider, model)
        _check_explicit_env(provider.env)
        assert provider.api_key is not None  # checked above; ProviderBinding.env contains strings only
        if effort and effort not in THINKING_LEVELS:
            raise RuntimeUnsupported(f"pi thinking must be one of {', '.join(THINKING_LEVELS)}, or empty")
        return ProviderBinding(
            args=("--provider", PROVIDER_ID, "--model", model, *(("--thinking", effort) if effort else ())),
            env=(*provider.env, (KEY_ENV, provider.api_key)),
        )

    def launch_args(self, launch: Launch) -> list[str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return ["-p", "--mode", "json", *_ISOLATION_FLAGS, *binding.args]

    def environment(self, launch: Launch) -> dict[str, str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        _check_explicit_env(launch.env)
        visible = {**child_env.inherited(), **dict(launch.env), **dict(binding.env)}
        # HOME and the two directory bindings need the prompt/session paths; run_command sets them via env.
        return {**_isolated_env(visible), KEY_ENV: dict(binding.env)[KEY_ENV], **_PI_ENV}

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        self._refuse()

    def resume_args(self, session_id: str) -> list[str]:
        self._refuse()

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        # Validate role/provider overrides before creating anything. max_turns has no pi CLI equivalent.
        self.environment(launch)
        local = Path(prompt_file).absolute().parent.parent
        home = write_home(local, models_json(launch), settings_json(tuple(name for name, _ in launch.profile.provider.env)),
                          binding_sha256=launch.binding_sha256)
        sessions = local / "pi" / "sessions"
        _private_dir(sessions)
        if session_dir is None:
            session = Path(tempfile.mkdtemp(prefix="run-", dir=sessions))
            session.chmod(0o700)
        else:
            session = Path(session_dir).absolute()
            if session.resolve().is_relative_to((local / "pi" / "homes").resolve()):
                raise RuntimeUnsupported("pi session directories must be outside shared homes")
            _private_dir(session)
        prompt_flag = ("--system-prompt" if launch.system_prompt_mode is SystemPromptMode.REPLACE
                       else "--append-system-prompt")
        return [
            "env", f"HOME={home}", f"PI_CODING_AGENT_DIR={home / 'agent'}",
            f"PI_CODING_AGENT_SESSION_DIR={session}", executable, *self.launch_args(launch),
            prompt_flag, str(prompt_file),
            *(["--no-session"] if session_dir is None else ["--session-dir", str(session)]),
        ]

    def resident_session(self, label: str) -> ResidentSession:
        self._refuse()

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        self._refuse()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        self._refuse()

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        self._refuse()

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        self._refuse()

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        self._refuse()

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        self._refuse()


def provider_api(provider: ProviderSpec) -> str:
    """The supported API to use, preferring Responses over Chat Completions."""
    api = next((name for protocol, name in API_NAMES if protocol in provider.protocols), None)
    if api is None:
        raise RuntimeUnsupported(f"pi speaks protocols {', '.join(p.value for p, _ in API_NAMES)}; "
                                 f"provider {provider.name!r} serves none of them")
    return api


def _check_provider(provider: ProviderSpec, model: str) -> str:
    """Fail before rendering or launching; never guess an endpoint or use a pi login."""
    if provider.auth is not AuthKind.API_KEY:
        raise RuntimeUnsupported(f"pi runs with an API key provider only; provider {provider.name!r} is not one")
    api = provider_api(provider)
    if not provider.api_key or not provider.api_key.strip():
        raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
    if not provider.base_url or not provider.base_url.strip():
        raise RuntimeUnsupported(f"provider {provider.name!r} has no base_url: set its endpoint in Settings")
    if provider.models and model not in provider.models:
        raise RuntimeUnsupported(f"provider {provider.name!r} does not list model {model!r}")
    return api


def models_json(launch: Launch) -> dict[str, Any]:
    """The private models.json, containing only the chosen model and an interpolated key reference.

    Missing context/output limits stay with pi's defaults; catalog prices are handled by Looper.
    """
    provider = launch.profile.provider
    api = _check_provider(provider, launch.model)
    entry = provider.catalog_model(launch.model)
    model: dict[str, Any] = {
        "id": launch.model, "name": launch.model,
        "reasoning": not (entry is not None and entry.efforts is not None and not entry.efforts),
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    if entry is not None and entry.context_window:
        model["contextWindow"] = entry.context_window
    if api == "openai-responses":
        # pi 1.3's openai-responses getCompat/convertResponsesTools (also checked in request serialization):
        # the default omits strict; this sends strict:false without making optional parameters required.
        model["compat"] = {"supportsStrictMode": True}
    return {"providers": {PROVIDER_ID: {"baseUrl": provider.base_url, "apiKey": f"${KEY_ENV}",
                                        "api": api, "models": [model]}}}


def _check_explicit_env(entries: tuple[tuple[str, str], ...]) -> None:
    for name, _ in entries:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RuntimeUnsupported("pi environment variable names must be shell identifiers")
        if name in _HIDDEN_DIRS or name.startswith(("PI_", "LOOPER_", "LOOPX_")):
            raise RuntimeUnsupported(f"pi owns {name}; provider and role environments cannot override it")


def _isolated_env(visible: Mapping[str, str]) -> dict[str, str]:
    dropped = {*_HIDDEN_DIRS, *child_env.RUNTIMES}
    return {name: value for name, value in visible.items()
            if name not in dropped and not name.startswith(("PI_", "LOOPER_", "LOOPX_"))}


def settings_json(provider_names: tuple[str, ...] = ()) -> dict[str, Any]:
    """Disable background spending/discovery; shell children keep private HOME without run credentials."""
    cleared = sorted({*_PLATFORM_ENV, *_PI_DIRS, *_PI_ENV, *_PI_SHELL_METADATA, *child_env.RUNTIMES, *provider_names})
    return {
        "cacheWarming": "off", "enableAnalytics": False, "enableInstallTelemetry": False,
        "defaultProjectTrust": "never",
        "packages": [], "extensions": [], "skills": [], "prompts": [], "themes": [],
        "shellCommandPrefix": "unset " + " ".join(shlex.quote(name) for name in cleared) + ";",
    }


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private(path: Path, text: str) -> None:
    """Publish a complete 0600 file; failed writes leave no temporary file behind."""
    fd, building = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(building, path)
    finally:
        Path(building).unlink(missing_ok=True)


def write_home(local: Path, models: Mapping[str, Any], settings: Mapping[str, Any], *, binding_sha256: str) -> Path:
    """Atomically publish reusable configuration, with no per-run session state in the home."""
    files = {name: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
             for name, value in (("models.json", models), ("settings.json", settings))}
    key = hashlib.sha256(json.dumps([HOME_LAYOUT_ID, binding_sha256, files], sort_keys=True).encode()).hexdigest()
    root = local / "pi"
    homes = root / "homes"
    for path in (root, homes):
        _private_dir(path)
    target = homes / key
    if target.is_dir():
        return target
    building = Path(tempfile.mkdtemp(prefix=".building-", dir=homes))
    try:
        building.chmod(0o700)
        _private_dir(building / "agent")
        for name, text in files.items():
            _write_private(building / "agent" / name, text)
        try:
            os.rename(building, target)
        except OSError as exc:
            # Another process may have published the identical complete directory first.
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not target.is_dir():
                raise
    finally:
        shutil.rmtree(building, ignore_errors=True)
    return target


def _probe_env(home: Path, agent: Path, session: Path) -> dict[str, str]:
    """A child's environment pointed at the probe's directories, offline, without the operator's pi paths or logins."""
    env = _isolated_env(child_env.inherited())
    env.update({
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(agent),
        "PI_CODING_AGENT_SESSION_DIR": str(session),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "PI_SKIP_VERSION_CHECK": "1",
    })
    return env


def _run(executable: str, flag: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """One of the two probe commands. Raises ``_ProbeFailure`` when it does not finish."""
    try:
        return subprocess.run([executable, flag], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC,
                              check=False, env=env, cwd=cwd)
    except FileNotFoundError:
        raise _ProbeFailure(f"{executable!r} was not found") from None
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise _ProbeFailure(f"{executable} {flag} did not run: {type(exc).__name__}: {exc}") from None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _has_token(text: str, token: str) -> bool:
    """Whether ``token`` appears as its own flag or word, not as the prefix of a longer one."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def _version_failure(executable: str, code: int, line: str) -> str:
    if not line and code == 0:
        return f"{executable} --version printed nothing"
    if not line:
        return f"{executable} --version failed (exit code {code})"
    return f"{executable} --version failed (exit code {code}): {line[:_VERSION_MAX]}"


def _old_version(shown: str, executable: str) -> str:
    return (f"{shown} from {executable} --version is older than 1.3.0, the oldest pi Looper supports; "
            "upgrade pi and probe again")


def _unknown_version(executable: str, shown: str) -> str:
    return (f"{executable} --version printed {shown!r}, which is not a pi version Looper can read; "
            "install pi 1.3.0 or newer and probe again")


def _help_failure(executable: str, code: int, text: str) -> str:
    line = _first_line(text)
    if code == 0 and not line:
        return f"{executable} --help printed nothing; probe again"
    detail = f": {line[:_VERSION_MAX]}" if line else ""
    return f"{executable} --help failed (exit code {code}){detail}; probe again"


def _missing_flags(executable: str, missing: list[str]) -> str:
    return (f"{executable} --help does not show {', '.join(missing)}; "
            "upgrade pi to 1.3.0 or newer and probe again")
