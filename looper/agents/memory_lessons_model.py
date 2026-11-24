"""The project lesson lifecycle, conservative replacement gate and independent settings.

Shared candidate vocabulary belongs to ``memory_model``; only this module owns the
transitions for rows whose destination is ``looper`` (memory-tiers RFC, section 4.3).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .memory_model import (CONFIRM_VIAS, REASONS, Action, Actor, Reason, SourceClass, State,
                           TransitionRefused, Via)

SETTING_KEY = "memory_lessons"

LESSON_TRANSITIONS: Mapping[tuple[State, State], frozenset[Action]] = {
    (State.HELD, State.IN_EFFECT): frozenset({Action.ACTIVATE}),
    (State.HELD, State.REJECTED): frozenset({Action.REJECT}),
    (State.IN_EFFECT, State.HELD): frozenset({Action.HOLD}),
    (State.IN_EFFECT, State.REVOKED): frozenset({Action.REVOKE}),
    (State.IN_EFFECT, State.IN_EFFECT): frozenset({Action.RENEW, Action.REINFORCE}),
    (State.IN_EFFECT, State.SUPERSEDED): frozenset({Action.SUPERSEDE}),
    (State.IN_EFFECT, State.EXPIRED): frozenset({Action.EXPIRE}),
    (State.HELD, State.EXPIRED): frozenset({Action.EXPIRE}),
}


def check_lesson_transition(current: State | str, to: State | str, action: Action | str, *,
                            actor: Actor | str = Actor.SYSTEM, via: Via | str = Via.SYSTEM,
                            reason: Reason | str | None = None,
                            source_class: SourceClass | str = SourceClass.AGENT) -> None:
    """Refuse an illegal lesson edge, event actor or activation source.

    ``reason`` describes the destination, so entering ``held`` requires one of its
    shared reasons. Storage clears a previous held reason when leaving that state.
    """
    current, to, action = State(current), State(to), Action(action)
    actor, via = Actor(actor), Via(via)
    if action not in LESSON_TRANSITIONS.get((current, to), ()):
        raise TransitionRefused(f"a {current} lesson cannot go to {to} by {action}", state=current)
    if action in {Action.REJECT, Action.REVOKE, Action.RENEW, Action.PROMOTE} and actor is not Actor.USER:
        raise TransitionRefused(f"only the operator can {action} a lesson", state=current)
    if action is Action.HOLD and actor is not Actor.SYSTEM:
        raise TransitionRefused("only the system can hold a lesson", state=current)
    if action is Action.ACTIVATE and not (actor is Actor.USER and via in CONFIRM_VIAS):
        raise TransitionRefused("a held lesson requires an operator review in dashboard or DM to activate",
                                state=current)
    if to is State.IN_EFFECT and SourceClass(source_class) is not SourceClass.AGENT:
        raise TransitionRefused("only an agent-source lesson may be in effect", state=current)
    if to is State.HELD and reason not in REASONS[State.HELD]:
        raise TransitionRefused("a held lesson needs a held reason", state=current)


# Preserve the complete fragments as well as their numeric/identifier parts.
# Separators around prose do not become part of a path or URL.
_CODE = re.compile(r"`+[^`\n]+`+")
_URL = re.compile(r"https?://[^\s`<>\"'，。；、）]+")
_PATH = re.compile(r"[^\s`<>\"'，。；、（）()\[\]{},;!?]+/[^\s`<>\"'，。；、（）()\[\]{},;!?]+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?%?")
_DATETIME = re.compile(r"\d{1,4}(?:[-/]\d{1,2}){1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}(?::\d{2})?")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*")


def protected_tokens(statement: str, why: str = "") -> frozenset[str]:
    """Extract NFKC-normalized facts a proposed automatic replacement must retain."""
    text = unicodedata.normalize("NFKC", f"{statement}\n{why}")
    tokens = {match.group() for pattern in (_CODE, _NUMBER, _DATETIME) for match in pattern.finditer(text)}
    tokens.update(match.group().rstrip(".,;:!?)]}") for pattern in (_URL, _PATH) for match in pattern.finditer(text))
    tokens.update(match.group() for match in _IDENTIFIER.finditer(text)
                  if any(char.isdigit() or char in "_.-" or char.isupper() for char in match.group()))
    return frozenset(tokens)


def overwrite_allowed(old_statement: str, old_why: str, new_statement: str, new_why: str) -> bool:
    """A conservative permission to replace text, never a claim that the text is true."""
    new = unicodedata.normalize("NFKC", f"{new_statement}\n{new_why}")
    return all(token in new for token in protected_tokens(old_statement, old_why))


@dataclass(frozen=True)
class InjectSettings:
    planner: bool = True
    worker: bool = True
    reviewer: bool = False


@dataclass(frozen=True)
class WeeklySettings:
    enabled: bool = True
    weekday: int = 1
    time: str = "09:30"
    push_channel: str | None = None


@dataclass(frozen=True)
class LessonSettings:
    enabled: bool = False
    auto_activate: bool = False
    inject: InjectSettings = field(default_factory=InjectSettings)
    max_lessons: int = 12
    max_chars: int = 3000
    project_cap: int = 60
    valid_days: int = 90
    wrong_threshold: int = 2
    min_journal_entries: int = 3
    retro_budget_usd: float = 0.05
    daily_budget_usd: float = 0.50
    weekly: WeeklySettings = field(default_factory=WeeklySettings)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _object(raw: Any, defaults: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be an object")
    values = asdict(defaults)
    if unknown := set(raw) - values.keys():
        raise ValueError(f"{name} has unknown keys: {', '.join(sorted(map(str, unknown)))}")
    return values | dict(raw)


def _boolean(values: Mapping[str, Any], key: str) -> None:
    if not isinstance(values[key], bool):
        raise ValueError(f"{key} must be true or false")


def parse_settings(raw: Mapping[str, Any] | None) -> LessonSettings:
    """Validate replacement settings, filling only omitted fields from the defaults.

    Channel identity and private-chat reachability need live configuration and are
    validated by the operator settings service before it stores this value.
    """
    values = _object({} if raw is None else raw, LessonSettings(), SETTING_KEY)
    for key in ("enabled", "auto_activate"):
        _boolean(values, key)
    for key in ("max_lessons", "max_chars", "project_cap", "valid_days", "wrong_threshold", "min_journal_entries"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or key == "max_lessons" and value > 50:
            raise ValueError(f"{key} must be a positive integer" + (" no greater than 50" if key == "max_lessons" else ""))
    for key in ("retro_budget_usd", "daily_budget_usd"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a positive finite number")
        try:
            number = float(value)
        except OverflowError:
            raise ValueError(f"{key} must be a positive finite number") from None
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} must be a positive finite number")
        values[key] = number
    inject = _object(values["inject"], InjectSettings(), "inject")
    for key in inject:
        _boolean(inject, key)
    weekly = _object(values["weekly"], WeeklySettings(), "weekly")
    _boolean(weekly, "enabled")
    weekday = weekly["weekday"]
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 1 <= weekday <= 7:
        raise ValueError("weekly.weekday must be an ISO weekday from 1 to 7")
    time = weekly["time"]
    if not isinstance(time, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time) is None:
        raise ValueError("weekly.time must be a 24-hour time in HH:mm format")
    channel = weekly["push_channel"]
    if channel is not None and (not isinstance(channel, str) or not channel.strip() or any(c in channel for c in "\0\r\n")):
        raise ValueError("weekly.push_channel must be a channel id or null")
    values["inject"] = InjectSettings(**inject)
    values["weekly"] = WeeklySettings(**weekly)
    return LessonSettings(**values)
