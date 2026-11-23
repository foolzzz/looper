"""The operator's ACP command, limited to the Reviewer pilot."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import (ApiProtocol, AuthKind, Capability, CostSource, Launcher, Probe, PromptBlock,
               ProviderBinding, RunCost, RuntimeAdapter, RuntimeUnsupported)

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..profile_registry import LegacyProviderSpec as ProviderSpec
    from ..run_usage import Usage


_SECRET_FIELDS = frozenset({"key", "token", "secret", "password", "authorization", "credential",
                            "credentials", "cookie", "auth"})
_KEY_VALUE = re.compile(r"(?i)\b(?:sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|xox[abposr]-[\w-]+)\b")


def _secret_name(name: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower().lstrip("-")
    return (normalized in {"apikey", "accesstoken", "clientsecret", "authkey"}
            or bool(set(re.split(r"[-_]", normalized)) & _SECRET_FIELDS))


def record_command(command: Sequence[str]) -> list[str]:
    """A display command only: never use its redacted argv to spawn the process."""
    from ..log_redaction import redact

    if not command:
        return ["acp"]
    safe = [Path(command[0]).name]
    mask_next = False
    for value in command[1:]:
        if mask_next:
            safe.append("***")
            mask_next = False
            continue
        name, equal, _ = value.partition("=")
        assignment = bool(equal and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))
        if (value.startswith("-") or assignment) and _secret_name(name):
            safe.append(name + "=***" if equal else name)
            mask_next = not equal
            continue
        try:
            url = urlsplit(value)
            if url.scheme in {"http", "https"} and url.netloc:
                host = url.netloc.rsplit("@", 1)[-1]
                netloc = "***@" + host if "@" in url.netloc else host
                query = urlencode([(key, "***" if _secret_name(key) else val)
                                   for key, val in parse_qsl(url.query, keep_blank_values=True)])
                value = urlunsplit((url.scheme, netloc, url.path, query, url.fragment))
        except ValueError:
            value = "***"  # malformed URLs cannot safely establish credential boundaries
        safe.append(_KEY_VALUE.sub("***", redact(value)))
    return safe


class AcpRuntime(RuntimeAdapter):
    type = "acp"
    label = "ACP reviewer (pilot)"
    default_executable = ""
    roles = frozenset({"reviewer"})
    capabilities = frozenset({Capability.RUN})
    # Pairing only: the command owns its model transport and authentication.
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    auth_kinds = frozenset({AuthKind.OAUTH})
    isolation_id = "acp-reviewer-write-policy-v1"

    def probe(self, executable: str) -> Probe:
        # Registration is operator-driven; probing an arbitrary command is not
        # required to discover capabilities and must never start a model turn.
        return Probe(None, self.capabilities)

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        return [PromptBlock("Looper instructions", "looper", looper_prompt, "the first ACP prompt text part")]

    def isolation_args(self, cwd: str | Path) -> list[str]:
        return []

    def check_pairing(self, provider: ProviderSpec, model: str, effort: str | None = None, *,
                      executable: str | None = None, launcher: Launcher = Launcher.NATIVE) -> None:
        if provider.auth is not AuthKind.OAUTH or provider.oauth_runtime_type != self.type:
            raise RuntimeUnsupported("ACP reviewer requires its own acp-command identity")

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        self.check_pairing(provider, model, effort)
        return ProviderBinding()

    def launch_args(self, launch: Launch) -> list[str]:
        return []

    def environment(self, launch: Launch) -> dict[str, str]:
        from ...child_env import RUNTIMES, inherited

        # The write policy supplies the command's own HOME/GROK_HOME. Never
        # borrow another runtime's login, key or config directory.
        env = {key: value for key, value in inherited().items() if key not in RUNTIMES}
        env.update(launch.env)
        return env

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        raise RuntimeUnsupported("ACP reviewer resident sessions are not supported")

    def resume_args(self, session_id: str) -> list[str]:
        raise RuntimeUnsupported("ACP reviewer resume is not supported")

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        settings = launch.acp_reviewer
        if settings is None:
            raise RuntimeUnsupported("ACP reviewer launch settings are missing")
        return list(settings.command)

    def steer_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                      max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        raise RuntimeUnsupported("ACP reviewer steering is not supported")

    def run_channel(self, process: asyncio.subprocess.Process, *, launch: Launch, cwd: str | Path,
                    session_dir: Path | None = None, prompt_file: Path | None = None, max_turns: int = 0):
        from .acp_channel import AcpChannel

        return AcpChannel(process, cwd=str(cwd), prompt_file=prompt_file, model=launch.model)

    def resident_session(self, label: str):
        raise RuntimeUnsupported("ACP reviewer resident sessions are not supported")

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        raise RuntimeUnsupported("ACP reviewer uses its JSON-RPC channel; write_turn is not supported")

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None):
        raise RuntimeUnsupported("ACP reviewer uses its JSON-RPC channel; parse is not supported")

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        return f"Tool: {tool_name}"

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        return RunCost(usage, CostSource.PROVIDER_PRICE)

    def permission_denials(self, result_event: dict[str, Any] | None) -> list[dict[str, str]]:
        return (result_event or {}).get("permission_denials") or []

    def result_notes(self, result_event: dict[str, Any] | None) -> dict[str, Any]:
        return {key: result_event[key] for key in ("runtime_reported_usd", "model_reported", "usage_complete")
                if result_event is not None and key in result_event}

    def failure_detail(self, result_event: dict[str, Any] | None, *, launch: Launch,
                       command: list[str], pid: int | None = None) -> str | None:
        from ..log_redaction import redact

        errors = (result_event or {}).get("errors")
        return redact(str(errors[0]))[:500] if isinstance(errors, list) and errors else None

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        return None

    async def login_problem(self, provider: ProviderSpec, *, executable: str | None = None,
                            launcher: Launcher = Launcher.NATIVE, wait: bool = True) -> str | None:
        from ..acp_reviewer_settings import read

        try:
            settings = await read()
        except ValueError as exc:
            from ..log_redaction import redact

            return f"acp_reviewer settings unavailable: {redact(str(exc))}"
        except Exception as exc:
            return f"acp_reviewer settings unavailable: {type(exc).__name__}"
        if not settings.command:
            return "acp_reviewer.command is empty"
        command = settings.command[0]
        if not shutil.which(command):
            return "acp_reviewer.command executable is unavailable"
        if settings.config_dir:
            path = Path(settings.config_dir).expanduser()
            if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
                return "acp_reviewer.config_dir is not a readable directory"
        return None

    def instruction_files(self, cwd: str | Path) -> tuple:
        return ()

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        from .acp_channel import PROTOCOL_VERSION

        return {"acp": {"protocol_version": PROTOCOL_VERSION, "looper_mcp_injected": False}}
