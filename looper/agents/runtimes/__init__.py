"""Agent runtimes: the layer that runs an agent's loop and tools (RFC decision 10).

A runtime type is code: one adapter per type, here. A runtime instance is a
registered row (``agent_runtimes``) of a type: the executable it runs and what
its last probe found. Every agent run goes through its runtime's adapter, so
the four tiers (Manager, Planner, Worker, Reviewer) never build a runtime's
command themselves. An adapter says:

- **what it can do** (``capabilities``, ``protocols``, ``auth_kinds``):
  which roles it can run (``ROLE_CAPABILITIES``), which LLM API protocols it
  speaks and which kinds of provider it can be started with;
- **how a provider is bound** (``bind_provider``): what points the run at
  its agent's LLM provider and model, beside the runtime's own flags;
- **how a run starts** (``resident_command``, ``run_command``,
  ``launch_args``, ``environment``): the argv, the environment, and the files
  it names (the system prompt file, the installed plugin directories, the MCP
  configuration), with the runtime's own **isolation** from the operator's
  personal configuration (``isolation_args``);
- **its stream** (``parse``, ``write_turn``, ``tool_summary``): its output as
  Looper's internal events, one user turn written to it, and a tool call in
  words for the task's activity. The internal event is Claude Code's
  stream-json event (``type`` ``system``/``assistant``/``user``/``result``...),
  which the recorder, the usage and context readings (``run_usage``) and the
  tiers read; another runtime's adapter translates its own events into it;
- **resident sessions** (``resident_session``, ``resume_args``): a process
  that stays up and takes one user turn at a time, and the arguments that make
  a new process resume a saved session;
- **cost** (``cost``, ``budget_error``): a run's spend for its provider and
  where the figure comes from (``CostSource``), and why a run stopped at its
  spend cap.

What the catalog and the project knowledge put in a run (plugin directories,
the MCP configuration, knowledge directories) is materialized by
``library.load_installed`` and ``knowledge.load_knowledge`` in Claude Code's
plugin format; ``launch_args`` turns it into flags. A runtime of another
format translates it at launch (Codex: ``codex.materialize``,
``codex.config_toml``) and says in the run's record what it left out
(``launch_record``).

The types: ``claude-code`` (``claude_code``) and ``codex`` (``codex``).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from ..agent_registry import ProviderSpec
    from ..agent_settings import Launch
    from ..claude_cli import ResidentTurn
    from ..run_usage import Usage


class ApiProtocol(str, Enum):
    """The LLM API an agent's requests speak; a runtime speaks some, a provider serves some."""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_RESPONSES = "openai-responses"
    OPENAI_CHAT = "openai-chat"


class AuthKind(str, Enum):
    """How a provider authenticates: a runtime type's own login, or an API key Looper stores."""

    OAUTH = "oauth"
    API_KEY = "api_key"


class Capability(str, Enum):
    """What a runtime can do; ``ROLE_CAPABILITIES`` says which of these each role needs."""

    RUN = "run"  # one run of a prompt, which takes more user turns while it runs (a Worker, a Reviewer)
    RESIDENT_SESSION = "resident_session"  # a process that stays up between user turns (the Manager, a Planner)
    RESUME = "resume"  # a new process continues a saved session (the Manager, a Planner)
    STRUCTURED_OUTPUT = "structured_output"  # the runtime enforces a reply schema itself
    CLAUDE_PLUGINS = "claude_plugins"  # it loads Claude Code plugins and skills from a directory
    MCP = "mcp"  # it loads the MCP servers Looper gives it
    ADD_DIRS = "add_dirs"  # it reads extra directories (project knowledge)
    COMPACTION_EVENTS = "compaction_events"  # it reports context compactions
    AUTONOMOUS_TURNS = "autonomous_turns"  # it runs a turn of its own when background work ends, and says so
    COST_REPORT = "cost_report"  # it reports what a run cost


class CostSource(str, Enum):
    """Where a run's cost comes from: what the runtime reported, or its tokens at the provider's prices."""

    RUNTIME = "runtime"
    PROVIDER_PRICE = "provider_price"


