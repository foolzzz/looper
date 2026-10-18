"""Runtimes, LLM providers and Agent Profiles: what they are, how they are checked, and their API (RFC decision 10).

An **Agent Profile** is a runtime, an LLM provider and a model, with a default
effort and, on Claude Code, whether its runs have agent teams (on unless turned
off, ``default_agent_teams``); a running agent (the Manager, a Planner, a step's Worker) runs the
profile its role selects. A **runtime** is a runtime type
(an adapter in ``looper.agents.runtimes``) found on this machine: the
executable it runs and what its probe found (``<executable> --version``, never
a model call). Runtimes are built in, never registered by hand: one per type
whose executable is on the server's PATH (``detect_runtime``). A runtime row
of another kind, registered by hand before, stays read-only while a profile
uses it (the schema drops the unused ones once). A **provider** is where an
agent's requests go: the protocols it serves (``anthropic-messages``,
``openai-responses``, ``openai-chat``), its ``base_url``, its model catalog
(each model's context window, prices, effort levels and the background model
its runtime's side requests use, each unknown until given), environment
variables of its own for the runs that use it (``env``), how many runs it takes
at once (``max_concurrency``, none for no limit) and how it authenticates: a
runtime type's own login (``oauth``, bound to that type) or an API key, which
is stored apart (``llm_provider_secrets``), shown as ``***`` and read only
when a run starts (``agent_settings.load_agent_settings``). A run is priced at
its provider's prices, the catalog's or else the built-in table's
(``runtimes.pricing``), unless its runtime reports the right figure itself;
the concurrency limit is stored and checked only. Presets
(``provider_presets``) fill a new provider for a known vendor.

The built-ins exist on every installation and are read-only: the
``claude-code`` runtime (the server's configured ``claude``), the
``claude-subscription`` provider (Claude Code's own login), the
``codex-login`` provider (Codex's own ChatGPT login, ``runtimes.codex_login``:
its view carries the login's state and the models to suggest), the
``grok-login`` provider (Grok's own login, ``runtimes.grok_login``, likewise)
and the ``claude-code/opus`` profile, which every role runs when no layer
selects another (``BUILTIN_PROFILE``). A runtime of another type is built in
too once ``looper serve`` finds its executable on PATH
(``register_found_runtimes``), or Re-detect does (``probe_runtime``):
``codex`` for Codex, ``omp`` for OMP, ``grok`` for Grok.

A profile is checked when it is created or changed, and again when a provider
it uses changes (``check_pairing``):

- its runtime's type is one this version can run;
- the runtime speaks a protocol the provider serves;
- an OAuth provider pairs only with runtimes of the type whose login it is;
- the runtime can be started with that kind of provider now
  (``RuntimeAdapter.auth_kinds``);
- the model is one the provider lists, when it lists any;
- agent teams are on only with a Claude Code runtime (``check_agent_teams``);
- replacing the default system prompt needs an adapter that supports it (``check_system_prompt_mode``).

Where a profile is selected (a layer, or a task's choice for one of its roles),
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
from dataclasses import dataclass, field, fields
from typing import Any

from . import runtimes
from .runtimes import ApiProtocol, AuthKind, Capability, RuntimeUnsupported
from .system_prompt_mode import SystemPromptMode

NAME_MAX_CHARS = 240
EXECUTABLE_MAX_CHARS = 1_000
BASE_URL_MAX_CHARS = 2_000
API_KEY_MAX_CHARS = 4_000
MODELS_MAX = 100
PRICE_MAX_USD = 10_000
PRICE_FIELDS = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")
MODEL_FIELDS = ("name", "context_window", *PRICE_FIELDS, "efforts", "background_model")
CONTEXT_WINDOW_MAX = 100_000_000
EFFORTS_MAX = 10
CONCURRENCY_MAX = 1_000
# What the API shows, and takes back unchanged, in place of a stored API key.
SECRET_MASK = "***"


class RegistryError(ValueError):
    """A request the registry refuses (answered with 400)."""


class RegistryNotFound(LookupError):
    """No such runtime, provider or Agent Profile (404)."""


class RegistryConflict(Exception):
    """A name in use, or a row still referred to (409)."""


# --- What an Agent Profile is made of ---


@dataclass(frozen=True)
class RuntimeSpec:
    id: str
    name: str
    type: str
    executable: str = ""  # '' = the type's default (for Claude Code, the server's configured claude)
    capabilities: frozenset[Capability] = frozenset()


@dataclass(frozen=True)
class CatalogModel:
    """One model of a provider's catalog; every field but the name None when unknown. The background model is
    the one a runtime's side requests (titles, summaries, quick subagents) use with this model; None: the model
    itself."""

    name: str
    context_window: int | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cache_read_usd_per_mtok: float | None = None
    cache_write_usd_per_mtok: float | None = None
    efforts: tuple[str, ...] | None = None
    background_model: str | None = None

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> CatalogModel:
        """A stored catalog entry (``_models``), as read back."""
        known = {item.name for item in fields(cls)}
        values = {name: entry.get(name) for name in known if entry.get(name) is not None}
        if values.get("efforts") is not None:
            values["efforts"] = tuple(values["efforts"])
        return cls(**{"name": str(entry.get("name")), **{k: v for k, v in values.items() if k != "name"}})


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    auth: AuthKind
    protocols: frozenset[ApiProtocol]
    oauth_runtime_type: str | None = None
    base_url: str = ""
    catalog: tuple[CatalogModel, ...] = ()  # the models it lists; () = any
    env: tuple[tuple[str, str], ...] = ()  # its own environment variables, for the runs that use it
    # Its API key: set only on the launch path (agent_settings.load_agent_settings), for the runtime's binding.
    # Never shown: not in the repr, not compared, never in a record, a view or an export.
    api_key: str | None = field(default=None, repr=False, compare=False)

    @property
    def models(self) -> tuple[str, ...]:
        """The names of the models it lists; () = any."""
        return tuple(model.name for model in self.catalog)

    def catalog_model(self, name: str) -> CatalogModel | None:
        return next((model for model in self.catalog if model.name == name), None)


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    runtime: RuntimeSpec
    provider: ProviderSpec
    model: str
    effort: str | None = None  # its default effort; a layer's effort wins
    agent_teams: bool = False  # Claude Code's agent teams in its runs (runtimes.claude_code)
    system_prompt_mode: SystemPromptMode = SystemPromptMode.APPEND
    custom_prompt: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AgentProfile:
        """A profile from ``db.profile_registry.PROFILE_JSON``."""
        known = {c.value for c in Capability}
        return cls(
            id=row["id"], name=row["name"], model=row["model"], effort=row.get("effort"),
            agent_teams=bool(row.get("agent_teams")),
            system_prompt_mode=SystemPromptMode(row.get("system_prompt_mode", "append")),
            custom_prompt=row.get("custom_prompt", ""),
            runtime=RuntimeSpec(row["runtime_id"], row["runtime_name"], row["runtime_type"],
                                row.get("runtime_executable") or "",
                                frozenset(Capability(c) for c in row.get("runtime_capabilities") or () if c in known)),
            provider=ProviderSpec(
                row["provider_id"], row["provider_name"], AuthKind(row["provider_auth"]),
                frozenset(ApiProtocol(p) for p in row.get("provider_protocols") or ()),
                row.get("provider_oauth_runtime_type"), row.get("provider_base_url") or "",
                _catalog(row.get("provider_models")), _env(row.get("provider_env")),
            ),
        )

    def record(self) -> dict[str, Any]:
        """Its entry in a run's record: what it is made of, by id and name."""
        return {
            "id": self.id, "name": self.name, "model": self.model, "custom_prompt": self.custom_prompt,
            "runtime": {"id": self.runtime.id, "name": self.runtime.name, "type": self.runtime.type},
            "provider": {"id": self.provider.id, "name": self.provider.name, "auth": self.provider.auth.value},
        }


