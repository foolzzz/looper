"""Runtimes, LLM providers and Agent Profiles: what they are, how they are checked, and their API (RFC decision 10).

A ``ProviderSpec`` is an account: its typed vendor, authentication, environment
and concurrency limit. ``Endpoint`` holds one account/protocol URL; ``ModelSpec``
holds an immutable provider/name/protocol identity and its model settings.
``ResolvedModel`` joins that model to its exact endpoint and account and checks
runtime compatibility. ``AgentProfile`` references a model id and runtime,
with effort, agent teams and custom prompt as its only runtime options.

Registry reads and account/profile writes use the normalized account,
endpoint and model tables. ``ResolvedAgentProfile`` snapshots that explicit
binding for a launch; adapters receive a ``LegacyProviderSpec`` projection
containing only its endpoint and model. ``CatalogModel`` also adapts saved model settings for the existing cost calculation. API keys
remain separate from stored account metadata and are omitted from reprs.

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

A legacy profile is checked when it is created or changed, and again when a provider
it uses changes (``check_pairing``):

- its runtime's type is one this version can run;
- the runtime speaks a protocol the provider serves;
- an OAuth provider pairs only with runtimes of the type whose login it is;
- the runtime can be started with that kind of provider now
  (``RuntimeAdapter.auth_kinds``);
- the model is one the provider lists, when it lists any;
- agent teams are on only with a Claude Code runtime (``check_agent_teams``);

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
from dataclasses import MISSING, dataclass, field, fields, replace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import runtimes
from .model_names import parse_model_name
from .runtimes import ApiProtocol, AuthKind, Capability, RuntimeUnsupported, VendorType

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
class LegacyProviderSpec:
    """The v2 provider shape, retained only for the pre-migration persistence and launch path."""

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
class LegacyAgentProfile:
    """The v2 resolved profile, retained until model references are migrated."""

    id: str
    name: str
    runtime: RuntimeSpec
    provider: LegacyProviderSpec
    model: str
    effort: str | None = None  # its default effort; a layer's effort wins
    agent_teams: bool = False  # Claude Code's agent teams in its runs (runtimes.claude_code)
    custom_prompt: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> LegacyAgentProfile:
        """A profile from ``db.profile_registry.PROFILE_JSON``."""
        known = {c.value for c in Capability}
        return cls(
            id=row["id"], name=row["name"], model=row["model"], effort=row.get("effort"),
            agent_teams=bool(row.get("agent_teams")),
            custom_prompt=row.get("custom_prompt", ""),
            runtime=RuntimeSpec(row["runtime_id"], row["runtime_name"], row["runtime_type"],
                                row.get("runtime_executable") or "",
                                frozenset(Capability(c) for c in row.get("runtime_capabilities") or () if c in known)),
            provider=LegacyProviderSpec(
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
BUILTIN_PROVIDER = LegacyProviderSpec("prov-claude-subscription", "claude-subscription", AuthKind.OAUTH,
                                frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), "claude-code")
BUILTIN_PROFILE = LegacyAgentProfile("agent-claude-code-opus", "claude-code/opus", BUILTIN_RUNTIME, BUILTIN_PROVIDER,
                               "opus", agent_teams=True)


# The normalized declarations keep account metadata, endpoint identity and model settings separate.


def _entry_values(cls: type, entry: Mapping[str, Any]) -> dict[str, Any]:
    values = _fields(dict(entry) if isinstance(entry, Mapping) else entry,
                     (item.name for item in fields(cls)))
    for item in fields(cls):
        if item.default is MISSING and item.default_factory is MISSING and item.name not in values:
            raise RegistryError(f"missing required field {item.name!r}")
    return values


def _typed_enum(enum: type, raw: Any, what: str) -> Any:
    try:
        return enum(raw)
    except (ValueError, TypeError):
        raise RegistryError(f"{what} must be one of {', '.join(value.value for value in enum)}") from None


@dataclass(frozen=True)
class ProviderSpec:
    """One account. Endpoints and models have their own identities and lifetimes."""

    id: str
    name: str
    vendor_type: VendorType
    auth: AuthKind
    oauth_runtime_type: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    max_concurrency: int | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)
    builtin: bool = False

    def __post_init__(self) -> None:
        vendor = _typed_enum(VendorType, self.vendor_type, "vendor_type")
        auth = _typed_enum(AuthKind, self.auth, "auth")
        if auth is not vendor.auth_kind or self.oauth_runtime_type != vendor.oauth_runtime_type:
            raise RegistryError("auth and oauth_runtime_type must match vendor_type")
        if auth is AuthKind.OAUTH and self.api_key is not None:
            raise RegistryError("a login provider cannot store an API key")
        object.__setattr__(self, "vendor_type", vendor)
        object.__setattr__(self, "auth", auth)
        object.__setattr__(self, "name", entry_name(self.name, "provider"))
        object.__setattr__(self, "max_concurrency", _max_concurrency(self.max_concurrency))
        if self.api_key is not None:
            object.__setattr__(self, "api_key", _api_key(self.api_key))
        if not isinstance(self.env, (tuple, list)) or any(
                not isinstance(entry, (tuple, list)) or len(entry) != 2 for entry in self.env):
            raise RegistryError("env must contain key/value pairs")
        env = _provider_env([{"key": key, "value": value} for key, value in self.env])
        object.__setattr__(self, "env", _env(env))

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> ProviderSpec:
        values = _entry_values(cls, entry)
        if "env" in values:
            values = {**values, "env": _env(_provider_env(values["env"]))}
        return cls(**values)

    def updated(self, **changes: Any) -> ProviderSpec:
        _fields(changes, ("name", "env", "max_concurrency", "api_key"))
        return replace(self, **changes)


@dataclass(frozen=True)
class Endpoint:
    """One provider's URL for exactly one API. An empty URL needs account context."""

    provider_id: str
    protocol: ApiProtocol
    base_url: str
    use_runtime_default: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol", _typed_enum(ApiProtocol, self.protocol, "protocol"))
        if not isinstance(self.base_url, str):
            raise RegistryError("base_url must be a string")
        object.__setattr__(self, "base_url", _base_url(self.base_url))
        if not isinstance(self.use_runtime_default, bool) or self.use_runtime_default and self.base_url:
            raise RegistryError("use_runtime_default must be boolean and requires an empty base_url")

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> Endpoint:
        return cls(**_entry_values(cls, entry))

    def check_provider(self, provider: ProviderSpec) -> None:
        if self.provider_id != provider.id:
            raise RegistryError("endpoint and provider must belong to the same account")
        if provider.auth is AuthKind.OAUTH:
            if self.protocol is not provider.vendor_type.login_protocol or self.base_url or self.use_runtime_default:
                raise RegistryError("a login endpoint must use its runtime's protocol and CLI-managed URL")
        elif not self.base_url and not self.use_runtime_default:
            raise RegistryError("an API-key endpoint needs an explicit base_url")


