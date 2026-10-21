"""The Manager's ``/profile`` command: parse it and list the Agent Profiles the Manager's session can switch to.

A choice (RFC §3 D1-D5) is per session, not per channel: it is stored with the Manager's current session, takes
effect on the next message, and "0" returns to following the settings. A profile on the same runtime and provider
resumes the session; any other starts a new one, with a handoff. Looper answers the command itself, like ``/new``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import profile_registry
from .agent_settings import AgentSettings, AgentType, profile_launch_problem
from .profile_registry import AgentProfile, ResolvedAgentProfile
from .runtimes import RuntimeUnsupported, adapter, can_resume

logger = logging.getLogger(__name__)

# Like ``tiers.manager.NEW_SESSION_COMMAND``: only this exact first word (any case) is the command.
PROFILE_COMMAND = "/profile"


class ProfileCommandKind(str, Enum):
    LIST = "list"
    SELECT = "select"
    FOLLOW_SETTINGS = "follow_settings"


@dataclass(frozen=True)
class ProfileCommand:
    kind: ProfileCommandKind
    ref: str | None = None


def parse_profile_command(text: str) -> ProfileCommand | None:
    """The command ``text`` is, or None when it is a message for the model."""
    head, _, rest = text.strip().partition(" ")
    if not head or head.lower() != PROFILE_COMMAND:
        return None
    rest = rest.strip()
    if not rest:
        return ProfileCommand(ProfileCommandKind.LIST)
    if rest == "0":
        return ProfileCommand(ProfileCommandKind.FOLLOW_SETTINGS)
    return ProfileCommand(ProfileCommandKind.SELECT, rest)


class Effect(str, Enum):
    CURRENT = "current"
    RESUMES = "resumes"
    NEW_SESSION = "new_session"
    STARTS = "starts"


def effect(session: Mapping[str, Any] | None, current_profile_id: str, candidate: AgentProfile) -> Effect:
    """What choosing ``candidate`` does to the saved ``session``."""
    if candidate.id == current_profile_id:
        return Effect.CURRENT
    if not session or not session.get("session_id"):
        return Effect.STARTS
    try:
        runtime = adapter(candidate.runtime.type)
    except RuntimeUnsupported:
        return Effect.NEW_SESSION
    if can_resume(session.get("runtime_type"), session.get("provider_id"), runtime, candidate.provider.id):
        return Effect.RESUMES
    return Effect.NEW_SESSION


@dataclass(frozen=True)
class SessionProfileResult:
    """What a choice of the Manager's session profile did: ``effect`` on the saved session, the profile now
    chosen (None: follow the settings) with its display ``name``, the choice before it and the reply ``text``."""

    effect: Effect
    profile_id: str | None
    name: str
    previous_profile_id: str | None
    text: str


def display_name(profile_view: Mapping[str, Any]) -> str:
    """The dashboard's label: the name, then ``· effort`` unless the name already ends with it."""
    name, effort = profile_view["name"], profile_view.get("effort")
    return f"{name} · {effort}" if effort and not name.endswith(effort) else name


@dataclass(frozen=True)
class ProfileChoice:
    number: int
    profile_id: str
    name: str
    runtime: str
    provider: str
    model: str
    effect: Effect
    problem: str | None
    raw_name: str = ""


class ProfileRefError(ValueError):
    pass


async def choices(session: Mapping[str, Any] | None, effective: AgentSettings) -> list[ProfileChoice]:
    """The profiles that can serve the Manager, by display name, numbered from 1."""
    view = await profile_registry.registry_view()
    from ..db import profile_registry as db

    listed = sorted((p for p in view["profiles"] if AgentType.MANAGER.value in p.get("roles", ())),
                    key=display_name)
    result: list[ProfileChoice] = []
    for number, item in enumerate(listed, 1):
        try:
            row = await db.get_profile(item["profile_id"])
            candidate = ResolvedAgentProfile.from_row(row) if row else None
        except Exception:
            logger.warning("cannot read Agent Profile %s", item["profile_id"], exc_info=True)
            candidate = None
        if candidate is None:
            continue
        result.append(ProfileChoice(
            len(result) + 1, item["profile_id"], display_name(item), item["runtime_name"], item["provider_name"],
            item["model"]["name"], effect(session, effective.profile.id, candidate),
            await profile_launch_problem(candidate, AgentType.MANAGER), item["name"]))
    return result


def _menu(listed: list[ProfileChoice]) -> str:
    return "\n".join(f"{c.number}. {c.name}" for c in listed)


def resolve_ref(ref: str, listed: list[ProfileChoice]) -> ProfileChoice:
    """The choice ``ref`` names: a number, an id, a name, or a case-insensitive name matching exactly one."""
    ref = ref.strip()
    if ref.isdigit():
        found = [c for c in listed if c.number == int(ref)]
    else:
        found = ([c for c in listed if c.profile_id == ref]
                 or [c for c in listed if ref in (c.name, c.raw_name)]
                 or [c for c in listed if ref.lower() in (c.name.lower(), c.raw_name.lower())])
    if len(found) == 1:
        return found[0]
    reason = "matches more than one" if found else "matches none"
    raise ProfileRefError(f"{ref!r} {reason} of the Agent Profiles:\n{_menu(listed)}")


def format_list(listed: list[ProfileChoice], *, settings_name: str, selected: str | None) -> str:
    current = "（当前）"
    lines = [f"0. 跟随设置（现在是 {settings_name}）{current if selected is None else ''}"]
    for c in listed:
        line = f"{c.number}. {c.name} · {c.runtime} · {c.provider}"
        if selected is not None and c.profile_id == selected:
            line += current
        if c.problem:
            line += f"——不可用：{c.problem}"
        lines.append(line)
    lines.append("发送 /profile <编号> 切换；切换在下一条消息生效，所有渠道共用这条会话。")
    return "\n".join(lines)
