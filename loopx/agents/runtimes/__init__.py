"""Agent runtimes: the layer that runs an agent's loop and tools (RFC decision 10).

A runtime type is code: one adapter per type, here. A runtime instance is a
registered row (``agent_runtimes``) of a type: the executable it runs and what
its last probe found. Every agent run goes through its runtime's adapter, so
the four tiers (Manager, Planner, Worker, Reviewer) never build a runtime's
command themselves. An adapter says:

- **what it can do** (``capabilities``, ``protocols``, ``auth_kinds``):
  which roles it can run (``ROLE_CAPABILITIES``), which LLM API protocols it
  speaks and which kinds of provider it can be started with;
- **how a run starts** (``resident_command``, ``run_command``,
  ``launch_args``, ``environment``): the argv, the environment, and the files
  it names (the system prompt file, the installed plugin directories, the MCP
  configuration), with the runtime's own **isolation** from the operator's
  personal configuration (``isolation_args``);
- **its stream** (``events``, ``write_turn``): its output as LoopX's internal
  events, and one user turn written to it. The internal event is Claude Code's
  stream-json event (``type`` ``system``/``assistant``/``user``/``result``...);
  another runtime's adapter translates its own events into that shape;
- **resident sessions** (``resident_session``, ``resume_args``): a process
  that stays up and takes one user turn at a time, and the arguments that make
  a new process resume a saved session;
- **cost and usage** (``usage``, ``budget_error``): a run's own spend and
  tokens from its result, and why a run stopped at its spend cap.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..claude_cli import ResidentClaude
    from ..run_usage import Usage


class ApiProtocol(str, Enum):
    """The LLM API an agent's requests speak; a runtime speaks some, a provider serves some."""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_RESPONSES = "openai-responses"
    OPENAI_CHAT = "openai-chat"


class AuthKind(str, Enum):
    """How a provider authenticates: a runtime type's own login, or an API key LoopX stores."""

    OAUTH = "oauth"
    API_KEY = "api_key"


class Capability(str, Enum):
    """What a runtime can do for a role."""

    RUN = "run"  # one run of a prompt, which takes more user turns while it runs (a Worker, a Reviewer)
    RESIDENT_SESSION = "resident_session"  # a process that stays up between user turns (the Manager, a Planner)
    RESUME = "resume"  # a new process continues a saved session (the Manager, a Planner)


# What each role needs of its runtime. The Manager and a Planner keep one process per session and resume it
# after a restart; a Worker or Reviewer runs once, taking the Planner's steering while it runs.
ROLE_CAPABILITIES: Mapping[str, frozenset[Capability]] = {
    "manager": frozenset({Capability.RESIDENT_SESSION, Capability.RESUME}),
    "planner": frozenset({Capability.RESIDENT_SESSION, Capability.RESUME}),
    "worker": frozenset({Capability.RUN}),
    "reviewer": frozenset({Capability.RUN}),
}


@dataclass(frozen=True)
class Probe:
    """What probing a runtime found: its version and capabilities, or why it could not be probed."""

    version: str | None
    capabilities: frozenset[Capability]
    error: str | None = None


class RuntimeUnsupported(ValueError):
    """The runtime cannot do what was asked of it (yet)."""


class RuntimeAdapter(ABC):
    """One runtime type. Instances are stateless; ``ADAPTERS`` holds one per type."""

    type: ClassVar[str]
    label: ClassVar[str]  # how the dashboard names the type
    default_executable: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    protocols: ClassVar[frozenset[ApiProtocol]]
    # The provider auth kinds it can be started with now; OAUTH is the type's own login.
    auth_kinds: ClassVar[frozenset[AuthKind]]
    isolation_id: ClassVar[str]  # names its isolation flags in each run's record

    # --- Registration ---

    @abstractmethod
    def probe(self, executable: str) -> Probe:
        """The version and capabilities of ``executable``; never a model call."""

    def capabilities_for(self, version: str | None) -> frozenset[Capability]:
        """What a probed version can do; every version of the type, unless the adapter says otherwise."""
        return self.capabilities

    # --- The launch ---

    @abstractmethod
    def isolation_args(self, cwd: str | Path) -> list[str]:
        """The flags that keep the operator's personal configuration out of a run in ``cwd``."""

    @abstractmethod
    def launch_args(self, launch: Launch) -> list[str]:
        """The flags of an agent's settings, installed items and knowledge."""

    @abstractmethod
    def environment(self, launch: Launch) -> dict[str, str] | None:
        """The subprocess environment; None to inherit the server's."""

    @abstractmethod
    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        """A resident process's command, without resuming a session (``resume_args``)."""

    @abstractmethod
    def resume_args(self, session_id: str) -> list[str]:
        """What makes a resident process continue ``session_id``."""

    @abstractmethod
    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0) -> list[str]:
        """A one-off run's command; its prompt and later user turns come on stdin."""

    # --- Sessions and the stream ---

    @abstractmethod
    def resident_session(self, label: str) -> ResidentClaude:
        """A resident process, not started yet: ``start``, then one ``run_turn`` per user turn, ``stop``."""

    @abstractmethod
    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Write one user turn to a running process."""

    @abstractmethod
    def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        """The process's output as internal events, in order."""

    # --- Cost ---

    @abstractmethod
    def usage(self, result_event: dict[str, Any] | None, *, before: Usage | None = None) -> Usage:
        """A run's own cost and tokens from its result (``before``: the session's totals it started from)."""

    @abstractmethod
    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        """Why a run stopped at its spend cap; None when it did not."""


def _adapters() -> dict[str, RuntimeAdapter]:
    from .claude_code import ClaudeCodeRuntime

    return {adapter.type: adapter for adapter in (ClaudeCodeRuntime(),)}


# The runtime types, by type id. Code owns this set: a registered runtime of a type missing here cannot run.
ADAPTERS: dict[str, RuntimeAdapter] = _adapters()
CLAUDE_CODE: RuntimeAdapter = ADAPTERS["claude-code"]


def adapter(runtime_type: str) -> RuntimeAdapter:
    """The adapter of ``runtime_type``; raises ``RuntimeUnsupported`` for a type this code does not have."""
    try:
        return ADAPTERS[runtime_type]
    except KeyError:
        raise RuntimeUnsupported(f"no runtime type {runtime_type!r}; the types are {', '.join(sorted(ADAPTERS))}") \
            from None
