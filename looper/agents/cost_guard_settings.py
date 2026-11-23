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
UNANSWERED_INTS = {"window_days": (1, 30), "min_pushes": (1, 1000), "pause_after_days": (1, 30), "snooze_days": (1, 365)}
UNANSWERED_FIELDS = ("enabled", *UNANSWERED_INTS)
KEYS = (PREFIX + "mode", *(PREFIX + cap for cap in CAPS),
        *(f"{PREFIX}roles.{role}.{cap}" for role in ROLES for cap in CAPS),
        *(f"{PREFIX}unanswered_pushes.{name}" for name in UNANSWERED_FIELDS))


class Mode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class RoleLimits:
    daily_usd: float | None = None
    daily_equivalent_usd: float | None = None


@dataclass(frozen=True)
class UnansweredRule:
    enabled: bool = False
    window_days: int = 3
    min_pushes: int = 15
    pause_after_days: int = 3
    snooze_days: int = 30


@dataclass(frozen=True)
class Config:
    mode: Mode = Mode.OFF
    daily_usd: float | None = None
    daily_equivalent_usd: float | None = None
    roles: dict[str, RoleLimits] = field(default_factory=dict)
    unanswered: UnansweredRule = field(default_factory=UnansweredRule)


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


def _unanswered_value(name: str, value: Any) -> str:
    if name == "enabled":
        if not isinstance(value, bool):
            raise ValueError("unanswered_pushes.enabled must be true or false")
        return "true" if value else "false"
    low, high = UNANSWERED_INTS[name]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"unanswered_pushes.{name} must be an integer from {low} to {high}")
    return str(value)


def checked(body: Mapping[str, Any]) -> dict[str, str | None]:
    """Validate the whole patch before any setting is written."""
    if not body or any(key not in ("mode", *CAPS, "roles", "unanswered_pushes") for key in body):
        raise ValueError("give mode, daily_usd, daily_equivalent_usd or roles; unknown settings are refused")
    values: dict[str, str | None] = {}
    if "mode" in body:
        raw = body["mode"]
        if raw is not None and raw not in tuple(mode.value for mode in Mode):
            raise ValueError("mode must be off, observe, enforce or null")
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
    if "unanswered_pushes" in body:
        raw = body["unanswered_pushes"]
        if not isinstance(raw, Mapping) or any(key not in UNANSWERED_FIELDS for key in raw):
            raise ValueError("unanswered_pushes must contain only enabled, window_days, min_pushes, "
                             "pause_after_days or snooze_days")
        if raw.get("enabled") is True:
            from .chat_settings import metrics_enabled

            if not metrics_enabled():
                raise ValueError("先打开 manager.metrics.enabled：没有推送和回复记录就无法判断没人回")
        for name, item in raw.items():
            values[f"{PREFIX}unanswered_pushes.{name}"] = _unanswered_value(name, item)
    if not values:
        raise ValueError("give at least one setting")
    return values


def from_stored(stored: Mapping[str, str]) -> Config:
    mode = Mode(stored.get(PREFIX + "mode", Mode.OFF.value))

    def cap(key):
        return _cap(float(stored[key]), key) if key in stored else None

    roles = {role: RoleLimits(**{name: cap(f"{PREFIX}roles.{role}.{name}") for name in CAPS})
             for role in ROLES if any(f"{PREFIX}roles.{role}.{name}" in stored for name in CAPS)}

    def unanswered_raw(name: str) -> str | None:
        return stored.get(f"{PREFIX}unanswered_pushes.{name}")

    enabled_raw = unanswered_raw("enabled")
    if enabled_raw not in (None, "true", "false"):
        raise ValueError("unanswered_pushes.enabled must be true or false")
    numbers: dict[str, int] = {}
    for name, (low, high) in UNANSWERED_INTS.items():
        raw = unanswered_raw(name)
        if raw is None:
            continue
        if not raw.isdecimal():
            raise ValueError(f"unanswered_pushes.{name} must be an integer from {low} to {high}")
        number = int(raw)
        if not low <= number <= high:
            raise ValueError(f"unanswered_pushes.{name} must be an integer from {low} to {high}")
        numbers[name] = number
    unanswered = UnansweredRule(enabled_raw == "true", **numbers)
    return Config(mode, *(cap(PREFIX + name) for name in CAPS), roles, unanswered)


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


def _unanswered_view(rule: UnansweredRule) -> dict[str, Any]:
    return {"enabled": rule.enabled, "window_days": rule.window_days, "min_pushes": rule.min_pushes,
            "pause_after_days": rule.pause_after_days, "snooze_days": rule.snooze_days}


def view() -> dict[str, Any]:
    return {"mode": _config.mode.value, "daily_usd": _config.daily_usd,
            "daily_equivalent_usd": _config.daily_equivalent_usd,
            "roles": {name: {cap: getattr(limits, cap) for cap in CAPS}
                      for name, limits in _config.roles.items()},
            "unanswered_pushes": _unanswered_view(_config.unanswered), "problem": _problem}


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
        elif name.startswith("unanswered_pushes."):
            field_name = name.split(".", 1)[1]
            entry.setdefault("unanswered_pushes", {})[field_name] = (
                raw == "true" if field_name == "enabled" else int(raw))
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