def _catalog(raw: Any) -> tuple[CatalogModel, ...]:
    return tuple(CatalogModel.from_entry(m) for m in raw or () if isinstance(m, Mapping))


def _env(raw: Any) -> tuple[tuple[str, str], ...]:
    return tuple((str(e["key"]), str(e["value"])) for e in raw or () if isinstance(e, Mapping))


BUILTIN_RUNTIME = RuntimeSpec("rt-claude-code", "claude-code", "claude-code", "", runtimes.CLAUDE_CODE.capabilities)
BUILTIN_PROVIDER = ProviderSpec("prov-claude-subscription", "claude-subscription", AuthKind.OAUTH,
                                frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), "claude-code")
BUILTIN_PROFILE = AgentProfile("agent-claude-code-opus", "claude-code/opus", BUILTIN_RUNTIME, BUILTIN_PROVIDER,
                               "opus", agent_teams=True)


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
        try:
            background = model_name(entry.get("background_model"), field="background_model") or None
        except SettingsError as exc:
            raise RegistryError(str(exc)) from None
        models.append({"name": name, "context_window": _context_window(entry.get("context_window")),
                       **{f: _price(entry.get(f), f) for f in PRICE_FIELDS}, "efforts": _efforts(entry.get("efforts")),
                       "background_model": background})
    return models


