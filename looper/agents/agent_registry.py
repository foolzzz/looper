"""Runtimes, LLM providers and agents: what they are, how they are checked, and their API (RFC decision 10).

An **agent** is a runtime, an LLM provider and a model, with a default
effort. A **runtime** is a registered instance of a runtime type (an adapter
in ``looper.agents.runtimes``): the executable it runs and what its probe found
(``<executable> --version``, never a model call). A **provider** is where an
agent's requests go: the protocols it serves (``anthropic-messages``,
``openai-responses``, ``openai-chat``), its ``base_url``, its model catalog
(each model's context window, prices and effort levels, each unknown until
given), how many runs it takes at once (``max_concurrency``, none for no
limit) and how it authenticates: a runtime type's own login (``oauth``, bound
to that type) or an API key, which is stored apart (``llm_provider_secrets``)
and shown as ``***``. The catalog's prices and the concurrency limit are
stored and checked here; the runs do not use them yet.

The built-ins exist on every installation and are read-only: the
``claude-code`` runtime (the server's configured ``claude``), the
``claude-subscription`` provider (Claude Code's own login) and the
``claude-code/opus`` agent, which every role runs when no layer selects
another (``BUILTIN_AGENT``). A runtime of another type is built in too once
``looper serve`` finds its executable on PATH (``register_found_runtimes``):
``codex`` for Codex.

An agent is checked when it is created or changed, and again when a provider
it uses changes (``check_pairing``):

- its runtime's type is one this version can run;
- the runtime speaks a protocol the provider serves;
- an OAuth provider pairs only with runtimes of the type whose login it is;
- the runtime can be started with that kind of provider now
  (``RuntimeAdapter.auth_kinds``);
- the model is one the provider lists, when it lists any.

Where an agent is selected (a layer, or a task's choice for one of its roles),
its runtime must have what each of those roles needs (``check_roles``,
``runtimes.ROLE_CAPABILITIES``): a resident session and resume for the
Manager and the Planner, a run for the Workers and Reviewers. Every runtime is
meant to run every role; one that cannot yet is refused with the reason.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import runtimes
from .runtimes import ApiProtocol, AuthKind, Capability, RuntimeUnsupported

NAME_MAX_CHARS = 240
EXECUTABLE_MAX_CHARS = 1_000
BASE_URL_MAX_CHARS = 2_000
API_KEY_MAX_CHARS = 4_000
MODELS_MAX = 100
PRICE_MAX_USD = 10_000
PRICE_FIELDS = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")
MODEL_FIELDS = ("name", "context_window", *PRICE_FIELDS, "efforts")
CONTEXT_WINDOW_MAX = 100_000_000
EFFORTS_MAX = 10
CONCURRENCY_MAX = 1_000
# What the API shows, and takes back unchanged, in place of a stored API key.
SECRET_MASK = "***"


class RegistryError(ValueError):
    """A request the registry refuses (answered with 400)."""


class RegistryNotFound(LookupError):
    """No such runtime, provider or agent (404)."""


class RegistryConflict(Exception):
    """A name in use, or a row still referred to (409)."""


# --- What an agent is made of ---


@dataclass(frozen=True)
class RuntimeSpec:
    id: str
    name: str
    type: str
    executable: str = ""  # '' = the type's default (for Claude Code, the server's configured claude)
    capabilities: frozenset[Capability] = frozenset()


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    auth: AuthKind
    protocols: frozenset[ApiProtocol]
    oauth_runtime_type: str | None = None
    base_url: str = ""
    models: tuple[str, ...] = ()  # the models it lists; () = any


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    runtime: RuntimeSpec
    provider: ProviderSpec
    model: str
    effort: str | None = None  # its default effort; a layer's effort wins

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AgentSpec:
        """An agent from ``db.agent_registry.AGENT_JSON``."""
        known = {c.value for c in Capability}
        return cls(
            id=row["id"], name=row["name"], model=row["model"], effort=row.get("effort"),
            runtime=RuntimeSpec(row["runtime_id"], row["runtime_name"], row["runtime_type"],
                                row.get("runtime_executable") or "",
                                frozenset(Capability(c) for c in row.get("runtime_capabilities") or () if c in known)),
            provider=ProviderSpec(
                row["provider_id"], row["provider_name"], AuthKind(row["provider_auth"]),
                frozenset(ApiProtocol(p) for p in row.get("provider_protocols") or ()),
                row.get("provider_oauth_runtime_type"), row.get("provider_base_url") or "",
                tuple(str(m.get("name")) for m in row.get("provider_models") or () if isinstance(m, Mapping)),
            ),
        )

    def record(self) -> dict[str, Any]:
        """Its entry in a run's record: what it is made of, by id and name."""
        return {
            "id": self.id, "name": self.name, "model": self.model,
            "runtime": {"id": self.runtime.id, "name": self.runtime.name, "type": self.runtime.type},
            "provider": {"id": self.provider.id, "name": self.provider.name, "auth": self.provider.auth.value},
        }