@dataclass(frozen=True)
class ModelSpec:
    """A single, exact callable model; the provider/name/protocol identity is immutable."""

    id: str
    provider_id: str
    name: str
    protocol: ApiProtocol
    context_window: int | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cache_read_usd_per_mtok: float | None = None
    cache_write_usd_per_mtok: float | None = None
    efforts: tuple[str, ...] | None = None
    background_model: str | None = None
    catalog_order: int = 0
    builtin: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol", _typed_enum(ApiProtocol, self.protocol, "protocol"))
        values = {name: getattr(self, name) for name in MODEL_FIELDS}
        if isinstance(values["efforts"], tuple):
            values["efforts"] = list(values["efforts"])
        checked = _models([values])[0]
        for name, value in checked.items():
            object.__setattr__(self, name, tuple(value) if name == "efforts" and value is not None else value)
        if isinstance(self.catalog_order, bool) or not isinstance(self.catalog_order, int) or self.catalog_order < 0:
            raise RegistryError("catalog_order must be a non-negative integer")

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> ModelSpec:
        return cls(**_entry_values(cls, entry))

    def updated(self, **changes: Any) -> ModelSpec:
        _fields(changes, (*MODEL_FIELDS[1:], "catalog_order"))
        return replace(self, **changes)


@dataclass(frozen=True)
class ResolvedModel:
    """A model joined to its own account and exact protocol endpoint, without fallback."""

    model: ModelSpec
    provider: ProviderSpec
    endpoint: Endpoint

    def __post_init__(self) -> None:
        if self.model.provider_id != self.provider.id or self.model.protocol is not self.endpoint.protocol:
            raise RegistryError("model, provider and endpoint references must match")
        self.endpoint.check_provider(self.provider)

    def check_runtime(self, runtime: RuntimeSpec, effort: str | None = None) -> None:
        adapter = _runtime_type(runtime.type)
        if self.model.protocol not in adapter.protocols:
            raise RegistryError("runtime does not support the model's exact protocol")
        if self.provider.auth is AuthKind.OAUTH and self.provider.oauth_runtime_type != runtime.type:
            raise RegistryError("a login provider can only use its own runtime")
        if self.provider.auth not in adapter.auth_kinds:
            raise RegistryError("runtime does not support the provider's auth kind")
        try:
            adapter.check_pairing(self.binding_provider(), self.model.name, effort)
        except RuntimeUnsupported as exc:
            raise RegistryError(str(exc)) from None

    def binding_provider(self) -> LegacyProviderSpec:
        """The adapter input: exactly this endpoint and model, never an account catalog."""
        model = CatalogModel(**{name: getattr(self.model, name) for name in MODEL_FIELDS})
        return LegacyProviderSpec(
            self.provider.id, self.provider.name, self.provider.auth, frozenset({self.model.protocol}),
            self.provider.oauth_runtime_type, self.endpoint.base_url, (model,), self.provider.env,
            self.provider.api_key,
        )


@dataclass(frozen=True)
class AgentProfile:
    """A runtime and model reference, with only the profile's three runtime options."""

    id: str
    name: str
    runtime: RuntimeSpec
    model_id: str
    effort: str | None = None
    agent_teams: bool = False
    custom_prompt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", entry_name(self.name, "Agent Profile"))
        if not isinstance(self.model_id, str) or not self.model_id or _CONTROL.search(self.model_id):
            raise RegistryError("an Agent Profile needs a model_id")
        object.__setattr__(self, "effort", _effort(self.effort, self.runtime))
        _agent_teams(self.agent_teams)
        check_agent_teams(self.runtime, self.agent_teams)
        object.__setattr__(self, "custom_prompt", _custom_prompt(self.custom_prompt))

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> AgentProfile:
        return cls(**_entry_values(cls, entry))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AgentProfile | LegacyAgentProfile:
        # The v2 database remains readable until the schema migration is applied.
        if "model_id" not in row:
            return LegacyAgentProfile.from_row(row)
        known = {c.value for c in Capability}
        runtime = RuntimeSpec(row["runtime_id"], row["runtime_name"], row["runtime_type"],
                              row.get("runtime_executable") or "",
                              frozenset(Capability(c) for c in row.get("runtime_capabilities") or () if c in known))
        return cls(row["id"], row["name"], runtime, row["model_id"], row.get("effort"),
                   row.get("agent_teams", False), row.get("custom_prompt", ""))

    def record(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "model_id": self.model_id, "custom_prompt": self.custom_prompt,
                "runtime": {"id": self.runtime.id, "name": self.runtime.name, "type": self.runtime.type}}