def _provider_env(raw: Any) -> list[dict[str, str]]:
    """A provider's own environment variables, ``[{"key", "value"}]``: like the settings' (no credentials, which
    belong in ``api_key``), and none that a runtime's provider binding owns (``provider_env_owned``)."""
    from .agent_settings import SettingsError, agent_teams_env, environment

    if raw is None:
        return []
    try:
        env = environment(raw)
        agent_teams_env(env)
    except SettingsError as exc:
        raise RegistryError(f"env: {exc}") from None
    for adapter in runtimes.ADAPTERS.values():
        if owned := sorted(set(env) & adapter.provider_env_owned):
            raise RegistryError(
                f"env: {owned[0]} is set by the {adapter.label} binding from the provider's base_url, api_key and "
                "model catalog (a model and its background_model); set it there")
    return [{"key": key, "value": str(value)} for key, value in env.items()]


def _model(raw: Any) -> str:
    from .agent_settings import SettingsError, model_name

    try:
        model = model_name(raw)
    except SettingsError as exc:
        raise RegistryError(str(exc)) from None
    if not model:
        raise RegistryError("an Agent Profile needs a model")
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
    """Refuse a profile of ``runtime``, ``provider`` and ``model`` that could not run (the module's rules)."""
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


def default_agent_teams(runtime: RuntimeSpec) -> bool:
    """Whether a profile of ``runtime`` that does not say has agent teams: on for Claude Code, whose feature they are,
    off for any other runtime."""
    return runtime.type == runtimes.CLAUDE_CODE.type


def check_agent_teams(runtime: RuntimeSpec, agent_teams: bool) -> None:
    """Refuse agent teams on a runtime other than Claude Code's, whose feature they are."""
    if agent_teams and runtime.type != runtimes.CLAUDE_CODE.type:
        raise RegistryError(f"agent teams are a Claude Code feature: runtime {runtime.name!r} is of type "
                            f"{runtime.type!r}; turn agent_teams off or choose a Claude Code runtime")