BUILTIN_RUNTIME = RuntimeSpec("rt-claude-code", "claude-code", "claude-code", "", runtimes.CLAUDE_CODE.capabilities)
BUILTIN_PROVIDER = ProviderSpec("prov-claude-subscription", "claude-subscription", AuthKind.OAUTH,
                                frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), "claude-code")
BUILTIN_AGENT = AgentSpec("agent-claude-code-opus", "claude-code/opus", BUILTIN_RUNTIME, BUILTIN_PROVIDER, "opus")


# --- Validation: each raises RegistryError ---

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _fields(data: Any, allowed: Iterable[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RegistryError("body must be a JSON object")
    allowed = tuple(allowed)
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise RegistryError(f"unknown field {unknown[0]!r}; the fields are {', '.join(allowed)}")
    return data


def entry_name(raw: Any, what: str) -> str:
    """A name: trimmed, 1 to ``NAME_MAX_CHARS`` characters, no control characters, not starting with ``-``."""
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        raise RegistryError(f"a {what} needs a name")
    if len(name) > NAME_MAX_CHARS or _CONTROL.search(name) or name.startswith("-"):
        raise RegistryError(f"a {what} name has 1 to {NAME_MAX_CHARS} characters, no control characters, and does "
                            "not start with -")
    return name


def _executable(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or _CONTROL.search(raw) or len(raw.strip()) > EXECUTABLE_MAX_CHARS:
        raise RegistryError(f"executable must be a path or command of at most {EXECUTABLE_MAX_CHARS} characters, "
                            "or empty for the type's default")
    executable = raw.strip()
    if executable.startswith("-"):
        raise RegistryError("executable must be a path or a command, not a flag")
    return executable


def _runtime_type(raw: Any) -> runtimes.RuntimeAdapter:
    try:
        return runtimes.adapter(raw if isinstance(raw, str) else "")
    except RuntimeUnsupported as exc:
        raise RegistryError(str(exc)) from None


def _protocols(raw: Any) -> list[str]:
    known = [p.value for p in ApiProtocol]
    if not isinstance(raw, list) or not raw or any(p not in known for p in raw) or len(set(raw)) != len(raw):
        raise RegistryError(f"protocols must be a non-empty list of distinct values from {', '.join(known)}")
    return [p for p in known if p in raw]  # in a fixed order


def _base_url(raw: Any) -> str:
    if raw is None:
        return ""
    url = raw.strip() if isinstance(raw, str) else None
    if url is None or len(url) > BASE_URL_MAX_CHARS or (url and not re.match(r"https?://[^\s/]+", url)) \
            or (url and _CONTROL.search(url)):
        raise RegistryError(f"base_url must be an http:// or https:// URL of at most {BASE_URL_MAX_CHARS} "
                            "characters, or empty for the protocol's own endpoint")
    return url


def _price(raw: Any, field: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) \
            or not 0 <= raw <= PRICE_MAX_USD:
        raise RegistryError(f"{field} must be US dollars per million tokens, from 0 to {PRICE_MAX_USD}, or null")
    return float(raw)


def _context_window(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 < raw <= CONTEXT_WINDOW_MAX:
        raise RegistryError(f"context_window must be a number of tokens from 1 to {CONTEXT_WINDOW_MAX}, or null")
    return raw


def _efforts(raw: Any) -> list[str] | None:
    """The effort levels a model takes, in the provider's own words (``low``, ``minimal``...); null: unknown."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > EFFORTS_MAX or len(set(map(str, raw))) != len(raw) \
            or not all(isinstance(e, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", e) for e in raw):
        raise RegistryError(f"efforts must be a list of at most {EFFORTS_MAX} distinct effort level names "
                            "(such as low, medium, high), or null")
    return list(raw)


def _max_concurrency(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 < raw <= CONCURRENCY_MAX:
        raise RegistryError(f"max_concurrency must be a number of runs from 1 to {CONCURRENCY_MAX}, or null for "
                            "no limit")
    return raw


def _models(raw: Any) -> list[dict[str, Any]]:
    """The model catalog; every field but the name may be null (unknown)."""
    from .agent_settings import SettingsError, model_name

    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MODELS_MAX:
        raise RegistryError(f"models must be a list of at most {MODELS_MAX} entries")
    models: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not set(entry) <= set(MODEL_FIELDS):
            raise RegistryError(f"each model is {{{', '.join(MODEL_FIELDS)}}}, all but the name optional")
        try:
            name = model_name(entry.get("name"), field="a model name")
        except SettingsError as exc:
            raise RegistryError(str(exc)) from None
        if not name:
            raise RegistryError("each model needs a name")
        if any(m["name"] == name for m in models):
            raise RegistryError(f"model {name!r} is listed twice")
        models.append({"name": name, "context_window": _context_window(entry.get("context_window")),
                       **{f: _price(entry.get(f), f) for f in PRICE_FIELDS}, "efforts": _efforts(entry.get("efforts"))})
    return models


def _model(raw: Any) -> str:
    from .agent_settings import SettingsError, model_name

    try:
        model = model_name(raw)
    except SettingsError as exc:
        raise RegistryError(str(exc)) from None
    if not model:
        raise RegistryError("an agent needs a model")
    return model


def _effort(raw: Any) -> str | None:
    from .agent_settings import SettingsError, effort_level

    try:
        return effort_level(raw) or None
    except SettingsError as exc:
        raise RegistryError(str(exc)) from None


def _api_key(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or _CONTROL.search(raw) or len(raw) > API_KEY_MAX_CHARS:
        raise RegistryError(f"api_key must be the key, at most {API_KEY_MAX_CHARS} characters, or null for none")
    return raw.strip()


def _listed(values: Iterable[Any]) -> str:
    return ", ".join(sorted(getattr(v, "value", str(v)) for v in values))


def check_pairing(runtime: RuntimeSpec, provider: ProviderSpec, model: str) -> None:
    """Refuse an agent of ``runtime``, ``provider`` and ``model`` that could not run (the module's rules)."""
    try:
        adapter = runtimes.adapter(runtime.type)
    except RuntimeUnsupported:
        raise RegistryError(f"runtime {runtime.name!r} is of type {runtime.type!r}, which this version cannot run") \
            from None
    if not adapter.protocols & provider.protocols:
        raise RegistryError(
            f"runtime {runtime.name!r} ({adapter.label}) speaks {_listed(adapter.protocols)} and provider "
            f"{provider.name!r} serves {_listed(provider.protocols)}: they share no protocol")
    if provider.auth is AuthKind.OAUTH and provider.oauth_runtime_type != runtime.type:
        raise RegistryError(
            f"provider {provider.name!r} is the login of {provider.oauth_runtime_type} runtimes: it pairs only with "
            f"them, not with runtime {runtime.name!r} ({runtime.type})")
    if provider.auth not in adapter.auth_kinds:
        raise RegistryError(
            f"runtime {runtime.name!r} ({adapter.label}) cannot be started with an {provider.auth.value} provider yet; "
            f"it takes {_listed(adapter.auth_kinds)} providers")
    if provider.models and model not in provider.models:
        raise RegistryError(f"provider {provider.name!r} lists the models {', '.join(provider.models)}; "
                            f"{model!r} is not one of them")


def check_roles(agent: AgentSpec, roles: Iterable[str]) -> None:
    """Refuse ``agent`` for a role its runtime lacks a capability for."""
    adapter = runtimes.ADAPTERS.get(agent.runtime.type)
    for role in roles:
        needs = runtimes.ROLE_CAPABILITIES[role]
        if missing := needs - agent.runtime.capabilities:
            label = adapter.label if adapter else agent.runtime.type
            raise RegistryError(
                f"agent {agent.name!r} cannot run the {role}: its runtime {agent.runtime.name!r} ({label}) lacks "
                f"{_listed(missing)}, which a {role} needs ({_listed(needs)})")


# The roles a layer of each scope, or a task's choice, selects an agent for.
_SCOPE_ROLES = {"system": tuple(runtimes.ROLE_CAPABILITIES), "project": ("planner", "worker", "reviewer")}


async def check_selection(agent_id: str, roles: Iterable[str]) -> AgentSpec:
    """The registered agent ``agent_id``, refused when it cannot run one of ``roles``."""
    from ..db import agent_registry as db

    row = await db.get_agent(agent_id) if isinstance(agent_id, str) else None
    if row is None:
        raise RegistryError(f"no agent {agent_id!r}: give the agent_id of a registered agent")
    agent = AgentSpec.from_row(row)
    if agent.runtime.type not in runtimes.ADAPTERS:
        raise RegistryError(f"agent {agent.name!r} runs on a {agent.runtime.type!r} runtime, which this version "
                            "cannot run")
    check_roles(agent, roles)
    return agent


async def _selected_roles(agent_id: str) -> list[str]:
    """Every role ``agent_id`` is selected for, by a layer or a task."""
    from ..db import agent_setting_layers

    roles: set[str] = set()
    for scope, role in await agent_setting_layers.selected_roles(agent_id):
        roles.update(_SCOPE_ROLES.get(scope, (role,)))
    return [role for role in runtimes.ROLE_CAPABILITIES if role in roles]


# --- Views ---


def runtime_spec(row: Mapping[str, Any]) -> RuntimeSpec:
    """A runtime row (``agent_runtimes``) as what an agent is checked with."""
    known = {c.value for c in Capability}
    return RuntimeSpec(row["id"], row["name"], row["type"], row["executable"],
                       frozenset(Capability(c) for c in row["capabilities"] or () if c in known))


def provider_spec(row: Mapping[str, Any]) -> ProviderSpec:
    """A provider row (``llm_providers``, or the fields of one) as what an agent is checked with."""
    return ProviderSpec(row["id"], row["name"], AuthKind(row["auth"]),
                        frozenset(ApiProtocol(p) for p in row["protocols"]), row["oauth_runtime_type"],
                        row["base_url"], tuple(m["name"] for m in row["models"]))


def type_view(adapter: runtimes.RuntimeAdapter) -> dict[str, Any]:
    return {"type": adapter.type, "label": adapter.label, "default_executable": adapter.default_executable,
            "protocols": sorted(p.value for p in adapter.protocols),
            "capabilities": sorted(c.value for c in adapter.capabilities),
            "auth_kinds": sorted(a.value for a in adapter.auth_kinds)}


def runtime_view(row: Mapping[str, Any]) -> dict[str, Any]:
    adapter = runtimes.ADAPTERS.get(row["type"])
    return {
        "runtime_id": row["id"], "name": row["name"], "type": row["type"],
        "type_label": adapter.label if adapter else row["type"], "executable": row["executable"],
        "default_executable": adapter.default_executable if adapter else None,
        "protocols": sorted(p.value for p in adapter.protocols) if adapter else [],
        "version": row["version"], "capabilities": list(row["capabilities"] or ()), "probed_at": row["probed_at"],
        "probe_error": row["probe_error"], "builtin": row["builtin"], "agent_count": row.get("agent_count", 0),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def provider_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": row["id"], "name": row["name"], "protocols": list(row["protocols"]), "auth": row["auth"],
        "oauth_runtime_type": row["oauth_runtime_type"], "base_url": row["base_url"], "models": row["models"],
        "max_concurrency": row["max_concurrency"],
        "api_key": SECRET_MASK if row.get("api_key_set") else None, "builtin": row["builtin"],
        "agent_count": row.get("agent_count", 0), "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def agent_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": row["id"], "name": row["name"], "model": row["model"], "effort": row.get("effort"),
        "runtime_id": row["runtime_id"], "runtime_name": row["runtime_name"], "runtime_type": row["runtime_type"],
        "provider_id": row["provider_id"], "provider_name": row["provider_name"], "builtin": row["builtin"],
    }


def _limits() -> dict[str, Any]:
    from .agent_settings import EFFORT_LEVELS

    return {
        "name_max_chars": NAME_MAX_CHARS, "models_max": MODELS_MAX, "price_fields": list(PRICE_FIELDS),
        "model_fields": list(MODEL_FIELDS),
        "protocols": [p.value for p in ApiProtocol], "auth_kinds": [a.value for a in AuthKind],
        "effort_levels": list(EFFORT_LEVELS),
        "role_capabilities": {role: sorted(c.value for c in needs) for role, needs in runtimes.ROLE_CAPABILITIES.items()},
        "secret_mask": SECRET_MASK,
    }


async def registry_view() -> dict[str, Any]:
    """The runtime types this version has, and every registered runtime, provider and agent."""
    from ..db import agent_registry as db

    return {
        "runtime_types": [type_view(a) for a in runtimes.ADAPTERS.values()],
        "runtimes": [runtime_view(r) for r in await db.list_runtimes()],
        "providers": [provider_view(r) for r in await db.list_providers()],
        "agents": [agent_view(r) for r in await db.list_agents()],
        "limits": _limits(),
    }


# --- Runtimes ---


def _probe_executable(runtime_type: str, executable: str, server_claude: str) -> str:
    """What a probe runs: the runtime's executable, else its type's default (the server's for Claude Code)."""
    if executable:
        return executable
    adapter = runtimes.adapter(runtime_type)
    return server_claude if adapter is runtimes.CLAUDE_CODE else adapter.default_executable


async def _probe(runtime_type: str, executable: str, server_claude: str) -> dict[str, Any]:
    adapter = runtimes.adapter(runtime_type)
    probe = await asyncio.to_thread(adapter.probe, _probe_executable(runtime_type, executable, server_claude))
    return {"version": probe.version, "capabilities": sorted(c.value for c in probe.capabilities),
            "probe_error": probe.error}


async def register_found_runtimes() -> list[str]:
    """Register a built-in runtime (``rt-<type>``, named after its type) for each runtime type other than Claude
    Code whose default executable is on PATH, probed once; one that exists already, or whose name is taken, is
    left alone. Claude Code's built-in runtime comes with the schema. Returns the names registered."""
    import shutil

    from ..db import agent_registry as db

    registered = []
    for adapter in runtimes.ADAPTERS.values():
        if adapter is runtimes.CLAUDE_CODE or shutil.which(adapter.default_executable) is None:
            continue
        if await db.get_runtime(f"rt-{adapter.type}") is not None:
            continue
        probe = await _probe(adapter.type, "", adapter.default_executable)
        if await db.insert_builtin_runtime(f"rt-{adapter.type}", {"name": adapter.type, "type": adapter.type,
                                                                   "executable": "", **probe}):
            registered.append(adapter.type)
    return registered


async def _existing_runtime(runtime_id: str) -> dict[str, Any]:
    from ..db import agent_registry as db

    row = await db.get_runtime(runtime_id)
    if row is None:
        raise RegistryNotFound(f"no runtime {runtime_id!r}")
    return row


def _not_builtin(row: Mapping[str, Any], what: str) -> None:
    if row["builtin"]:
        raise RegistryError(f"the built-in {what} {row['name']!r} cannot be changed or removed; register another one")


async def _runtime_answer(runtime_id: str) -> dict[str, Any]:
    from ..db import agent_registry as db

    rows = {r["id"]: r for r in await db.list_runtimes()}
    return {"runtime": runtime_view(rows[runtime_id])}


async def create_runtime(data: Any, *, server_claude: str = "claude") -> dict[str, Any]:
    """Register a runtime ``{name, type, executable?}`` and probe it at once."""
    import asyncpg

    from ..db import agent_registry as db

    data = _fields(data, ("name", "type", "executable"))
    name = entry_name(data.get("name"), "runtime")
    adapter = _runtime_type(data.get("type"))
    executable = _executable(data.get("executable"))
    probe = await _probe(adapter.type, executable, server_claude)
    try:
        runtime_id = await db.insert_runtime({"name": name, "type": adapter.type, "executable": executable, **probe})
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"a runtime named {name!r} already exists") from None
    return await _runtime_answer(runtime_id)


async def update_runtime(runtime_id: str, data: Any, *, server_claude: str = "claude") -> dict[str, Any]:
    """Rename a runtime or change its executable (which probes it again); its type stays."""
    import asyncpg

    from ..db import agent_registry as db

    data = _fields(data, ("name", "executable"))
    if not data:
        raise RegistryError("give at least one of name, executable")
    row = await _existing_runtime(runtime_id)
    _not_builtin(row, "runtime")
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = entry_name(data["name"], "runtime")
    if "executable" in data:
        fields["executable"] = _executable(data["executable"])
        if fields["executable"] != row["executable"]:
            fields.update(await _probe(row["type"], fields["executable"], server_claude))
    try:
        await db.update_runtime(runtime_id, fields)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"a runtime named {fields['name']!r} already exists") from None
    return await _runtime_answer(runtime_id)


async def probe_runtime(runtime_id: str, *, server_claude: str = "claude") -> dict[str, Any]:
    """Run ``<executable> --version`` again and record what it found; the built-in runtime too."""
    from ..db import agent_registry as db

    row = await _existing_runtime(runtime_id)
    try:
        probe = await _probe(row["type"], row["executable"], server_claude)
    except RuntimeUnsupported as exc:
        raise RegistryError(str(exc)) from None
    await db.update_runtime(runtime_id, probe)
    return await _runtime_answer(runtime_id)


async def delete_runtime(runtime_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import agent_registry as db

    row = await _existing_runtime(runtime_id)
    _not_builtin(row, "runtime")
    try:
        await db.delete_runtime(runtime_id)
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict(f"runtime {row['name']!r} still runs agents: change or remove them first") from None
    return {"deleted": runtime_id}


# --- Providers ---

_PROVIDER_FIELDS = ("name", "protocols", "auth", "oauth_runtime_type", "base_url", "models", "max_concurrency",
                    "api_key")


async def _existing_provider(provider_id: str, conn: Any = None) -> dict[str, Any]:
    from ..db import agent_registry as db

    row = await db.get_provider(provider_id, pool=conn)
    if row is None:
        raise RegistryNotFound(f"no provider {provider_id!r}")
    return row


def _provider_fields(data: Mapping[str, Any], current: Mapping[str, Any] | None) -> tuple[dict[str, Any], Any]:
    """The provider's columns after the request, validated, and what to do with its key: ``...`` keep it, None
    remove it, or the new key."""
    merged = {name: data[name] if name in data else (current or {}).get(name) for name in _PROVIDER_FIELDS[:-1]}
    try:
        auth = AuthKind(merged["auth"])
    except ValueError:
        raise RegistryError(f"auth must be one of {', '.join(a.value for a in AuthKind)}") from None
    fields: dict[str, Any] = {
        "name": entry_name(merged["name"], "provider"),
        "protocols": _protocols(list(merged["protocols"]) if merged["protocols"] is not None else None),
        "auth": auth.value,
        "base_url": _base_url(merged["base_url"]),
        "models": _models(merged["models"]),
        "max_concurrency": _max_concurrency(merged["max_concurrency"]),
    }
    key: Any = ...
    if auth is AuthKind.OAUTH:
        adapter = _runtime_type(merged["oauth_runtime_type"])
        if AuthKind.OAUTH not in adapter.auth_kinds:
            raise RegistryError(f"{adapter.label} has no login of its own to bind a provider to")
        if fields["base_url"]:
            raise RegistryError("an OAuth provider uses its runtime's own endpoint: leave base_url empty")
        if data.get("api_key") not in (None, SECRET_MASK):
            raise RegistryError("an OAuth provider signs in with its runtime's login and takes no api_key")
        fields["oauth_runtime_type"] = adapter.type
        key = None if current is not None and current.get("api_key_set") else ...
    else:
        if merged["oauth_runtime_type"] is not None and "oauth_runtime_type" in data:
            raise RegistryError("only an OAuth provider names an oauth_runtime_type")
        fields["oauth_runtime_type"] = None
        if "api_key" in data and data["api_key"] != SECRET_MASK:
            key = None if data["api_key"] in (None, "") else _api_key(data["api_key"])
    return fields, key


async def _check_provider_agents(conn: Any, provider: Mapping[str, Any]) -> None:
    """Refuse a provider change that would leave one of its agents unable to run."""
    from ..db import agent_registry as db

    spec = provider_spec(provider)
    for row in await db.list_agents(pool=conn, provider_id=provider["id"]):
        agent = AgentSpec.from_row(row)
        try:
            check_pairing(agent.runtime, spec, agent.model)
        except RegistryError as exc:
            raise RegistryError(f"agent {agent.name!r} uses this provider: {exc}") from None


async def _provider_answer(provider_id: str) -> dict[str, Any]:
    from ..db import agent_registry as db

    rows = {r["id"]: r for r in await db.list_providers()}
    return {"provider": provider_view(rows[provider_id])}


async def create_provider(data: Any) -> dict[str, Any]:
    """Register ``{name, protocols, auth, oauth_runtime_type?, base_url?, models?, max_concurrency?, api_key?}``."""
    import asyncpg

    from ..db import agent_registry as db
    from ..db.connection import transaction

    data = _fields(data, _PROVIDER_FIELDS)
    fields, key = _provider_fields(data, None)
    try:
        async with transaction() as conn:
            provider_id = await db.insert_provider(conn, fields, None if key is ... else key)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"a provider named {fields['name']!r} already exists") from None
    return await _provider_answer(provider_id)


async def update_provider(provider_id: str, data: Any) -> dict[str, Any]:
    """Change a provider; ``api_key`` ``***`` keeps the stored key, null removes it. Its agents must still run."""
    import asyncpg

    from ..db import agent_registry as db
    from ..db.connection import transaction

    data = _fields(data, _PROVIDER_FIELDS)
    if not data:
        raise RegistryError(f"give at least one of {', '.join(_PROVIDER_FIELDS)}")
    try:
        async with transaction() as conn:
            await conn.execute("SELECT 1 FROM llm_providers WHERE id = $1 FOR UPDATE", provider_id)
            current = await _existing_provider(provider_id, conn)
            _not_builtin(current, "provider")
            fields, key = _provider_fields(data, current)
            await db.update_provider(conn, provider_id, fields, api_key=None if key is ... else key,
                                     keep_key=key is ...)
            await _check_provider_agents(conn, {**fields, "id": provider_id})
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"a provider named {data.get('name')!r} already exists") from None
    return await _provider_answer(provider_id)


async def delete_provider(provider_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import agent_registry as db

    row = await _existing_provider(provider_id)
    _not_builtin(row, "provider")
    try:
        await db.delete_provider(provider_id)
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict(f"provider {row['name']!r} still serves agents: change or remove them first") from None
    return {"deleted": provider_id}


# --- Agents ---

_AGENT_FIELDS = ("name", "runtime_id", "provider_id", "model", "effort")


async def _existing_agent(agent_id: str) -> dict[str, Any]:
    from ..db import agent_registry as db

    row = await db.get_agent(agent_id)
    if row is None:
        raise RegistryNotFound(f"no agent {agent_id!r}")
    return row


async def _parts(runtime_id: Any, provider_id: Any) -> tuple[RuntimeSpec, ProviderSpec]:
    from ..db import agent_registry as db

    runtime = await db.get_runtime(runtime_id) if isinstance(runtime_id, str) else None
    if runtime is None:
        raise RegistryError(f"no runtime {runtime_id!r}: give the runtime_id of a registered runtime")
    provider = await db.get_provider(provider_id) if isinstance(provider_id, str) else None
    if provider is None:
        raise RegistryError(f"no provider {provider_id!r}: give the provider_id of a registered provider")
    return runtime_spec(runtime), provider_spec(provider)


async def create_agent(data: Any) -> dict[str, Any]:
    """Register ``{name, runtime_id, provider_id, model, effort?}``; refused when it could not run."""
    import asyncpg

    from ..db import agent_registry as db

    data = _fields(data, _AGENT_FIELDS)
    fields = {"name": entry_name(data.get("name"), "agent"), "runtime_id": data.get("runtime_id"),
              "provider_id": data.get("provider_id"), "model": _model(data.get("model")),
              "effort": _effort(data.get("effort"))}
    runtime, provider = await _parts(fields["runtime_id"], fields["provider_id"])
    check_pairing(runtime, provider, fields["model"])
    try:
        agent_id = await db.insert_agent(fields)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"an agent named {fields['name']!r} already exists") from None
    return {"agent": agent_view(await _existing_agent(agent_id))}


async def update_agent(agent_id: str, data: Any) -> dict[str, Any]:
    """Change an agent; the result is checked like a new one."""
    import asyncpg

    from ..db import agent_registry as db

    data = _fields(data, _AGENT_FIELDS)
    if not data:
        raise RegistryError(f"give at least one of {', '.join(_AGENT_FIELDS)}")
    row = await _existing_agent(agent_id)
    _not_builtin(row, "agent")
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = entry_name(data["name"], "agent")
    if "model" in data:
        fields["model"] = _model(data["model"])
    if "effort" in data:
        fields["effort"] = _effort(data["effort"])
    for name in ("runtime_id", "provider_id"):
        if name in data:
            fields[name] = data[name]
    runtime, provider = await _parts(fields.get("runtime_id", row["runtime_id"]),
                                     fields.get("provider_id", row["provider_id"]))
    model = fields.get("model", row["model"])
    check_pairing(runtime, provider, model)
    check_roles(AgentSpec(agent_id, fields.get("name", row["name"]), runtime, provider, model),
                await _selected_roles(agent_id))
    try:
        await db.update_agent(agent_id, fields)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"an agent named {fields['name']!r} already exists") from None
    return {"agent": agent_view(await _existing_agent(agent_id))}


async def delete_agent(agent_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import agent_registry as db

    row = await _existing_agent(agent_id)
    _not_builtin(row, "agent")
    try:
        await db.delete_agent(agent_id)
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict(f"agent {row['name']!r} is still selected: select another agent there first") \
            from None
    return {"deleted": agent_id}
