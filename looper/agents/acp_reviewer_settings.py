"""Operator-owned ACP Reviewer command settings; launch always reads a fresh snapshot."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ..db import system_settings as db_settings

PREFIX = "acp_reviewer."
FIELDS = ("enabled", "command", "model", "account", "config_dir", "inspect_roots")
_UNSET = object()


@dataclass(frozen=True)
class AcpReviewerSettings:
    enabled: bool = False
    command: tuple[str, ...] = ()
    model: str = ""
    account: str = ""
    config_dir: str = ""
    inspect_roots: tuple[str, ...] = ()

    def require_ready(self) -> None:
        if not self.enabled:
            raise ValueError("acp_reviewer.enabled is off")
        if len(self.command) < 2:
            raise ValueError("acp_reviewer.command must contain an executable and arguments")
        for name in ("model", "account"):
            if not getattr(self, name):
                raise ValueError(f"acp_reviewer.{name} must be set")

    @property
    def ready(self) -> bool:
        return self.enabled and len(self.command) >= 2 and bool(self.model) and bool(self.account)


def _text(value: Any, name: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"acp_reviewer.{name} must be a string without control characters (at most {maximum} chars)")
    return value.strip()


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"acp_reviewer.{name} must be a JSON array of at most 100 strings")
    result = []
    for entry in value:
        if not isinstance(entry, str) or not entry or len(entry) > 4000 or any(ord(c) < 32 for c in entry):
            raise ValueError(f"acp_reviewer.{name} must contain nonempty strings without control characters")
        result.append(entry)
    return tuple(result)


def checked(values: Mapping[str, Any], current: AcpReviewerSettings | None = None) -> AcpReviewerSettings:
    if not isinstance(values, Mapping) or set(values) - set(FIELDS):
        raise ValueError("unknown acp_reviewer setting")
    changes: dict[str, Any] = {}
    for name, value in values.items():
        if name == "enabled":
            if value is not None and type(value) is not bool:
                raise ValueError("acp_reviewer.enabled must be true, false or null")
            changes[name] = value is True
        elif name in ("command", "inspect_roots"):
            changes[name] = _strings([] if value is None else value, name)
        else:
            changes[name] = _text("" if value is None else value, name, maximum=240 if name in ("model", "account") else 4000)
    config = replace(current or AcpReviewerSettings(), **changes)
    if config.command and len(config.command) < 2:
        raise ValueError("acp_reviewer.command must contain at least two strings")
    if any(arg == "--always-approve" or arg.startswith("--always-approve=") for arg in config.command):
        raise ValueError("acp_reviewer.command must not use --always-approve")
    if config.enabled:
        config.require_ready()
    return config


async def read(*, pool: Any = None) -> AcpReviewerSettings:
    stored = await db_settings.get_settings([PREFIX + name for name in FIELDS], pool=pool)
    values: dict[str, Any] = {}
    for name in FIELDS:
        if PREFIX + name not in stored:
            continue
        value = stored[PREFIX + name]
        if name == "enabled":
            # Only the exact stored value true enables the pilot.
            values[name] = value == "true"
        elif name in ("command", "inspect_roots"):
            try:
                values[name] = json.loads(value)
            except (TypeError, ValueError):
                raise ValueError(f"acp_reviewer.{name} is not a JSON array") from None
        else:
            values[name] = value
    return checked(values)


def check_pairing(config: AcpReviewerSettings, provider: Any, model: str, effort: str | None = None, *,
                  profile_effort: str | None | object = _UNSET) -> None:
    """Match the actual command identity; never borrow another runtime's login or price."""
    config.require_ready()
    if getattr(provider, "oauth_runtime_type", None) != "acp":
        raise ValueError("acp_reviewer.account requires an acp-command provider (oauth_runtime_type acp)")
    if provider.name != config.account:
        raise ValueError("acp_reviewer.account does not match the Profile's account label")
    if model != config.model:
        raise ValueError("acp_reviewer.model does not match the Profile's declared model")
    if effort and profile_effort is not _UNSET and effort != profile_effort:
        raise ValueError("ACP effort must be empty or equal the Profile's declared effort")


async def ensure_registered(config: AcpReviewerSettings, *, pool: Any = None) -> None:
    """ACP has no executable to discover; only an explicitly enabled command registers it."""
    if not config.ready:
        return
    from ..db import profile_registry as db

    await db.insert_builtin_runtime("rt-acp", {
        "name": "acp", "type": "acp", "executable": "", "launcher": "native",
        "capabilities": ["run"], "version": None, "probe_error": None,
    }, pool=pool)


def _policy_view(config: AcpReviewerSettings) -> dict[str, str]:
    from .runtimes.acp_policy import config_check

    # An empty config_dir will be a new scratch at launch, so inspect a fresh empty directory here too.
    with tempfile.TemporaryDirectory(prefix="looper-acp-config-check-") as scratch:
        roots = []
        for root in config.inspect_roots:
            try:
                roots.append(Path(root).expanduser().resolve(strict=True))
            except (OSError, RuntimeError):
                return {"reason": "unresolvable_protected_path", "detail": f"Cannot resolve inspect root: {root}"}
        check = config_check(config.command, config.config_dir or scratch, protected=roots)
        return check.record


async def view(config: AcpReviewerSettings | None = None) -> dict[str, Any]:
    config = config or await read()
    values = asdict(config)
    values["command"] = list(config.command)
    values["inspect_roots"] = list(config.inspect_roots)
    values["policy_check"] = await asyncio.to_thread(_policy_view, config)
    from ..db.connection import get_pool

    latest = await get_pool().fetchval(
        "SELECT metadata->'acp_write_policy' FROM llm_records WHERE metadata ? 'acp_write_policy' "
        "ORDER BY created_at DESC, id DESC LIMIT 1")
    if isinstance(latest, str):
        latest = json.loads(latest)
    values["latest_launch_check"] = ({key: latest[key] for key in ("reason", "detail", "mechanism") if key in latest}
                                     if isinstance(latest, dict) and isinstance(latest.get("reason"), str) else None)
    return values


async def update(values: Mapping[str, Any]) -> dict[str, Any]:
    from ..db.connection import transaction

    async with transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('looper.acp_reviewer_settings'))")
        config = checked(values, await read(pool=conn))
        for name in values:
            value = getattr(config, name)
            stored = json.dumps(value, ensure_ascii=False) if name in ("enabled", "command", "inspect_roots") else value
            await conn.execute(
                "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", PREFIX + name, stored)
        await ensure_registered(config, pool=conn)
    return await view(config)