def _agent_teams(raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise RegistryError("agent_teams must be true or false")
    return raw


def _system_prompt_mode(raw: Any) -> SystemPromptMode:
    try:
        return SystemPromptMode(raw)
    except (ValueError, TypeError):
        raise RegistryError('system_prompt_mode must be "append" or "replace"') from None


def _custom_prompt(raw: Any) -> str:
    from .agent_settings import HARNESS_MAX_CHARS

    if not isinstance(raw, str):
        raise RegistryError("custom_prompt must be a string")
    text = raw.strip()
    if len(text) > HARNESS_MAX_CHARS:
        raise RegistryError(f"custom_prompt has at most {HARNESS_MAX_CHARS} characters, got {len(text)}")
    return text


def check_system_prompt_mode(runtime: RuntimeSpec, mode: SystemPromptMode) -> None:
    """Refuse replacement unless the profile's final runtime explicitly supports it."""
    if mode is not SystemPromptMode.REPLACE:
        return
    adapter = runtimes.ADAPTERS.get(runtime.type)
    if adapter is None:
        raise RegistryError(f"runtime {runtime.name!r} ({runtime.type}) cannot replace its default system prompt: "
                            "its runtime type is unknown")
    if refusal := runtimes.replace_refusal(adapter):
        raise RegistryError(f"runtime {runtime.name!r}: {refusal}")


def check_roles(profile: AgentProfile, roles: Iterable[str]) -> None:
    """Refuse ``profile`` for a role its runtime lacks a capability for."""
    adapter = runtimes.ADAPTERS.get(profile.runtime.type)
    for role in roles:
        needs = runtimes.ROLE_CAPABILITIES[role]
        if missing := needs - profile.runtime.capabilities:
            label = adapter.label if adapter else profile.runtime.type
            raise RegistryError(
                f"Agent Profile {profile.name!r} cannot run the {role}: its runtime {profile.runtime.name!r} "
                f"({label}) lacks {_listed(missing)}, which a {role} needs ({_listed(needs)})")


# The roles a layer of each scope, or a task's choice, selects a profile for.
_SCOPE_ROLES = {"system": tuple(runtimes.ROLE_CAPABILITIES), "project": ("planner", "worker", "reviewer")}


async def check_selection(profile_id: str, roles: Iterable[str]) -> AgentProfile:
    """The Agent Profile ``profile_id``, refused when it cannot run one of ``roles``."""
    from ..db import profile_registry as db

    row = await db.get_profile(profile_id) if isinstance(profile_id, str) else None
    if row is None:
        raise RegistryError(f"no Agent Profile {profile_id!r}: give the profile_id of a registered one")
    profile = AgentProfile.from_row(row)
    if profile.runtime.type not in runtimes.ADAPTERS:
        raise RegistryError(f"Agent Profile {profile.name!r} runs on a {profile.runtime.type!r} runtime, which this "
                            "version cannot run")
    check_roles(profile, roles)
    return profile


async def _selected_roles(profile_id: str) -> list[str]:
    """Every role ``profile_id`` is selected for, by a layer or a task."""
    from ..db import agent_setting_layers

    roles: set[str] = set()
    for scope, role in await agent_setting_layers.selected_roles(profile_id):
        roles.update(_SCOPE_ROLES.get(scope, (role,)))
    return [role for role in runtimes.ROLE_CAPABILITIES if role in roles]


# --- Views ---


def runtime_spec(row: Mapping[str, Any]) -> RuntimeSpec:
    """A runtime row (``agent_runtimes``) as what a profile is checked with."""
    known = {c.value for c in Capability}
    return RuntimeSpec(row["id"], row["name"], row["type"], row["executable"],
                       frozenset(Capability(c) for c in row["capabilities"] or () if c in known))


def provider_spec(row: Mapping[str, Any]) -> ProviderSpec:
    """A provider row (``llm_providers``, or the fields of one) as what a profile is checked with."""
    return ProviderSpec(row["id"], row["name"], AuthKind(row["auth"]),
                        frozenset(ApiProtocol(p) for p in row["protocols"]), row["oauth_runtime_type"],
                        row["base_url"], _catalog(row["models"]), _env(row.get("env")))


def servable_roles(capabilities: Iterable[Any]) -> list[str]:
    """The roles a runtime with ``capabilities`` (``Capability`` members or their values) can run, in order."""
    have = {getattr(c, "value", c) for c in capabilities}
    return [role for role, needs in runtimes.ROLE_CAPABILITIES.items() if {c.value for c in needs} <= have]


def type_view(adapter: runtimes.RuntimeAdapter) -> dict[str, Any]:
    return {"type": adapter.type, "label": adapter.label, "default_executable": adapter.default_executable,
            "runtime_id": builtin_runtime_id(adapter),
            "protocols": sorted(p.value for p in adapter.protocols),
            "capabilities": sorted(c.value for c in adapter.capabilities),
            "roles": servable_roles(adapter.capabilities),
            "can_replace_system_prompt": adapter.can_replace_system_prompt,
            "replace_system_prompt_refusal": runtimes.replace_refusal(adapter),
            "auth_kinds": sorted(a.value for a in adapter.auth_kinds)}


def builtin_runtime_id(adapter: runtimes.RuntimeAdapter) -> str:
    """The id of the built-in runtime of ``adapter``'s type: ``rt-<type>``."""
    return f"rt-{adapter.type}"


def runtime_path(runtime_type: str, executable: str, server_claude: str) -> str | None:
    """Where the runtime's executable is on the server's PATH (or the absolute path it names); None when it is
    not found."""
    import shutil

    try:
        command = _probe_executable(runtime_type, executable, server_claude)
    except RuntimeUnsupported:
        command = executable
    return shutil.which(command) if command else None


def runtime_view(row: Mapping[str, Any], *, server_claude: str = "claude") -> dict[str, Any]:
    adapter = runtimes.ADAPTERS.get(row["type"])
    return {
        "runtime_id": row["id"], "name": row["name"], "type": row["type"],
        "type_label": adapter.label if adapter else row["type"], "executable": row["executable"],
        "default_executable": adapter.default_executable if adapter else None,
        "path": runtime_path(row["type"], row["executable"], server_claude),
        "protocols": sorted(p.value for p in adapter.protocols) if adapter else [],
        "version": row["version"], "capabilities": list(row["capabilities"] or ()),
        "roles": servable_roles(row["capabilities"] or ()), "probed_at": row["probed_at"],
        "probe_error": row["probe_error"], "builtin": row["builtin"], "profile_count": row.get("profile_count", 0),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def catalog_entries(models: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stored catalog entries with every field (one stored before a field existed lacks it: null, unknown)."""
    return [{**dict.fromkeys(MODEL_FIELDS), **m} for m in models]


def provider_view(row: Mapping[str, Any], logins: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """A provider; an OAuth one with the models its runtime suggests for the login (``suggested_models``) and the
    login's state when it is known (``login``, from ``logins`` by runtime type: ``{"state", "detail"}``)."""
    adapter = runtimes.ADAPTERS.get(row["oauth_runtime_type"] or "")
    oauth = row["auth"] == AuthKind.OAUTH.value and adapter is not None
    return {
        "provider_id": row["id"], "name": row["name"], "protocols": list(row["protocols"]), "auth": row["auth"],
        "oauth_runtime_type": row["oauth_runtime_type"], "base_url": row["base_url"],
        "models": catalog_entries(row["models"]),
        "suggested_models": list(adapter.login_models) if oauth and adapter else [],
        "login": dict(logins[adapter.type]) if oauth and adapter and adapter.type in (logins or {}) else None,
        "env": list(row.get("env") or []), "max_concurrency": row["max_concurrency"],
        "api_key": SECRET_MASK if row.get("api_key_set") else None, "builtin": row["builtin"],
        "profile_count": row.get("profile_count", 0), "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _login_modules() -> dict[str, Any]:
    """The runtimes whose own login Looper links into its homes, by type, and the module of each."""
    from .runtimes import codex_login, grok_login

    return {"codex": codex_login, "grok": grok_login}


async def _logins(providers: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """The state of each runtime login a listed OAuth provider stands for, by runtime type (Codex's and Grok's:
    their module's ``login_view``; Claude Code's comes from the startup self-check)."""
    types = {row["oauth_runtime_type"] for row in providers if row["auth"] == AuthKind.OAUTH.value}
    return {kind: await module.login_view() for kind, module in _login_modules().items() if kind in types}


def cost_basis(profile: AgentProfile) -> dict[str, Any]:
    """How a profile's runs are priced: by its runtime (``runtime``), or by Looper at its provider's prices
    (``provider_price``), with the models of its runs that have no full price (``unpriced``: those runs record
    tokens only and no spend cap applies to them); ``equivalent`` when the figure is what a subscription's usage
    would cost at API prices, not money spent."""
    from .runtimes.pricing import model_price

    adapter = runtimes.ADAPTERS.get(profile.runtime.type)
    if adapter is None:
        return {"source": None, "equivalent": False, "unpriced": []}
    source = adapter.cost_source(profile.provider)
    unpriced: list[str] = []
    if source is runtimes.CostSource.PROVIDER_PRICE:
        entry = profile.provider.catalog_model(profile.model)
        for model in dict.fromkeys([profile.model, *([entry.background_model] if entry and entry.background_model
                                                     else [])]):
            if not model_price(profile.provider, model).complete:
                unpriced.append(model)
    return {"source": source.value, "equivalent": profile.provider.auth is AuthKind.OAUTH, "unpriced": unpriced}


def cost_warnings(profile: AgentProfile) -> list[str]:
    """What registering ``profile`` should warn about: models it would run without a full price, and a runtime
    whose own cost figure no per-run cap can stop (``RuntimeAdapter.runtime_cost_capped``)."""
    warnings = [f"model {model!r} has no full price with provider {profile.provider.name!r} (input, output, cache "
                "read and cache write): its runs record tokens only and spend caps do not apply to them; add its "
                "prices to the provider's model catalog" for model in cost_basis(profile)["unpriced"]]
    adapter = runtimes.ADAPTERS.get(profile.runtime.type)
    if adapter is not None and adapter.cost_source(profile.provider) is runtimes.CostSource.RUNTIME \
            and not adapter.runtime_cost_capped:
        warnings.append(f"{adapter.label} reports a run's cost only when the run ends and has no spend cap of its "
                        "own: per-run spend caps do not stop its runs; task spend caps count what they cost")
    return warnings


# Where nothing selects a profile.
NOT_USED: Mapping[str, Any] = {"layers": [], "task_count": 0, "schedule_count": 0, "allowed_in": []}


def profile_view(row: Mapping[str, Any], used_by: Mapping[str, Any] = NOT_USED) -> dict[str, Any]:
    """An Agent Profile, the roles its runtime can run and where it is selected (``used_by``: the layers, each
    ``{scope, role, project_id, project_name}``, how many tasks and schedules chose it, and the projects whose
    Planners may choose it; ``db.profile_usage``)."""
    spec = AgentProfile.from_row(row)
    return {
        "profile_id": row["id"], "name": row["name"], "model": row["model"], "effort": row.get("effort"),
        "runtime_id": row["runtime_id"], "runtime_name": row["runtime_name"], "runtime_type": row["runtime_type"],
        "provider_id": row["provider_id"], "provider_name": row["provider_name"], "builtin": row["builtin"],
        "agent_teams": spec.agent_teams,
        "system_prompt_mode": spec.system_prompt_mode.value,
        "custom_prompt": spec.custom_prompt,
        "roles": servable_roles(spec.runtime.capabilities), "used_by": dict(used_by),
        "cost": cost_basis(spec),
    }


def usage_text(used_by: Mapping[str, Any]) -> str:
    """Where a profile is selected, in words: "the defaults of every role, the worker role, 2 tasks"."""
    scopes = {"system": lambda u: "the defaults of every role", "role": lambda u: f"the {u['role']} role",
              "project": lambda u: f"project {u['project_name']!r}",
              "role_project": lambda u: f"the {u['role']} in project {u['project_name']!r}"}
    parts = [scopes[u["scope"]](u) for u in used_by["layers"]]
    if count := used_by["task_count"]:
        parts.append(f"{count} {'task' if count == 1 else 'tasks'}")
    if count := used_by.get("schedule_count"):
        parts.append(f"{count} {'schedule' if count == 1 else 'schedules'}")
    parts += [f"the profiles the Planners of project {name!r} may choose" for name in used_by.get("allowed_in") or ()]
    return ", ".join(parts)


def _limits() -> dict[str, Any]:
    from .agent_settings import EFFORT_LEVELS

    return {
        "name_max_chars": NAME_MAX_CHARS, "models_max": MODELS_MAX, "price_fields": list(PRICE_FIELDS),
        "model_fields": list(MODEL_FIELDS),
        "provider_env_owned": sorted(set().union(*(a.provider_env_owned for a in runtimes.ADAPTERS.values()))),
        "protocols": [p.value for p in ApiProtocol], "auth_kinds": [a.value for a in AuthKind],
        "effort_levels": list(EFFORT_LEVELS),
        "role_capabilities": {role: sorted(c.value for c in needs) for role, needs in runtimes.ROLE_CAPABILITIES.items()},
        "secret_mask": SECRET_MASK,
    }


async def registry_view(*, server_claude: str = "claude") -> dict[str, Any]:
    """The runtime types this version has, and every runtime, provider and Agent Profile (with where it is used)."""
    from ..db import profile_registry as db

    from .provider_presets import presets_view
    from .runtimes.pricing import BUILTIN_PRICES, PRICE_SOURCES, PRICES_AS_OF

    uses = await db.profile_usage()
    providers = await db.list_providers()
    logins = await _logins(providers)
    return {
        "runtime_types": [type_view(a) for a in runtimes.ADAPTERS.values()],
        "runtimes": [runtime_view(r, server_claude=server_claude) for r in await db.list_runtimes()],
        "providers": [provider_view(r, logins) for r in providers],
        "profiles": [profile_view(r, uses.get(r["id"], NOT_USED)) for r in await db.list_profiles()],
        "provider_presets": presets_view(),
        "prices": {"as_of": PRICES_AS_OF, "sources": list(PRICE_SOURCES),
                   "models": {name: price.view() for name, price in BUILTIN_PRICES.items()}},
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


async def _detect(adapter: runtimes.RuntimeAdapter) -> bool:
    """Register the built-in runtime of ``adapter``'s type (``rt-<type>``, named after its type), probed once,
    when its default executable is on PATH and neither it nor a runtime of its name exists; whether it did."""
    import shutil

    from ..db import profile_registry as db

    if shutil.which(adapter.default_executable) is None:
        return False
    if await db.get_runtime(builtin_runtime_id(adapter)) is not None:
        return False
    probe = await _probe(adapter.type, "", adapter.default_executable)
    return await db.insert_builtin_runtime(builtin_runtime_id(adapter), {
        "name": adapter.type, "type": adapter.type, "executable": "", **probe})


async def register_found_runtimes() -> list[str]:
    """Register a built-in runtime for each runtime type other than Claude Code whose default executable is on
    PATH (``_detect``); one that exists already, or whose name is taken, is left alone. Claude Code's built-in
    runtime comes with the schema. Returns the names registered."""
    return [adapter.type for adapter in runtimes.ADAPTERS.values()
            if adapter is not runtimes.CLAUDE_CODE and await _detect(adapter)]


async def _existing_runtime(runtime_id: str) -> dict[str, Any]:
    from ..db import profile_registry as db

    row = await db.get_runtime(runtime_id)
    if row is None:
        raise RegistryNotFound(f"no runtime {runtime_id!r}")
    return row


def _not_builtin(row: Mapping[str, Any], what: str) -> None:
    if row["builtin"]:
        raise RegistryError(f"the built-in {what} {row['name']!r} cannot be changed or removed; register another one")


async def _runtime_answer(runtime_id: str, server_claude: str) -> dict[str, Any]:
    from ..db import profile_registry as db

    rows = {r["id"]: r for r in await db.list_runtimes()}
    return {"runtime": runtime_view(rows[runtime_id], server_claude=server_claude)}


async def probe_runtime(runtime_id: str, *, server_claude: str = "claude") -> dict[str, Any]:
    """Re-detect a runtime: run ``<executable> --version`` again and record what it found. The built-in runtime
    of a type not found yet (``rt-<type>``) is registered when its executable is on PATH now (``_detect``)."""
    from ..db import profile_registry as db

    adapter = next((a for a in runtimes.ADAPTERS.values() if builtin_runtime_id(a) == runtime_id), None)
    if await db.get_runtime(runtime_id) is None and adapter is not None:
        if not await _detect(adapter):
            import shutil

            if shutil.which(adapter.default_executable) is None:
                raise RegistryNotFound(f"{adapter.label} ({adapter.default_executable}) is not on the server's PATH")
            raise RegistryConflict(f"a runtime named {adapter.type!r} exists already, so the built-in "
                                   f"{adapter.label} runtime cannot be added")
        return await _runtime_answer(runtime_id, server_claude)
    row = await _existing_runtime(runtime_id)
    try:
        probe = await _probe(row["type"], row["executable"], server_claude)
    except RuntimeUnsupported as exc:
        raise RegistryError(str(exc)) from None
    await db.update_runtime(runtime_id, probe)
    return await _runtime_answer(runtime_id, server_claude)


# --- Providers ---

_PROVIDER_FIELDS = ("name", "protocols", "auth", "oauth_runtime_type", "base_url", "models", "env",
                    "max_concurrency", "api_key")


async def _existing_provider(provider_id: str, conn: Any = None) -> dict[str, Any]:
    from ..db import profile_registry as db

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
        "env": _provider_env(merged["env"]),
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


async def _check_provider_profiles(conn: Any, provider: Mapping[str, Any]) -> None:
    """Refuse a provider change that would leave one of its Agent Profiles unable to run."""
    from ..db import profile_registry as db

    spec = provider_spec(provider)
    for row in await db.list_profiles(pool=conn, provider_id=provider["id"]):
        profile = AgentProfile.from_row(row)
        try:
            check_pairing(profile.runtime, spec, profile.model)
        except RegistryError as exc:
            raise RegistryError(f"Agent Profile {profile.name!r} uses this provider: {exc}") from None


async def _provider_answer(provider_id: str) -> dict[str, Any]:
    from ..db import profile_registry as db

    rows = {r["id"]: r for r in await db.list_providers()}
    return {"provider": provider_view(rows[provider_id], await _logins([rows[provider_id]]))}


async def create_provider(data: Any) -> dict[str, Any]:
    """Register ``{name, protocols, auth, oauth_runtime_type?, base_url?, models?, env?, max_concurrency?,
    api_key?}``."""
    import asyncpg

    from ..db import profile_registry as db
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
    """Change a provider; ``api_key`` ``***`` keeps the stored key, null removes it. Its profiles must still run."""
    import asyncpg

    from ..db import profile_registry as db
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
            await _check_provider_profiles(conn, {**fields, "id": provider_id})
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"a provider named {data.get('name')!r} already exists") from None
    return await _provider_answer(provider_id)


async def clear_login_problem(provider_id: str) -> dict[str, Any]:
    """Make a runtime's own login (Codex's or Grok's) available to runs again after a run found it forked: the
    operator has checked it on the machine (its module's ``clear_problem``). The provider is one of that login."""
    row = await _existing_provider(provider_id)
    module = _login_modules().get(row["oauth_runtime_type"] or "") if row["auth"] == AuthKind.OAUTH.value else None
    if module is None:
        raise RegistryError(f"provider {row['name']!r} is not the Codex or Grok login")
    await module.clear_problem()
    return await _provider_answer(provider_id)


async def delete_provider(provider_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import profile_registry as db

    row = await _existing_provider(provider_id)
    _not_builtin(row, "provider")
    try:
        await db.delete_provider(provider_id)
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict(f"provider {row['name']!r} still serves Agent Profiles: change or remove them first") \
            from None
    return {"deleted": provider_id}


# --- Agent Profiles ---

_PROFILE_FIELDS = ("name", "runtime_id", "provider_id", "model", "effort", "agent_teams", "system_prompt_mode", "custom_prompt")


async def _existing_profile(profile_id: str) -> dict[str, Any]:
    from ..db import profile_registry as db

    row = await db.get_profile(profile_id)
    if row is None:
        raise RegistryNotFound(f"no Agent Profile {profile_id!r}")
    return row


async def _parts(runtime_id: Any, provider_id: Any) -> tuple[RuntimeSpec, ProviderSpec]:
    from ..db import profile_registry as db

    runtime = await db.get_runtime(runtime_id) if isinstance(runtime_id, str) else None
    if runtime is None:
        raise RegistryError(f"no runtime {runtime_id!r}: give the runtime_id of a registered runtime")
    provider = await db.get_provider(provider_id) if isinstance(provider_id, str) else None
    if provider is None:
        raise RegistryError(f"no provider {provider_id!r}: give the provider_id of a registered provider")
    return runtime_spec(runtime), provider_spec(provider)


async def create_profile(data: Any) -> dict[str, Any]:
    """Register a profile (system prompt appended by default, agent teams on for Claude Code); refuse settings
    its runtime cannot run."""
    import asyncpg

    from ..db import profile_registry as db

    data = _fields(data, _PROFILE_FIELDS)
    fields = {"name": entry_name(data.get("name"), "profile"), "runtime_id": data.get("runtime_id"),
              "provider_id": data.get("provider_id"), "model": _model(data.get("model")),
              "effort": _effort(data.get("effort")),
              "system_prompt_mode": _system_prompt_mode(data.get("system_prompt_mode", "append")),
              "custom_prompt": _custom_prompt(data.get("custom_prompt", ""))}
    runtime, provider = await _parts(fields["runtime_id"], fields["provider_id"])
    fields["agent_teams"] = _agent_teams(data["agent_teams"]) if "agent_teams" in data else default_agent_teams(runtime)
    check_system_prompt_mode(runtime, fields["system_prompt_mode"])
    check_pairing(runtime, provider, fields["model"])
    check_agent_teams(runtime, fields["agent_teams"])
    try:
        profile_id = await db.insert_profile(fields)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"an Agent Profile named {fields['name']!r} already exists") from None
    return await _profile_answer(profile_id)


async def _profile_answer(profile_id: str) -> dict[str, Any]:
    """The profile, and what registering it as it is should warn about (``cost_warnings``)."""
    from ..db import profile_registry as db

    row = await _existing_profile(profile_id)
    uses = await db.profile_usage(profile_id)
    return {"profile": profile_view(row, uses.get(profile_id, NOT_USED)),
            "warnings": cost_warnings(AgentProfile.from_row(row))}


async def update_profile(profile_id: str, data: Any) -> dict[str, Any]:
    """Change an Agent Profile; the result is checked like a new one."""
    import asyncpg

    from ..db import profile_registry as db

    data = _fields(data, _PROFILE_FIELDS)
    if not data:
        raise RegistryError(f"give at least one of {', '.join(_PROFILE_FIELDS)}")
    row = await _existing_profile(profile_id)
    _not_builtin(row, "profile")
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = entry_name(data["name"], "profile")
    if "model" in data:
        fields["model"] = _model(data["model"])
    if "effort" in data:
        fields["effort"] = _effort(data["effort"])
    if "agent_teams" in data:
        fields["agent_teams"] = _agent_teams(data["agent_teams"])
    if "system_prompt_mode" in data:
        fields["system_prompt_mode"] = _system_prompt_mode(data["system_prompt_mode"])
    if "custom_prompt" in data:
        fields["custom_prompt"] = _custom_prompt(data["custom_prompt"])
    for name in ("runtime_id", "provider_id"):
        if name in data:
            fields[name] = data[name]
    runtime, provider = await _parts(fields.get("runtime_id", row["runtime_id"]),
                                     fields.get("provider_id", row["provider_id"]))
    model = fields.get("model", row["model"])
    if "agent_teams" not in fields and runtime.type != row["runtime_type"]:
        # A profile moved to a runtime of another type takes that type's default, unless the change says.
        fields["agent_teams"] = default_agent_teams(runtime)
    check_system_prompt_mode(runtime, fields.get("system_prompt_mode",
                                               _system_prompt_mode(row.get("system_prompt_mode", "append"))))
    check_pairing(runtime, provider, model)
    check_agent_teams(runtime, fields.get("agent_teams", bool(row.get("agent_teams"))))
    check_roles(AgentProfile(profile_id, fields.get("name", row["name"]), runtime, provider, model),
                await _selected_roles(profile_id))
    try:
        await db.update_profile(profile_id, fields)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"an Agent Profile named {fields['name']!r} already exists") from None
    return await _profile_answer(profile_id)


async def deletable_profile(profile_id: str) -> dict[str, Any]:
    """The profile, when nothing keeps it from being deleted: not built in, and selected nowhere
    (``RegistryConflict`` says where it is)."""
    from ..db import profile_registry as db

    row = await _existing_profile(profile_id)
    _not_builtin(row, "profile")
    used_by = (await db.profile_usage(profile_id)).get(profile_id)
    if used_by:
        raise RegistryConflict(f"Agent Profile {row['name']!r} is selected by {usage_text(used_by)}: select "
                               "another profile there first")
    return row


async def delete_profile(profile_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import profile_registry as db

    row = await deletable_profile(profile_id)
    try:
        await db.delete_profile(profile_id)
    except asyncpg.ForeignKeyViolationError:  # selected since it was checked
        raise RegistryConflict(f"Agent Profile {row['name']!r} is still selected: select another profile there "
                               "first") from None
    return {"deleted": profile_id}