@dataclass(frozen=True, kw_only=True)
class ResolvedAgentProfile(AgentProfile):
    """A launch snapshot of the profile and its single explicit model binding."""

    resolved_model: ResolvedModel

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.model_id != self.resolved_model.model.id:
            raise RegistryError("profile and resolved model references must match")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ResolvedAgentProfile:
        profile = AgentProfile.from_row(row)
        model = ModelSpec.from_entry({name: row["model"][name] for name in ModelSpec.__dataclass_fields__})
        resolved = ResolvedModel(model, provider_spec(row["provider"]), Endpoint.from_entry(row["endpoint"]))
        return cls(profile.id, profile.name, profile.runtime, profile.model_id, profile.effort,
                   profile.agent_teams, profile.custom_prompt, resolved_model=resolved)

    @property
    def model(self) -> str:
        return self.resolved_model.model.name

    @property
    def provider(self) -> LegacyProviderSpec:
        self.resolved_model.check_runtime(self.runtime, self.effort)
        return self.resolved_model.binding_provider()

    def record(self) -> dict[str, Any]:
        provider = self.resolved_model.provider
        return {**super().record(), "model": self.model,
                "provider": {"id": provider.id, "name": provider.name, "auth": provider.auth.value}}


def builtin_resolved_profile() -> ResolvedAgentProfile:
    """The offline fallback; normal loads read the built-in's current model settings from the DB."""
    provider = ProviderSpec(BUILTIN_PROVIDER.id, BUILTIN_PROVIDER.name, VendorType.CLAUDE_SUBSCRIPTION,
                            AuthKind.OAUTH, "claude-code", builtin=True)
    model = ModelSpec("model-84f4b69e-12a7-5fd7-b665-a9b7d01568eb", provider.id, "opus",
                      ApiProtocol.ANTHROPIC_MESSAGES, builtin=True)
    return ResolvedAgentProfile(BUILTIN_PROFILE.id, BUILTIN_PROFILE.name, BUILTIN_RUNTIME, model.id,
                                agent_teams=True,
                                resolved_model=ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, "")))


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
    if url is None or len(url) > BASE_URL_MAX_CHARS or _CONTROL.search(raw) \
            or (url and re.search(r"\s", url)):
        raise RegistryError(f"base_url must be an http:// or https:// URL of at most {BASE_URL_MAX_CHARS} "
                            "characters, or empty for the protocol's own endpoint")
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        parsed.port  # Validate numeric port and range; urlsplit does not do it eagerly.
    except ValueError:
        valid = False
    if not valid:
        raise RegistryError("base_url must be a valid http:// or https:// URL")
    credential_fields = {"key", "apikey", "token", "apitoken", "accesstoken", "refreshtoken", "authtoken",
                         "sessiontoken", "authorization", "auth", "secret", "secretkey", "clientsecret",
                         "password", "credential", "credentials", "signature", "sig", "xapikey", "xgoogapikey",
                         "xamzcredential", "xamzsignature", "xamzsecuritytoken"}
    if parsed.username is not None or parsed.password is not None or parsed.fragment \
            or any(re.sub(r"[-_.]", "", name).lower() in credential_fields
                   for name, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise RegistryError("base_url must not contain credentials or fragments; use the separate api_key field")
    return url


def _price(raw: Any, field: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= PRICE_MAX_USD \
            or not math.isfinite(raw):
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


def _effort(raw: Any, runtime: RuntimeSpec | None = None) -> str | None:
    from .agent_settings import SettingsError, effort_level

    # Profiles can use adapter-specific choices beyond the shared settings levels.
    adapter = runtimes.ADAPTERS.get(runtime.type) if runtime is not None else None
    levels = (adapter.effort_levels if adapter is not None else
              tuple(level for adapter in runtimes.ADAPTERS.values() for level in adapter.effort_levels or ()))
    if adapter is not None and levels is not None and raw not in (None, "") and raw not in levels:
        raise RegistryError(f"{adapter.label} effort must be one of {', '.join(levels)}, or empty for the runtime's own")
    if levels is not None and isinstance(raw, str) and raw in levels:
        return raw
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


def check_pairing(runtime: RuntimeSpec, provider: LegacyProviderSpec, model: str, effort: str | None = None) -> None:
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
    try:
        adapter.check_pairing(provider, model, effort)
    except RuntimeUnsupported as exc:
        raise RegistryError(str(exc)) from None


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


def _custom_prompt(raw: Any) -> str:
    from .agent_settings import HARNESS_MAX_CHARS

    if not isinstance(raw, str):
        raise RegistryError("custom_prompt must be a string")
    if "\x00" in raw:
        raise RegistryError("custom_prompt cannot contain NUL characters")
    text = raw.strip()
    if len(text) > HARNESS_MAX_CHARS:
        raise RegistryError(f"custom_prompt has at most {HARNESS_MAX_CHARS} characters, got {len(text)}")
    return text


def check_roles(profile: AgentProfile | LegacyAgentProfile, roles: Iterable[str]) -> None:
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


async def _selected_roles(profile_id: str, conn: Any = None) -> list[str]:
    """Every role ``profile_id`` is selected for, by a layer or a task."""
    from ..db import agent_sessions, agent_setting_layers

    roles: set[str] = set()
    for scope, role in await agent_setting_layers.selected_roles(profile_id, pool=conn):
        roles.update(_SCOPE_ROLES.get(scope, (role,)))
    if await agent_sessions.selects_profile("manager", profile_id, pool=conn):
        roles.add("manager")
    return [role for role in runtimes.ROLE_CAPABILITIES if role in roles]


# --- Views ---


def runtime_spec(row: Mapping[str, Any]) -> RuntimeSpec:
    """A runtime row (``agent_runtimes``) as what a profile is checked with."""
    known = {c.value for c in Capability}
    return RuntimeSpec(row["id"], row["name"], row["type"], row["executable"],
                       frozenset(Capability(c) for c in row["capabilities"] or () if c in known))


def provider_spec(row: Mapping[str, Any]) -> ProviderSpec:
    """An account's public fields, never its separately stored API key."""
    return ProviderSpec(row["id"], row["name"], VendorType(row["vendor_type"]), AuthKind(row["auth"]),
                        row.get("oauth_runtime_type"), _env(row.get("env")), row.get("max_concurrency"),
                        builtin=bool(row.get("builtin")))


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
            "supported_models": sorted(adapter.supported_models) if adapter.supported_models is not None else None,
            "effort_levels": list(adapter.effort_levels) if adapter.effort_levels is not None else None,
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
        "provider_id": row["id"], "name": row["name"], "vendor_type": row["vendor_type"], "auth": row["auth"],
        "oauth_runtime_type": row["oauth_runtime_type"], "endpoints": [dict(e) for e in row["endpoints"]],
        "suggested_models": list(adapter.login_models) if oauth and adapter else [],
        "login": dict(logins[adapter.type]) if oauth and adapter and adapter.type in (logins or {}) else None,
        "env": list(row.get("env") or []), "max_concurrency": row["max_concurrency"],
        "api_key": SECRET_MASK if row.get("api_key_set") else None, "builtin": row["builtin"],
        "endpoint_count": len(row["endpoints"]),
        "model_count": row.get("model_count", 0), "profile_count": row.get("profile_count", 0),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def model_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A callable model's identity, settings and usage, without account secrets."""
    from .runtimes.pricing import model_price

    account = row["provider"]
    provider = LegacyProviderSpec(
        account["id"], account["name"], AuthKind(account["auth"]),
        frozenset({ApiProtocol(row["protocol"])}), account.get("oauth_runtime_type"),
        row["endpoint"]["base_url"], (CatalogModel.from_entry(row),))
    effective = model_price(provider, row["name"])
    prices = {name: {"override": row[name], "effective": getattr(effective, name),
                     "source": "override" if row[name] is not None else
                     "builtin" if getattr(effective, name) is not None else "unknown"}
              for name in PRICE_FIELDS}
    warnings = []
    if not effective.complete:
        warnings.append("This model has no full price: runs may record tokens only and spend caps may not apply.")
    if row["protocol"] != ApiProtocol.ANTHROPIC_MESSAGES.value:
        warnings += _context_suffix_warnings(name for name in (row["name"], row["background_model"]) if name)
    return {
        "model_id": row["id"], "provider_id": row["provider_id"], "provider_name": row["provider_name"],
        "protocol": row["protocol"], **{name: row[name] for name in MODEL_FIELDS},
        "catalog_order": row["catalog_order"], "builtin": row["builtin"],
        "profile_count": row["profile_count"], "endpoint": dict(row["endpoint"]),
        "prices": prices, "warnings": warnings,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def vendor_types_view() -> list[dict[str, Any]]:
    """Creation suggestions only: no saved model or account is resolved from presets."""
    from .provider_presets import vendor_defaults

    result = []
    for vendor in VendorType:
        defaults = vendor_defaults(vendor, provider_id="draft")
        adapter = runtimes.ADAPTERS.get(vendor.oauth_runtime_type or "")
        models = [{"name": m.name, "protocol": m.protocol.value,
                   **{name: getattr(m, name) for name in MODEL_FIELDS[1:]}} for m in defaults.models]
        if adapter is not None and vendor.login_protocol is not None:
            models = [{"name": name, "protocol": vendor.login_protocol.value} for name in adapter.login_models]
        result.append({
            "vendor_type": vendor.value, "auth": vendor.auth_kind.value,
            "oauth_runtime_type": vendor.oauth_runtime_type,
            "endpoints": [{"protocol": e.protocol.value, "base_url": e.base_url,
                           "use_runtime_default": e.use_runtime_default}
                          for e in sorted(defaults.endpoints, key=lambda e: e.protocol.value)],
            "suggested_models": models,
            "env": [{"key": key, "value": value} for key, value in defaults.env],
            "requires_url_confirmation": defaults.requires_url_confirmation,
        })
    return result


def _login_modules() -> dict[str, Any]:
    """The runtimes whose own login Looper links into its homes, by type, and the module of each."""
    from .runtimes import codex_login, grok_login

    return {"codex": codex_login, "grok": grok_login}


async def _logins(providers: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """The state of each runtime login a listed OAuth provider stands for, by runtime type (Codex's and Grok's:
    their module's ``login_view``; Claude Code's comes from the startup self-check)."""
    types = {row["oauth_runtime_type"] for row in providers if row["auth"] == AuthKind.OAUTH.value}
    return {kind: await module.login_view() for kind, module in _login_modules().items() if kind in types}


def cost_basis(profile: LegacyAgentProfile) -> dict[str, Any]:
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


def cost_warnings(profile: LegacyAgentProfile) -> list[str]:
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


def _context_suffix_warnings(models: Iterable[str]) -> list[str]:
    names = [name for name in dict.fromkeys(models) if parse_model_name(name).context_suffix is not None]
    if not names:
        return []
    return [f"models {_listed(names)} carry a context suffix: this is a Claude Code convention; other runtimes "
            "send these names verbatim to the API, which may reject them. Use suffix-free model names and set "
            "context_window in the provider's catalog"]


def provider_warnings(provider: LegacyProviderSpec) -> list[str]:
    """An API without Anthropic messages cannot rely on Claude Code interpreting its catalog's suffixes."""
    if ApiProtocol.ANTHROPIC_MESSAGES in provider.protocols:
        return []
    return _context_suffix_warnings(name for model in provider.catalog
                                    for name in (model.name, model.background_model) if name)


def profile_warnings(profile: LegacyAgentProfile) -> list[str]:
    """Warnings about this profile's prices and the model names its runtime sends."""
    warnings = cost_warnings(profile)
    adapter = runtimes.ADAPTERS.get(profile.runtime.type)
    entry = profile.provider.catalog_model(profile.model)
    sent = {name: adapter.api_model(name) if adapter is not None else name
            for name in [profile.model, *([entry.background_model] if entry and entry.background_model else [])]}
    # Warn only about the names that reach the API still carrying a suffix, as the adapter maps them.
    return warnings + _context_suffix_warnings(name for name, api in sent.items()
                                               if parse_model_name(api).context_suffix is not None)


# Where nothing selects a profile.
NOT_USED: Mapping[str, Any] = {"layers": [], "task_count": 0, "schedule_count": 0, "allowed_in": [],
                                "manager_session": False}


def _cost_profile(row: Mapping[str, Any], models: Iterable[Mapping[str, Any]] = ()) -> LegacyAgentProfile:
    """Adapt the exact normalized model to the existing cost/warning calculation.

    Like a launch (``agent_settings``), prices come from the models of the selected model's account and protocol
    (``models``, any model rows; others are ignored), so a background model with its own price is priced.
    """
    profile = AgentProfile.from_row(row)
    model, account, endpoint = row["model"], row["provider"], row["endpoint"]
    provider = LegacyProviderSpec(
        account["id"], account["name"], AuthKind(account["auth"]),
        frozenset({ApiProtocol(model["protocol"])}), account.get("oauth_runtime_type"),
        endpoint["base_url"], (CatalogModel.from_entry(model), *(
            CatalogModel.from_entry(other) for other in models if other["id"] != model["id"]
            and other["provider_id"] == model["provider_id"] and other["protocol"] == model["protocol"])),
        _env(account.get("env")))
    return LegacyAgentProfile(profile.id, profile.name, profile.runtime, provider, model["name"],
                              profile.effort, profile.agent_teams, profile.custom_prompt)


def profile_view(row: Mapping[str, Any], used_by: Mapping[str, Any] = NOT_USED,
                 models: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """An Agent Profile's saved model and derived account, roles, usage and cost warnings; ``models`` are the
    model rows its prices may come from (see ``_cost_profile``)."""
    spec = AgentProfile.from_row(row)
    cost_profile = _cost_profile(row, models)
    model, provider = row["model"], row["provider"]
    return {
        "profile_id": row["id"], "name": row["name"], "model_id": row["model_id"], "effort": row.get("effort"),
        "model": {"model_id": model["id"], "name": model["name"], "protocol": model["protocol"],
                  "provider_id": model["provider_id"]},
        "runtime_id": row["runtime_id"], "runtime_name": row["runtime_name"], "runtime_type": row["runtime_type"],
        "provider_id": provider["id"], "provider_name": provider["name"], "builtin": row["builtin"],
        "provider": {"provider_id": provider["id"], "name": provider["name"],
                     "vendor_type": provider["vendor_type"], "auth": provider["auth"]},
        "agent_teams": spec.agent_teams, "custom_prompt": spec.custom_prompt,
        "roles": servable_roles(spec.runtime.capabilities), "used_by": dict(used_by),
        "cost": cost_basis(cost_profile), "warnings": profile_warnings(cost_profile),
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
    if used_by.get("manager_session"):
        parts.append("the Manager's current session")
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
    """Runtime/vendor types and saved runtimes, accounts, models and profiles, with usage counts."""
    from ..db import profile_registry as db

    from .runtimes.pricing import BUILTIN_PRICES, PRICE_SOURCES, PRICES_AS_OF

    uses = await db.profile_usage()
    providers = await db.list_providers()
    logins = await _logins(providers)
    models = await db.list_models()
    return {
        "runtime_types": [type_view(a) for a in runtimes.ADAPTERS.values()],
        "runtimes": [runtime_view(r, server_claude=server_claude) for r in await db.list_runtimes()],
        "providers": [provider_view(r, logins) for r in providers],
        "profiles": [profile_view(r, uses.get(r["id"], NOT_USED), models) for r in await db.list_profiles()],
        "models": [model_view(r) for r in models],
        "vendor_types": vendor_types_view(),
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

_PROVIDER_FIELDS = ("name", "vendor_type", "endpoints", "env", "max_concurrency", "api_key")


async def _existing_provider(provider_id: str, conn: Any = None) -> dict[str, Any]:
    from ..db import profile_registry as db

    row = await db.get_provider(provider_id, pool=conn)
    if row is None:
        raise RegistryNotFound(f"no provider {provider_id!r}")
    return row


async def list_providers() -> dict[str, Any]:
    """Saved accounts with login state and usage counts, using the registry's public view."""
    from ..db import profile_registry as db

    providers = await db.list_providers()
    logins = await _logins(providers)
    return {"providers": [provider_view(row, logins) for row in providers]}


def _provider_fields(data: Mapping[str, Any], current: Mapping[str, Any] | None) -> tuple[dict[str, Any], Any]:
    """The provider's columns after the request, validated, and what to do with its key: ``...`` keep it, None
    remove it, or the new key."""
    from .provider_presets import vendor_defaults

    _fields(data, _PROVIDER_FIELDS)
    vendor = _typed_enum(VendorType, data.get("vendor_type", (current or {}).get("vendor_type")), "vendor_type")
    if current is not None and vendor.value != current["vendor_type"]:
        raise RegistryError("vendor_type is immutable: create another provider instead")
    provider_id = current["id"] if current else "draft"
    # Defaults belong only to creation, never re-applied during an update.
    if current is None:
        defaults = vendor_defaults(vendor, provider_id=provider_id)
        initial = {"endpoints": [{"protocol": e.protocol.value, "base_url": e.base_url,
                                  "use_runtime_default": e.use_runtime_default} for e in defaults.endpoints],
                   "env": [{"key": key, "value": value} for key, value in defaults.env]}
        if defaults.requires_url_confirmation and "endpoints" not in data:
            raise RegistryError("this vendor requires explicit endpoint URL confirmation")
    else:
        initial = dict(current)
    merged = {**initial, **data}
    fields: dict[str, Any] = {
        "name": entry_name(merged.get("name"), "provider"), "vendor_type": vendor.value,
        "auth": vendor.auth_kind.value, "oauth_runtime_type": vendor.oauth_runtime_type,
        "env": _provider_env(merged.get("env")),
        "max_concurrency": _max_concurrency(merged.get("max_concurrency")),
    }
    spec = provider_spec({**fields, "id": provider_id})
    raw = merged.get("endpoints")
    if not isinstance(raw, list) or (current is None and not raw) or len(raw) > len(ApiProtocol):
        raise RegistryError("endpoints must be a list with distinct protocols; a new account needs an endpoint")
    old = {e["protocol"]: e for e in (current or {}).get("endpoints", [])}
    endpoints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        entry = _fields(entry, ("provider_id", "protocol", "base_url", "use_runtime_default"))
        if "provider_id" in entry and entry["provider_id"] != provider_id:
            raise RegistryError("endpoint belongs to a different provider")
        protocol = _typed_enum(ApiProtocol, entry.get("protocol"), "protocol").value
        if protocol in seen:
            raise RegistryError("endpoints must contain distinct protocols")
        seen.add(protocol)
        url = _base_url(entry.get("base_url"))
        previous = old.get(protocol, {})
        marker = entry.get("use_runtime_default", previous.get("use_runtime_default", False))
        if not isinstance(marker, bool):
            raise RegistryError("use_runtime_default must be boolean")
        if url != previous.get("base_url"):
            # Explicit URLs end migration compatibility, even on a read-modify-write body.
            if url:
                marker = False
            elif marker:
                raise RegistryError("use_runtime_default is reserved for migrated endpoints")
        if marker and not previous.get("use_runtime_default"):
            raise RegistryError("use_runtime_default is reserved for migrated endpoints")
        endpoint = Endpoint(provider_id, ApiProtocol(protocol), url, marker)
        endpoint.check_provider(spec)
        endpoints.append({"protocol": protocol, "base_url": endpoint.base_url, "use_runtime_default": marker})
    fields["endpoints"] = endpoints
    key: Any = ...
    if spec.auth is AuthKind.OAUTH:
        if data.get("api_key") not in (None, SECRET_MASK):
            raise RegistryError("a login provider takes no api_key")
        key = None if current is not None and current.get("api_key_set") else ...
    elif "api_key" in data and data["api_key"] != SECRET_MASK:
        key = None if data["api_key"] in (None, "") else _api_key(data["api_key"])
    return fields, key


async def _check_provider_profiles(conn: Any, provider: Mapping[str, Any]) -> None:
    """Refuse a provider change that would leave one of its Agent Profiles unable to run."""
    from ..db import profile_registry as db

    spec = provider_spec(provider)
    for row in await db.list_profiles(pool=conn, provider_id=provider["id"]):
        profile = AgentProfile.from_row(row)
        try:
            model = ModelSpec.from_entry({k: row["model"][k] for k in ModelSpec.__dataclass_fields__})
            ResolvedModel(model, spec, Endpoint.from_entry(row["endpoint"])).check_runtime(profile.runtime, profile.effort)
            check_agent_teams(profile.runtime, profile.agent_teams)
        except RegistryError as exc:
            raise RegistryError(f"Agent Profile {profile.name!r} uses this provider: {exc}") from None


async def _provider_answer(provider_id: str) -> dict[str, Any]:
    from ..db import profile_registry as db

    row = await _existing_provider(provider_id)
    warnings: list[str] = []
    if not any(e["protocol"] == ApiProtocol.ANTHROPIC_MESSAGES.value for e in row["endpoints"]):
        models = await db.list_models(provider_id=provider_id)
        warnings = _context_suffix_warnings(name for model in models
                                            for name in (model["name"], model["background_model"]) if name)
    return {"provider": provider_view(row, await _logins([row])), "warnings": warnings}


async def create_provider(data: Any) -> dict[str, Any]:
    """Register an account and its endpoints. Vendor suggestions never register models implicitly."""
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
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict("an endpoint still serves models: remove those models first") from None
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
    from ..db.connection import transaction

    try:
        async with transaction() as conn:
            await db.lock_provider(conn, provider_id)
            row = await _existing_provider(provider_id, conn)
            _not_builtin(row, "provider")
            await db.delete_provider(provider_id, pool=conn)
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict(f"provider {row['name']!r} still serves models: remove them first") \
            from None
    return {"deleted": provider_id}


# --- Models ---

_MODEL_SETTINGS = MODEL_FIELDS[1:]
_MODEL_FIELDS = ("provider_id", "name", "protocol", *_MODEL_SETTINGS)


def _resolved_model(row: Mapping[str, Any]) -> ResolvedModel:
    model = ModelSpec.from_entry({name: row[name] for name in ModelSpec.__dataclass_fields__})
    return ResolvedModel(model, provider_spec(row["provider"]), Endpoint.from_entry(row["endpoint"]))


async def _existing_model(model_id: str, conn: Any = None) -> dict[str, Any]:
    from ..db import profile_registry as db

    row = await db.get_model(model_id, pool=conn)
    if row is None:
        raise RegistryNotFound(f"no model {model_id!r}")
    return row


async def list_models(*, provider_id: str | None = None, runtime_id: str | None = None) -> dict[str, Any]:
    """List exact saved models, optionally restricted to pairings a runtime can run."""
    from ..db import profile_registry as db

    runtime = runtime_spec(await _existing_runtime(runtime_id)) if runtime_id is not None else None
    models = []
    for row in await db.list_models(provider_id=provider_id):
        if runtime is not None:
            try:
                _resolved_model(row).check_runtime(runtime)
            except RegistryError:
                continue
        models.append(model_view(row))
    return {"models": models}


async def get_model(model_id: str) -> dict[str, Any]:
    model = model_view(await _existing_model(model_id))
    return {"model": model, "warnings": model["warnings"]}


async def create_model(data: Any) -> dict[str, Any]:
    """Register one exact account/name/protocol triple with explicit endpoint validation."""
    import asyncpg

    from ..db import profile_registry as db
    from ..db.connection import transaction

    data = _fields(data, _MODEL_FIELDS)
    provider_id = data.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        raise RegistryError("a model needs a provider_id")
    model = ModelSpec.from_entry({"id": "draft", **data})
    fields = {name: getattr(model, name) for name in _MODEL_FIELDS}
    try:
        async with transaction() as conn:
            await db.lock_provider(conn, provider_id)
            provider = await _existing_provider(provider_id, conn)
            endpoint = next((e for e in provider["endpoints"] if e["protocol"] == model.protocol.value), None)
            if endpoint is None:
                available = _listed(e["protocol"] for e in provider["endpoints"])
                raise RegistryError(f"protocol {model.protocol.value!r} needs an endpoint on this provider; "
                                    f"available protocols: {available}")
            ResolvedModel(model, provider_spec(provider), Endpoint.from_entry(endpoint))
            model_id = await db.insert_model(conn, fields)
            answer = model_view(await _existing_model(model_id, conn))
    except asyncpg.UniqueViolationError:
        raise RegistryConflict("a model with this provider, exact name and protocol already exists") from None
    return {"model": answer, "warnings": answer["warnings"]}


async def update_model(model_id: str, data: Any) -> dict[str, Any]:
    """Change settings under the account lock and revalidate every referring profile before committing."""
    from ..db import profile_registry as db
    from ..db.connection import transaction

    data = _fields(data, _MODEL_SETTINGS)
    if not data:
        raise RegistryError(f"give at least one of {', '.join(_MODEL_SETTINGS)}")
    async with transaction() as conn:
        row = await db.lock_model(conn, model_id)
        if row is None:
            raise RegistryNotFound(f"no model {model_id!r}")
        _not_builtin(row, "model")
        checked = _resolved_model(row).model.updated(**data)
        await db.update_model(conn, model_id, {name: getattr(checked, name) for name in data})
        resolved = _resolved_model(await _existing_model(model_id, conn))
        for profile_row in await db.list_profiles(pool=conn, provider_id=row["provider_id"]):
            if profile_row["model_id"] != model_id:
                continue
            try:
                profile = AgentProfile.from_row(profile_row)
                resolved.check_runtime(profile.runtime, profile.effort)
                check_roles(profile, await _selected_roles(profile.id, conn))
            except RegistryError as exc:
                raise RegistryError(f"Agent Profile {profile_row['name']!r} uses this model: {exc}") from None
        answer = model_view(await _existing_model(model_id, conn))
    return {"model": answer, "warnings": answer["warnings"]}


async def delete_model(model_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import profile_registry as db
    from ..db.connection import transaction

    try:
        async with transaction() as conn:
            row = await db.lock_model(conn, model_id)
            if row is None:
                raise RegistryNotFound(f"no model {model_id!r}")
            _not_builtin(row, "model")
            await db.delete_model(model_id, pool=conn)
    except asyncpg.ForeignKeyViolationError:
        raise RegistryConflict("this model still has Agent Profile references: remove or repoint them first") from None
    return {"deleted": model_id}


# --- Agent Profiles ---

_PROFILE_FIELDS = ("name", "runtime_id", "model_id", "effort", "agent_teams", "custom_prompt")


async def _existing_profile(profile_id: str, conn: Any = None) -> dict[str, Any]:
    from ..db import profile_registry as db

    row = await db.get_profile(profile_id, pool=conn)
    if row is None:
        raise RegistryNotFound(f"no Agent Profile {profile_id!r}")
    return row


async def _parts(runtime_id: Any, model_id: Any, conn: Any) -> tuple[RuntimeSpec, ResolvedModel]:
    from ..db import profile_registry as db

    runtime = await db.get_runtime(runtime_id, pool=conn) if isinstance(runtime_id, str) else None
    if runtime is None:
        raise RegistryError(f"no runtime {runtime_id!r}: give the runtime_id of a registered runtime")
    row = await db.lock_model(conn, model_id) if isinstance(model_id, str) else None
    if row is None:
        raise RegistryError(f"no model {model_id!r}: give the model_id of a registered model")
    return runtime_spec(runtime), _resolved_model(row)


async def create_profile(data: Any) -> dict[str, Any]:
    """Register a profile (agent teams on for Claude Code); refuse settings
    its runtime cannot run."""
    import asyncpg

    from ..db import profile_registry as db
    from ..db.connection import transaction

    data = _fields(data, _PROFILE_FIELDS)
    fields = {"name": entry_name(data.get("name"), "profile"), "runtime_id": data.get("runtime_id"),
              "model_id": data.get("model_id"),
              "effort": _effort(data.get("effort")),
              "custom_prompt": _custom_prompt(data.get("custom_prompt", ""))}
    try:
        async with transaction() as conn:
            runtime, model = await _parts(fields["runtime_id"], fields["model_id"], conn)
            fields["agent_teams"] = (_agent_teams(data["agent_teams"]) if "agent_teams" in data
                                     else default_agent_teams(runtime))
            model.check_runtime(runtime, fields["effort"])
            AgentProfile("draft", fields["name"], runtime, model.model.id, fields["effort"],
                         fields["agent_teams"], fields["custom_prompt"])
            profile_id = await db.insert_profile(fields, pool=conn)
    except asyncpg.UniqueViolationError:
        raise RegistryConflict(f"an Agent Profile named {fields['name']!r} already exists") from None
    return await _profile_answer(profile_id)


async def _profile_answer(profile_id: str) -> dict[str, Any]:
    """The profile, and what registering it as it is should warn about (``profile_warnings``)."""
    from ..db import profile_registry as db

    row = await _existing_profile(profile_id)
    uses = await db.profile_usage(profile_id)
    models = await db.list_models(provider_id=row["provider"]["id"])
    return {"profile": profile_view(row, uses.get(profile_id, NOT_USED), models),
            "warnings": profile_warnings(_cost_profile(row, models))}


async def profile_views(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``profile_view`` of each profile row, keyed as given, priced like a launch."""
    from ..db import profile_registry as db

    models = await db.list_models() if rows else []
    return {key: profile_view(row, models=models) for key, row in rows.items()}


async def update_profile(profile_id: str, data: Any) -> dict[str, Any]:
    """Change an Agent Profile; the result is checked like a new one."""
    import asyncpg

    from ..db import profile_registry as db
    from ..db.connection import transaction

    data = _fields(data, _PROFILE_FIELDS)
    if not data:
        raise RegistryError(f"give at least one of {', '.join(_PROFILE_FIELDS)}")
    row = await _existing_profile(profile_id)
    _not_builtin(row, "profile")
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = entry_name(data["name"], "profile")
    if "effort" in data:
        fields["effort"] = _effort(data["effort"])
    if "agent_teams" in data:
        fields["agent_teams"] = _agent_teams(data["agent_teams"])
    if "custom_prompt" in data:
        fields["custom_prompt"] = _custom_prompt(data["custom_prompt"])
    for name in ("runtime_id", "model_id"):
        if name in data:
            fields[name] = data[name]
    try:
        async with transaction() as conn:
            runtime, model = await _parts(fields.get("runtime_id", row["runtime_id"]),
                                           fields.get("model_id", row["model_id"]), conn)
            await conn.execute("SELECT 1 FROM agent_profiles WHERE id = $1 FOR UPDATE", profile_id)
            current = await _existing_profile(profile_id, conn)
            if current["model_id"] != row["model_id"] or current["runtime_id"] != row["runtime_id"]:
                raise RegistryConflict("Agent Profile changed concurrently: reload it before updating")
            if "agent_teams" not in fields and runtime.type != current["runtime_type"]:
                fields["agent_teams"] = default_agent_teams(runtime)
            model.check_runtime(runtime, fields.get("effort", current["effort"]))
            checked = AgentProfile(profile_id, fields.get("name", current["name"]), runtime,
                                   model.model.id, fields.get("effort", current["effort"]),
                                   fields.get("agent_teams", current["agent_teams"]),
                                   fields.get("custom_prompt", current["custom_prompt"]))
            check_roles(checked, await _selected_roles(profile_id, conn))
            await db.update_profile(profile_id, fields, pool=conn)
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
        how = ("; switch the Manager's session with /profile or start a new one with /new"
               if used_by.get("manager_session") else "")
        raise RegistryConflict(f"Agent Profile {row['name']!r} is selected by {usage_text(used_by)}: select "
                               f"another profile there first{how}")
    return row


async def delete_profile(profile_id: str) -> dict[str, Any]:
    import asyncpg

    from ..db import profile_registry as db, tasks as db_tasks

    row = await deletable_profile(profile_id)
    try:
        # A Planner's choice under a project without a list is on no list; one of a task that has not ended blocks
        # the deletion, one of an ended task goes with the profile. Checked and deleted under one lock.
        chosen = await db.delete_profile_unchosen(profile_id, [db_tasks.READY, *db_tasks.ACTIVE_STATUSES])
        if chosen:
            named = ", ".join(f"{t['title']!r} ({t['id']})" for t in chosen[:5])
            raise RegistryConflict(f"Agent Profile {row['name']!r} is chosen by the Planner of {len(chosen)} "
                                   f"task(s) that have not ended: {named}; clear the choice (looper_task_profile_set "
                                   "with a null profile_id, or define the role again without it) or wait for the "
                                   "task to end")
    except asyncpg.ForeignKeyViolationError:  # selected since it was checked
        raise RegistryConflict(f"Agent Profile {row['name']!r} is still selected: select another profile there "
                               "first") from None
    return {"deleted": profile_id}
