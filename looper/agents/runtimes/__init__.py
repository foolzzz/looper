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
- **cost** (``cost_source``, ``cost``, ``budget_error``): where a run's
  spend figure comes from with a provider (``CostSource``), the run's spend,
  and why a run stopped at its spend cap. Tokens priced at a provider's prices
  (``pricing.price_cost``) and the per-run soft cap Looper enforces itself
  (``pricing.cap_exceeded``, fed by internal ``usage`` events) are shared by
  every adapter;
- **its own login** (``login_problem``, ``after_run``, ``login_models``):
  why a run with the runtime's login cannot start now, what a run left wrong
  with the login, and the models the form suggests for it (Codex:
  ``codex_login``);
- **the project's instruction files** (``instruction_files``): which of the
  repository's ``CLAUDE.md`` and ``AGENTS.md`` a run is given, read by the
  runtime itself or put in its system prompt by Looper (``instructions``).

What the catalog and the project knowledge put in a run (plugin directories,
the MCP configuration, knowledge directories) is materialized by
``library.load_installed`` and ``knowledge.load_knowledge`` in Claude Code's
plugin format; ``launch_args`` turns it into flags. A runtime of another
format translates it at launch (Codex: ``codex.materialize``,
``codex.config_toml``; OMP: ``omp.overlay``, ``omp.mcp_config``) and says in
the run's record what it left out (``launch_record``).

A runtime whose CLI runs one turn per process keeps the Manager's and a
Planner's session by resuming it on every turn (``per_turn.PerTurnSession``).

The types: ``claude-code`` (``claude_code``), ``codex`` (``codex``) and ``omp``
(``omp``).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from ..profile_registry import ProviderSpec
    from ..agent_settings import Launch
    from ..claude_cli import CapStop, ResidentTurn
    from ..run_usage import Price, Usage
    from .instructions import InstructionFile


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
    """What points a run at its agent's provider and model, beside the runtime's own flags: arguments, variables
    set in its environment and variables removed from the environment it inherits (``unset``). ``env`` may hold
    the provider's API key: it goes into the subprocess environment only, never into a record or a log."""

    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    unset: tuple[str, ...] = ()


@dataclass(frozen=True)
class Probe:
    """What probing a runtime found: its version and capabilities, or why it could not be probed."""

    version: str | None
    capabilities: frozenset[Capability]
    error: str | None = None


