"""The default Manager's persona (system setting ``manager.persona``).

``section`` is a pure renderer. ``manager_persona_context`` compiles the live value as the default Manager's
first state section, omitting the address and trimming by message audience and session isolation.

Empty, including whitespace-only, is unset. The value is kept in memory: ``load`` at startup, ``update`` after a
write. An unreadable stored value loads as unset and logs ``manager.persona is unreadable`` once per payload.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..db import system_settings as db_settings

logger = logging.getLogger(__name__)

PERSONA_KEY = "manager.persona"
FIELDS = ("name", "address", "tone", "notes")
LIMITS = {"name": 20, "address": 20, "tone": 200, "notes": 400}
TOTAL = 600
HEADING = "Persona (set by the operator: your name, how you address them, your tone; Looper's rules still apply):"
_LINES = (
    ("name", "Name"),
    ("address", "Address the operator as"),
    ("tone", "Tone"),
    ("notes", "Notes"),
)


@dataclass(frozen=True)
class Persona:
    name: str = ""
    address: str = ""
    tone: str = ""
    notes: str = ""


_current = Persona()
_warned: str | None = None


def current() -> Persona:
    return _current


def set_current(value: Persona) -> None:
    """Apply a value in memory only (``load`` and ``update`` store it; tests use this directly)."""
    global _current
    _current = value


def payload(persona: Persona) -> dict[str, str]:
    """The stored object: only fields that still have text."""
    return {name: getattr(persona, name) for name in FIELDS if getattr(persona, name)}


def encoded(persona: Persona) -> str | None:
    """The ``system_settings`` value, or None when unset (the row is deleted)."""
    body = payload(persona)
    if not body:
        return None
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checked(raw: Any) -> Persona:
    """``raw`` as a persona. Missing fields and null are empty. Strings are stripped before the limits."""
    if not isinstance(raw, Mapping):
        raise ValueError("manager.persona must be a JSON object")
    if unknown := [key for key in raw if key not in FIELDS]:
        raise ValueError(f"unknown field {unknown[0]!r}")
    values: dict[str, str] = {}
    for name in FIELDS:
        if name not in raw or raw[name] is None:
            values[name] = ""
            continue
        value = raw[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        value = value.strip()
        limit = LIMITS[name]
        if len(value) > limit:
            raise ValueError(f"{name} is {len(value)} characters; the limit is {limit}")
        values[name] = value
    persona = Persona(**values)
    total = sum(len(getattr(persona, name)) for name in FIELDS)
    if total > TOTAL:
        raise ValueError(f"manager.persona is {total} characters; the limit is {TOTAL}")
    return persona


def view() -> dict[str, str]:
    """What get and update return: every field, empty when unset."""
    return {name: getattr(_current, name) for name in FIELDS}


def _line(value: str) -> str:
    """Keep field text on one prompt line without controls or backtick fences; storage is unchanged."""
    text = "".join(" " if unicodedata.category(char) in {"Cc", "Cf"} else char for char in value)
    text = " ".join(text.split())
    return re.sub(r"`{3,}", lambda match: " ".join(match[0]), text)


def section(persona: Persona, *, group: bool = False) -> list[str]:
    """The persona block, or ``[]`` when it would have no lines.

    ``group`` keeps only the name and the address (a later group-session trim). The heading is the same.
    An empty persona, or a group persona with neither of those lines, is ``[]``. The block ends with a blank line.
    """
    chosen = ("name", "address") if group else FIELDS
    lines = [f"  {label}: {_line(getattr(persona, name))}"
             for name, label in _LINES if name in chosen and getattr(persona, name)]
    if not lines:
        return []
    return [HEADING, *lines, ""]


def _remember_unreadable(raw: str) -> None:
    global _warned
    set_current(Persona())
    if _warned == raw:
        return
    logger.warning("manager.persona is unreadable")
    _warned = raw


async def load() -> Persona:
    """Read the stored persona. Missing is unset. Unreadable is unset and does not raise."""
    global _warned
    stored = await db_settings.get_settings([PERSONA_KEY])
    raw = stored.get(PERSONA_KEY)
    if raw is None:
        set_current(Persona())
        _warned = None
        return _current
    try:
        persona = checked(json.loads(raw))
    except ValueError:
        _remember_unreadable(raw)
        return _current
    set_current(persona)
    _warned = None
    return _current


async def update(body: Mapping[str, Any]) -> dict[str, str]:
    """Merge ``body`` onto the stored persona and store it. Omitted fields stay; null clears one.

    A corrupt row loads as unset, so the fields this body sets replace it. An empty result deletes the row.
    """
    global _warned
    if not isinstance(body, Mapping):
        raise ValueError("manager.persona must be a JSON object")
    if unknown := [key for key in body if key not in FIELDS]:
        raise ValueError(f"unknown field {unknown[0]!r}")
    if not body:
        raise ValueError("give at least one of name, address, tone, notes (null clears it)")
    for name in FIELDS:
        if name in body and body[name] is not None and not isinstance(body[name], str):
            raise ValueError(f"{name} must be a string")
    await load()
    merged = {name: getattr(_current, name) for name in FIELDS}
    for name in FIELDS:
        if name not in body:
            continue
        merged[name] = "" if body[name] is None else body[name]
    persona = checked(merged)
    await db_settings.put_settings({PERSONA_KEY: encoded(persona)})
    set_current(persona)
    _warned = None
    return view()


async def export_value(conn: Any) -> dict[str, str] | None:
    """The persona a configuration file carries, or None when unset or unreadable (the key is left out)."""
    if not await conn.fetchval("SELECT to_regclass('system_settings') IS NOT NULL"):
        return None
    raw = (await db_settings.get_settings([PERSONA_KEY], pool=conn)).get(PERSONA_KEY)
    if raw is None:
        return None
    try:
        body = payload(checked(json.loads(raw)))
    except ValueError:
        return None
    return body or None


async def write(persona: Persona, *, conn: Any) -> None:
    """Store ``persona`` on ``conn`` (a configuration import's transaction). Empty deletes the row."""
    text = encoded(persona)
    if text is None:
        await conn.execute("DELETE FROM system_settings WHERE key = $1", PERSONA_KEY)
        return
    await conn.execute(
        "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        PERSONA_KEY, text)
