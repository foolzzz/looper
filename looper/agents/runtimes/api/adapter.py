"""Model API registration, launch descriptions and resident session adapter."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .. import (
    ApiProtocol, AuthKind, Capability, CostSource, Probe, PromptBlock, ProviderBinding,
    RunCost, RuntimeAdapter, RuntimeUnsupported, runtime_prompt_text,
)

if TYPE_CHECKING:
    from ...agent_settings import Launch
    from ...profile_registry import LegacyProviderSpec as ProviderSpec
    from ...run_usage import Usage


KEY_ENV = "LOOPER_PROVIDER_API_KEY"
RUNTIME_NOTE = "You run on a model API, not inside a coding tool. Your only tools are Looper's platform tools (looper_*): you cannot read or write files, run commands, browse the web or use skills. When the user needs any of that, offer to create a task for it."
_NO_PROCESS = "the api runtime runs the Manager only, inside looper serve; it has no process"


def prompt_text(launch: Launch, looper_prompt: str) -> str:
    return runtime_prompt_text(launch, looper_prompt, RUNTIME_NOTE, keep_empty=False)


@dataclass(frozen=True)
class Descriptor:
    """A recorded launch description, consumed only by the in-process session."""

    protocol: ApiProtocol
    model: str
    base_url: str
    effort: str | None
    context_window: int | None
    system_prompt_file: str
    session: str | None = None

    def to_argv(self) -> list[str]:
        argv = ["looper-api", "--protocol", self.protocol.value, "--model", self.model,
                "--base-url", self.base_url, "--effort", self.effort or "", "--context-window",
                str(self.context_window) if self.context_window else "", "--system-prompt-file",
                self.system_prompt_file]
        return argv + ["--session", self.session] if self.session else argv

    @classmethod
    def parse(cls, argv: list[str]) -> Descriptor:
        flags = ["--protocol", "--model", "--base-url", "--effort", "--context-window", "--system-prompt-file"]
        try:
            if (len(argv) not in (13, 15) or argv[0] != "looper-api" or argv[1:13:2] != flags
                    or (len(argv) == 15 and argv[13] != "--session")
                    or any(not isinstance(value, str) for value in argv)):
                raise ValueError
            return cls(ApiProtocol(argv[2]), argv[4], argv[6], argv[8] or None,
                       int(argv[10]) if argv[10] else None, argv[12],
                       (argv[14] or None) if len(argv) == 15 else None)
        except (ValueError, TypeError, IndexError):
            raise ValueError("not an api runtime command") from None


class ApiRuntime(RuntimeAdapter):
    type = "api"
    label = "Model API (SDK)"
    default_executable = ""
    capabilities = frozenset({Capability.RESIDENT_SESSION, Capability.RESUME})
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    auth_kinds = frozenset({AuthKind.API_KEY})
    roles = frozenset({"manager"})
    in_process = True
    isolation_id = "api-v1"
    can_replace_system_prompt = True
    can_interrupt = True
    provider_env_owned = frozenset({KEY_ENV})

    def probe(self, executable: str) -> Probe:
        try:
            version = importlib.metadata.version("openai")
        except importlib.metadata.PackageNotFoundError:
            return Probe(None, frozenset(),
                         "the openai Python package is not installed with this Looper; reinstall Looper")
        return Probe(f"openai {version}", self.capabilities)

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        return [PromptBlock("Looper instructions", "looper", prompt_text(launch, looper_prompt),
                            "the request's instructions")]

    def isolation_args(self, cwd: str | Path) -> list[str]:
        return []

    def check_pairing(self, provider: ProviderSpec, model: str, effort: str | None = None) -> None:
        if not provider.base_url:
            raise RuntimeUnsupported("the api runtime needs the endpoint's base_url")

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str | None) -> ProviderBinding:
        if provider.auth is AuthKind.OAUTH:
            raise RuntimeUnsupported(f"the api runtime takes an API key provider; {provider.name!r} is a login")
        if provider.protocols != {ApiProtocol.OPENAI_RESPONSES}:
            raise RuntimeUnsupported(f"the api runtime speaks openai-responses; provider {provider.name!r} "
                                     "must supply that exact protocol")
        if len(provider.catalog) != 1 or provider.catalog[0].name != model:
            raise RuntimeUnsupported("the api runtime requires one resolved model matching the launch model")
        if not provider.api_key:
            raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
        return ProviderBinding(args=(), env=((KEY_ENV, provider.api_key),))

    def launch_args(self, launch: Launch) -> list[str]:
        self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return []

    def environment(self, launch: Launch) -> dict[str, str]:
        return dict(self.bind_provider(launch.profile.provider, launch.model, launch.effort).env)

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        text = prompt_text(launch, Path(prompt_file).read_text(encoding="utf-8"))
        # SystemPrompt.write_file places the input under <runtime_root>/.local/prompts/.
        local = Path(prompt_file).absolute().parent.parent
        directory = local / "api" / "prompts"
        for path in (local / "api", directory):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        target = directory / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"
        if not target.exists():
            fd, temporary = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        provider = launch.profile.provider
        return Descriptor(ApiProtocol.OPENAI_RESPONSES, self.api_model(launch.model), provider.base_url,
                          launch.effort, provider.catalog[0].context_window, str(target)).to_argv()

    def resume_args(self, session_id: str) -> list[str]:
        return ["--session", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        raise RuntimeUnsupported(_NO_PROCESS)

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        raise RuntimeUnsupported(_NO_PROCESS)

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None):
        raise RuntimeUnsupported(_NO_PROCESS)

    def resident_session(self, label: str):
        from .session import ApiSession

        return ApiSession(label=label)

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        return f"Tool: {tool_name}"

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        return RunCost(usage, CostSource.PROVIDER_PRICE)

    def result_notes(self, result_event: dict[str, Any] | None) -> dict[str, Any]:
        return {"api": result_event["api"]} if result_event and "api" in result_event else {}

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        from ... import claude_cli

        return claude_cli.budget_error(result_event) if (result_event or {}).get(claude_cli.SOFT_CAP) else None

    def failure_detail(self, result_event: dict[str, Any] | None, *, launch: Launch,
                       command: list[str], pid: int | None = None) -> str | None:
        from ... import log_redaction

        errors = (result_event or {}).get("errors")
        return log_redaction.redact(str(errors[0]))[:300] if errors else None

    def instruction_files(self, cwd: str | Path) -> tuple:
        return ()

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        from ...library import ItemKind

        provider = launch.profile.provider
        return {
            "api": {"protocol": ApiProtocol.OPENAI_RESPONSES.value,
                    "base_url_host": urlparse(provider.base_url).hostname},
            "omitted": [{"kind": item.kind.value, "name": item.name,
                         "reason": "the api runtime loads no plugins, skills or catalog MCP servers"}
                        for item in launch.library.items
                        if not (item.kind is ItemKind.MCP and item.name == "looper")],
            "provider_env_ignored": sorted(name for name, _ in provider.env),
            "settings_env_ignored": sorted(name for name, _ in launch.env),
        }