class ResidentSession(Protocol):
    """The Manager's or a Planner's session on a runtime, as the tiers drive it (``RuntimeAdapter.resident_session``):
    ``start`` with a command, then one ``run_turn`` per user turn, ``stop``; ``stale`` when it must start over.
    Claude Code keeps one process for it (``claude_cli.ResidentClaude``), Codex and OMP one process per turn
    (``per_turn.PerTurnSession``: ``codex.ResidentCodex``, ``omp.ResidentOmp``)."""

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
                    totals: Usage | None = None, autonomous: Callable[[Any], Awaitable[Any]] | None = None,
                    price: Price | None = None) -> None: ...
    async def run_turn(self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *,
                       tag: Any = None, stop: Callable[[], CapStop | None] | None = None) -> ResidentTurn: ...
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
    # The environment variables its provider binding owns: a provider's own variables (``ProviderSpec.env``)
    # cannot set them.
    provider_env_owned: ClassVar[frozenset[str]] = frozenset()
    # Whether a run reads more user turns while it runs (``write_turn`` after the prompt): a forwarded message, a
    # Planner's nudge, an agent room reminder. A runtime that reads only its prompt has False.
    run_input: ClassVar[bool] = False

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
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        """A one-off run's command; its prompt and later user turns come on stdin. With ``session_dir`` a runtime
        that keeps no session for a run (``--ephemeral``, ``--no-session``) keeps it there, so it can be resumed
        later (the agent room's consults); Claude Code keeps every run's session in its own place."""

    # --- Sessions and the stream ---

    @abstractmethod
    def resident_session(self, label: str) -> ResidentSession:
        """A resident session, not started yet: ``start``, then one ``run_turn`` per user turn, ``stop``."""

    @abstractmethod
    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Write one user turn to a running process."""

    @abstractmethod
    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """The process's output as internal events, in order; ``model`` is the one the run was started with, for a
        runtime whose output does not name it."""

    @abstractmethod
    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """One of its tool calls in a few words, for the task's activity."""

    # --- Cost ---

    def cost_source(self, provider: ProviderSpec) -> CostSource:
        """Where a run's spend figure comes from with ``provider``: Looper prices its tokens at the provider's
        prices unless the adapter says the runtime's own report is right for it."""
        return CostSource.PROVIDER_PRICE

    @abstractmethod
    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """A run's cost with ``provider``, from its ``usage`` (its tokens and what the runtime reported)."""

    @abstractmethod
    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        """Why a run stopped at its spend cap; None when it did not."""

    # --- Its own login ---

    # The models the Agent Profile form suggests with the runtime's own login (any model is taken).
    login_models: ClassVar[tuple[str, ...]] = ()

    async def login_problem(self, provider: ProviderSpec) -> str | None:
        """Why a run with ``provider`` (the runtime's own login) cannot start now; None when it can, or when the
        adapter cannot tell."""
        return None

    async def after_run(self, launch: Launch, command: list[str]) -> list[str]:
        """Checked when a run (or a resident turn) of ``command`` ends: what it left wrong in the runtime's own
        state that the operator must hear of (the run's task activity and the server log carry it); nothing
        unless the adapter says otherwise."""
        return []

    # --- The project's instruction files ---

    @abstractmethod
    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        """Which of its project's own instruction files (``CLAUDE.md``, ``AGENTS.md``) a run in ``cwd`` is given,
        and how (``instructions``)."""

    # --- The record ---

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        """What the runtime adds to a run's record, such as the installed items it could not load; nothing
        unless the adapter says otherwise."""
        return {}


def _adapters() -> dict[str, RuntimeAdapter]:
    from .claude_code import ClaudeCodeRuntime
    from .codex import CodexRuntime
    from .omp import OmpRuntime

    return {adapter.type: adapter for adapter in (ClaudeCodeRuntime(), CodexRuntime(), OmpRuntime())}


# The runtime types, by type id. Code owns this set: a registered runtime of a type missing here cannot run.
ADAPTERS: dict[str, RuntimeAdapter] = _adapters()
CLAUDE_CODE: RuntimeAdapter = ADAPTERS["claude-code"]


# The provider of a session saved before sessions recorded one: Claude Code's own login, the only provider a
# Claude Code agent could run with then (profile_registry.BUILTIN_PROVIDER).
LEGACY_SESSION_PROVIDER_ID = "prov-claude-subscription"


def can_resume(session_runtime_type: str | None, session_provider_id: str | None, runtime: RuntimeAdapter,
               provider_id: str) -> bool:
    """Whether a process of ``runtime`` with provider ``provider_id`` can resume a session saved by
    ``session_runtime_type`` with ``session_provider_id``: only when both are unchanged (RFC P5). A provider's
    protocol fields (thinking signatures, tool_use ids, reasoning items) mean nothing to another provider, so
    every change of provider, in any direction, starts a new session; a change of model alone does not.

    None for the runtime type is a session from before agents (Claude Code ran it); None for the provider is a
    session from before sessions recorded it (``LEGACY_SESSION_PROVIDER_ID``)."""
    return ((session_runtime_type or CLAUDE_CODE.type) == runtime.type
            and (session_provider_id or LEGACY_SESSION_PROVIDER_ID) == provider_id)


def adapter(runtime_type: str) -> RuntimeAdapter:
    """The adapter of ``runtime_type``; raises ``RuntimeUnsupported`` for a type this code does not have."""
    try:
        return ADAPTERS[runtime_type]
    except KeyError:
        raise RuntimeUnsupported(f"no runtime type {runtime_type!r}; the types are {', '.join(sorted(ADAPTERS))}") \
            from None



# The shared pricing helpers every adapter uses, here beside the adapters (``pricing``).
from .pricing import cap_exceeded, price_cost  # noqa: E402, F401
