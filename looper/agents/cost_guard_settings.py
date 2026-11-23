"""Operator-owned observation settings, cached before any run is admitted."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..db import system_settings as db_settings

PREFIX = "cost_guard."
ROLES = ("processor", "leads_check", "advice_check", "outbound_check", "advisor")
CAPS = ("daily_usd", "daily_equivalent_usd")
KEYS = (PREFIX + "mode", *(PREFIX + cap for cap in CAPS),
        *(f"{PREFIX}roles.{role}.{cap}" for role in ROLES for cap in CAPS))


class Mode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"


@dataclass(frozen=True)
class RoleLimits:
    daily_usd: float | None = None
    daily_equivalent_usd: float | None = None


@dataclass(frozen=True)
class Config:
    mode: Mode = Mode.OFF
    daily_usd: float | None = None
    daily_equivalent_usd: float | None = None
    roles: dict[str, RoleLimits] = field(default_factory=dict)


_config = Config()
_problem: str | None = None


def cached() -> Config:
    return _config


def _cap(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError(f"{name} must be a nonnegative finite USD number, or null (no cap)")
    return float(value)


def checked(body: Mapping[str, Any]) -> dict[str, str | None]:
    """Validate the whole patch before any setting is written."""
    if not body or any(key not in ("mode", *CAPS, "roles") for key in body):
        raise ValueError("give mode, daily_usd, daily_equivalent_usd or roles; unknown settings are refused")
    values: dict[str, str | None] = {}
    if "mode" in body:
        raw = body["mode"]
        if raw is not None and raw not in (Mode.OFF.value, Mode.OBSERVE.value):
            raise ValueError("mode must be off, observe or null; enforcement is not available")
        values[PREFIX + "mode"] = raw
    for cap in CAPS:
        if cap in body:
            value = _cap(body[cap], cap)
            values[PREFIX + cap] = None if value is None else repr(value)
    if "roles" in body:
        roles = body["roles"]
        if not isinstance(roles, Mapping) or any(role not in ROLES for role in roles):
            raise ValueError("roles must contain only supported background roles")
        for role, limits in roles.items():
            if not isinstance(limits, Mapping) or not limits or any(cap not in CAPS for cap in limits):
                raise ValueError(f"roles.{role} must contain daily_usd or daily_equivalent_usd")
            for cap, raw in limits.items():
                value = _cap(raw, f"roles.{role}.{cap}")
                values[f"{PREFIX}roles.{role}.{cap}"] = None if value is None else repr(value)
    if not values:
        raise ValueError("give at least one setting")
    return values


def from_stored(stored: Mapping[str, str]) -> Config:
    mode = Mode(stored.get(PREFIX + "mode", Mode.OFF.value))

    def cap(key):
        return _cap(float(stored[key]), key) if key in stored else None

    roles = {role: RoleLimits(**{name: cap(f"{PREFIX}roles.{role}.{name}") for name in CAPS})
             for role in ROLES if any(f"{PREFIX}roles.{role}.{name}" in stored for name in CAPS)}
    return Config(mode, *(cap(PREFIX + name) for name in CAPS), roles)


async def load(*, pool: Any = None) -> Config:
    global _config, _problem
    stored = await db_settings.get_settings(KEYS, pool=pool)
    try:
        _config = from_stored(stored)
        _problem = None
    except (ValueError, TypeError):
        _config = Config()
        _problem = "Stored cost guard settings are invalid; the guard is off."
    return _config


def view() -> dict[str, Any]:
    return {"mode": _config.mode.value, "daily_usd": _config.daily_usd,
            "daily_equivalent_usd": _config.daily_equivalent_usd,
            "roles": {name: {cap: getattr(limits, cap) for cap in CAPS}
                      for name, limits in _config.roles.items()}, "problem": _problem}


async def update(body: Mapping[str, Any], *, pool: Any = None) -> dict[str, Any]:
    values = checked(body)
    await db_settings.put_settings(values, pool=pool)
    await load(pool=pool)
    return view()


async def portable(conn: Any) -> dict[str, Any]:
    """Only explicitly stored settings travel; reservations and observations never do."""
    if not await conn.fetchval("SELECT to_regclass('system_settings') IS NOT NULL"):
        return {}
    stored = await db_settings.get_settings(KEYS, pool=conn)
    entry: dict[str, Any] = {}
    for key, raw in stored.items():
        name = key.removeprefix(PREFIX)
        if name == "mode":
            entry[name] = raw
        elif name in CAPS:
            entry[name] = float(raw)
        else:
            _, role, cap = name.split(".")
            entry.setdefault("roles", {}).setdefault(role, {})[cap] = float(raw)
    return entry


async def import_into(importer: Any, entry: Any) -> None:
    """Join the existing importer transaction; validate the full guard patch before writing."""
    from .config_transfer import Action

    try:
        values = checked(entry)
    except ValueError as exc:
        importer.record("settings.cost_guard", "cost_guard", Action.SKIPPED, reason=str(exc))
        return
    current = await importer.conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1::text[])",
                                        list(values))
    stored = {row["key"]: row["value"] for row in current}
    changed = tuple(key for key, value in values.items() if stored.get(key) != value)
    action = importer.outcome("settings.cost_guard", "cost_guard", stored or None, changed)
    if importer.apply and action in (Action.ADDED, Action.CHANGED):
        for key, value in values.items():
            if value is None:
                await importer.conn.execute("DELETE FROM system_settings WHERE key=$1", key)
            else:
                await importer.conn.execute("INSERT INTO system_settings (key,value) VALUES ($1,$2) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", key, value)
