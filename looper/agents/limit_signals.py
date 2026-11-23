"""Optional provider quota observations; no admission, retries or failure decisions.

Read at the stream boundary, before a processor's transcript filter. No model
content, prompts or run metadata is changed. Claude reads the switch per signal;
the Codex app-server reader caches it for five seconds before emitting internal
events, so disabling collection takes effect in running processes too.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import provider_limits as db_limits, profile_registry, system_settings

logger = logging.getLogger(__name__)
KEY = "engine.limit_signals.enabled"
CODEX_EVENT = "provider_limit_signal"
_STATUS = {"allowed": "allowed", "allowed_warning": "warning", "rejected": "rejected"}
_NAME = re.compile(r"[a-z0-9_]{1,40}\Z")
_warning_counts: Counter[str] = Counter()


def _warn(kind: str, message: str) -> None:
    # Process-local totals in the warning log expose rejected signals without their payloads.
    _warning_counts[kind] += 1
    logger.warning("%s (count=%d)", message, _warning_counts[kind])


async def enabled() -> bool:
    return (await system_settings.get_settings([KEY])).get(KEY) == "true"


async def settings_view() -> dict[str, bool]:
    return {"enabled": await enabled()}


async def update_settings(body: Mapping[str, Any]) -> dict[str, bool]:
    if set(body) != {"enabled"} or (body["enabled"] is not None and type(body["enabled"]) is not bool):
        raise ValueError("enabled must be true, false or null (default false)")
    value = body["enabled"]
    await system_settings.put_settings({KEY: None if value is None else str(value).lower()})
    return await settings_view()


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid utilization")
    return float(value)


def _reset(value: Any, now: datetime) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        at = datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return at if now < at <= now + timedelta(days=8) else None


def _window(raw: Mapping[str, Any], *, status: str, source: str, now: datetime, codex: bool = False) -> dict[str, Any]:
    if codex:
        raw = {k: v for k, v in raw.items() if v is not None}
    fields: dict[str, Any] = {"status": status, "source": source, "observed_at": now}
    key = "usedPercent" if codex else "utilization"
    if key in raw:
        fields["utilization"] = None if raw[key] is None else min(1., _number(raw[key]) / (100 if codex else 1))
    if "resetsAt" in raw and (reset := _reset(raw["resetsAt"], now)) is not None:
        fields["resets_at"] = reset
    if codex and "windowDurationMins" in raw:
        minutes = raw["windowDurationMins"]
        if minutes is not None and (type(minutes) is not int or not 0 < minutes <= 2147483647):
            raise ValueError("invalid window duration")
        fields["window_minutes"] = minutes
    return fields


def parse(event: Mapping[str, Any], *, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Valid windows only. Bad siblings cannot erase good observations; no raw text is retained."""
    now = now or datetime.now(timezone.utc)
    found: dict[str, dict[str, Any]] = {}
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info")
        if not isinstance(info, Mapping):
            return []
        top_status = _STATUS.get(info.get("status")) if isinstance(info.get("status"), str) else None
        windows = info.get("unifiedWindows")
        candidates = dict(windows) if isinstance(windows, Mapping) else {}
        name = info.get("rateLimitType")
        if isinstance(name, str):
            # A top-level summary must not replace the fuller unified window.
            nested = candidates.get(name)
            candidates[name] = {**info, **(nested if isinstance(nested, Mapping) else {})}
        for name, raw in candidates.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(raw, Mapping):
                continue
            status = _STATUS.get(raw.get("status")) if isinstance(raw.get("status"), str) else (
                top_status if name == info.get("rateLimitType") else None)
            try:
                if status is None:
                    if "status" in raw or raw.get("utilization") is None:
                        continue
                    used = _number(raw["utilization"])
                    status = "rejected" if used >= 1 else "warning" if used >= .9 else "allowed"
                found[name] = _window(raw, status=status, source="claude_stream", now=now)
            except (TypeError, ValueError, OverflowError):
                _warn("parse", "ignored malformed Claude limit window")
    elif event.get("type") == CODEX_EVENT:
        limits = event.get("rateLimits")
        if not isinstance(limits, Mapping):
            return []
        for name in ("primary", "secondary"):
            raw = limits.get(name)
            if not isinstance(raw, Mapping):
                continue  # null/absent window is a sparse update, not a reset
            try:
                used = _number(raw["usedPercent"])
                status = "rejected" if used >= 100 else "warning" if used >= 90 else "allowed"
                found[name] = _window(raw, status=status, source="codex_app_server", now=now, codex=True)
            except (KeyError, TypeError, ValueError, OverflowError):
                _warn("parse", "ignored malformed Codex limit window")
    return list(found.items())


async def observe(event: Mapping[str, Any], provider_id: str, record_id: int | None) -> None:
    if event.get("type") not in ("rate_limit_event", CODEX_EVENT):
        return
    try:
        # Internal Codex events have already passed the reader's cached switch.
        if event.get("type") != CODEX_EVENT and not await enabled():
            return
        windows = parse(event)
        if not windows:
            _warn("parse", "ignored provider limit signal without valid windows")
        try:
            for name, fields in windows:
                await db_limits.upsert(provider_id, name, {**fields, "record_id": record_id})
        finally:
            from . import admission

            if admission.enabled():
                await admission.observed(provider_id, windows, record_id)
    except Exception:
        # No payload or exception text: either may contain provider/account details.
        _warn("write", "could not save provider limit signal")


async def limits(provider_id: str | None = None) -> dict[str, Any]:
    from . import admission

    accounts = await profile_registry.list_providers()
    if provider_id is not None:
        accounts = [p for p in accounts if p["id"] == provider_id]
        if not accounts:
            raise LookupError("provider not found")
    active = await enabled()
    windows = await db_limits.windows(provider_id) if active else []
    usage = await db_limits.usage(provider_id) if active else []
    return {"enabled": active, "providers": [
        {"provider_id": p["id"], "name": p["name"], "admission": await admission.snapshot(p["id"]) if admission.enabled() else None,
         "cost_basis": "api_equivalent" if p["auth"] == "oauth" else "reported",
         "windows": [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in w.items()
                      if k != "provider_id"} for w in windows if w["provider_id"] == p["id"]],
         "usage": [{k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in u.items()
                    if k != "provider_id"} for u in usage if u["provider_id"] == p["id"]]}
        for p in accounts]}