# What each role needs of its runtime. The Manager and a Planner keep one process per session and resume it
# after a restart; a Worker or Reviewer runs once, taking the Planner's steering while it runs.
ROLE_CAPABILITIES: Mapping[str, frozenset[Capability]] = {
    "manager": frozenset({Capability.RESIDENT_SESSION, Capability.RESUME}),
    "planner": frozenset({Capability.RESIDENT_SESSION, Capability.RESUME}),
    "worker": frozenset({Capability.RUN}),
    "reviewer": frozenset({Capability.RUN}),
}


@dataclass(frozen=True)
class RunCost:
    """A run's tokens and dollars, and where the dollars come from."""

    usage: Usage
    source: CostSource


@dataclass(frozen=True)
class ProviderBinding:
    """What points a run at its agent's provider and model, beside the runtime's own flags."""

    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Probe:
    """What probing a runtime found: its version and capabilities, or why it could not be probed."""

    version: str | None
    capabilities: frozenset[Capability]
    error: str | None = None


class ResidentSession(Protocol):
    """The Manager's or a Planner's session on a runtime, as the tiers drive it (``RuntimeAdapter.resident_session``):
    ``start`` with a command, then one ``run_turn`` per user turn, ``stop``; ``stale`` when it must start over.
    Claude Code keeps one process for it (``claude_cli.ResidentClaude``), Codex one process per turn
    (``codex.ResidentCodex``)."""

    @property
    def label(self) -> str: ...
    @property
    def started(self) -> bool: ...
    @property
    def command(self) -> list[str]: ...
    @property
    def pid(self) -> int | None: ...
    @property
    def totals(self) -> Usage: ...
    def stale(self, signature: Any) -> bool: ...
    async def start(self, command: list[str], *, cwd: str, signature: Any, env: dict[str, str] | None = None,
                    totals: Usage | None = None, autonomous: Callable[[Any], Awaitable[Any]] | None = None) -> None: ...
    async def run_turn(self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *,
                       tag: Any = None) -> ResidentTurn: ...
    async def stop(self, *, kill: bool = False) -> None: ...


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
    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """What points a run at ``provider``, ``model`` and ``effort`` beside the runtime's own flags; raises
        ``RuntimeUnsupported`` when it cannot."""

    @abstractmethod
    def launch_args(self, launch: Launch) -> list[str]:
        """The flags of an agent's settings, installed items, knowledge and provider binding."""

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
    def resident_session(self, label: str) -> ResidentSession:
        """A resident session, not started yet: ``start``, then one ``run_turn`` per user turn, ``stop``."""

    @abstractmethod
    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Write one user turn to a running process."""

    @abstractmethod
    def parse(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        """The process's output as internal events, in order."""

    @abstractmethod
    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """One of its tool calls in a few words, for the task's activity."""

    # --- Cost ---

    @abstractmethod
    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """A run's cost with ``provider``, from its ``usage`` (its tokens and what the runtime reported)."""

    @abstractmethod
    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        """Why a run stopped at its spend cap; None when it did not."""

    # --- The record ---

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        """What the runtime adds to a run's record, such as the installed items it could not load; nothing
        unless the adapter says otherwise."""
        return {}


def _adapters() -> dict[str, RuntimeAdapter]:
    from .claude_code import ClaudeCodeRuntime
    from .codex import CodexRuntime

    return {adapter.type: adapter for adapter in (ClaudeCodeRuntime(), CodexRuntime())}


# The runtime types, by type id. Code owns this set: a registered runtime of a type missing here cannot run.
ADAPTERS: dict[str, RuntimeAdapter] = _adapters()
CLAUDE_CODE: RuntimeAdapter = ADAPTERS["claude-code"]


def can_resume(session_runtime_type: str | None, runtime: RuntimeAdapter) -> bool:
    """Whether a process of ``runtime`` can resume a session saved by one of ``session_runtime_type`` (None: a
    session from before agents, which Claude Code ran): only one of the same type can."""
    return (session_runtime_type or CLAUDE_CODE.type) == runtime.type


def adapter(runtime_type: str) -> RuntimeAdapter:
    """The adapter of ``runtime_type``; raises ``RuntimeUnsupported`` for a type this code does not have."""
    try:
        return ADAPTERS[runtime_type]
    except KeyError:
        raise RuntimeUnsupported(f"no runtime type {runtime_type!r}; the types are {', '.join(sorted(ADAPTERS))}") \
            from None
