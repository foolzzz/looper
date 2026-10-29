"""The effort is the Agent Profile's: moving the effort that layers set onto profiles, once.

Settings layers used to set an effort too, and a layer's effort won over its
profile's: a role ran its profile with the last effort its layers set, else the
profile's own. Now a run's effort is its profile's, and no layer sets one.
``convert`` keeps what every run gets: for every place a layer governs (the
system layer, each role, and each task role of a project with layers, at the
project and at the role in the project) and every task's choice of profile, it
finds the profile and effort the old rule gave, and selects on each layer the
profile that gives the same under the new one. That is the profile itself when
its own effort is that effort, else one like it (the same runtime, provider,
model and agent teams) with that effort: an existing one, or one it adds,
named ``<profile> · <effort>``. A project layer applies to three roles that may
need different profiles; it selects the one most of them need, and each other
role gets its own on its role-in-project layer.

The pure conversion remains available for legacy configuration projections.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .agent_settings import TASK_ROLES, AgentType, Layer
from .profile_registry import BUILTIN_PROFILE, AgentProfile, LegacyAgentProfile, RegistryError, check_roles

logger = logging.getLogger(__name__)

Key = tuple[Layer, AgentType | None, str | None]  # a layer: its scope, its role and its project
SYSTEM: Key = (Layer.SYSTEM, None, None)


@dataclass(frozen=True)
class Before:
    """What a layer set under the old rule: its profile and its effort, ``None`` where it set none."""

    profile: AgentProfile | None = None
    effort: str | None = None


NOTHING = Before()


@dataclass(frozen=True)
class Derived:
    """A profile ``convert`` adds: ``base``'s runtime, provider, model and agent teams, with ``effort``."""

    base: AgentProfile
    effort: str
    name: str


Choice = AgentProfile | Derived


def choice_name(choice: Choice | None) -> str | None:
    return None if choice is None else choice.name


def _ident(choice: Choice | None) -> tuple[Any, ...] | None:
    if choice is None:
        return None
    if isinstance(choice, Derived):
        return ("derived", choice.name)
    return ("profile", choice.id)


@dataclass
class Conversion:
    """What changes: each layer that set an effort or now selects another profile, and what it selects (None:
    nothing); each task's choice that changes; and the profiles to add, in order."""

    layers: dict[Key, Choice | None] = field(default_factory=dict)
    tasks: dict[tuple[str, AgentType], Choice] = field(default_factory=dict)
    derived: list[Derived] = field(default_factory=list)


def _model_identity(profile: AgentProfile) -> tuple[str, ...]:
    """The model ``profile`` runs: its provider and model name when it carries them (a resolved profile, or a v2
    one before the model migration), else its model id."""
    if hasattr(profile, "provider"):
        return (profile.provider.id, profile.model)
    return (profile.model_id,)


def _model_fields(profile: AgentProfile) -> dict[str, str]:
    """``profile``'s model as the fields of the profile inserted beside it: its model id, or (a v2 profile, before
    the model migration) its provider and model name."""
    if isinstance(profile, LegacyAgentProfile):
        return {"provider_id": profile.provider.id, "model": profile.model}
    return {"model_id": profile.model_id}


class _Targets:
    """The profile that runs like ``base`` with ``effort``: ``base`` when that is its own effort, else an existing
    one with the same runtime, provider, model, agent teams and that effort (the built-in one first, then by name),
    else a derived one."""

    def __init__(self, profiles: Iterable[AgentProfile]) -> None:
        self.profiles = sorted(profiles, key=lambda p: (p.id != BUILTIN_PROFILE.id, p.name))
        self.names = {p.name for p in self.profiles}
        self.derived: dict[tuple[Any, ...], Derived] = {}

    def __call__(self, base: AgentProfile, effort: str | None) -> Choice:
        if (effort or None) == (base.effort or None):
            return base
        key = (base.runtime.id, _model_identity(base), base.agent_teams, base.custom_prompt, effort)
        for profile in self.profiles:
            if (profile.runtime.id, _model_identity(profile), profile.agent_teams,
                    profile.custom_prompt, profile.effort) == key:
                return profile
        if key not in self.derived:
            name, n = f"{base.name} · {effort}", 2
            while name in self.names:
                name, n = f"{base.name} · {effort} ({n})", n + 1
            self.names.add(name)
            self.derived[key] = Derived(base, str(effort), name)
        return self.derived[key]


def _base(choice: Choice) -> AgentProfile:
    return choice.base if isinstance(choice, Derived) else choice


def _runs_task_roles(choice: Choice) -> bool:
    try:
        check_roles(_base(choice), [role.value for role in TASK_ROLES])
    except RegistryError:
        return False
    return True


def convert(layers: Mapping[Key, Before], profiles: Iterable[AgentProfile],
            tasks: Mapping[str, tuple[str | None, Mapping[AgentType, AgentProfile]]] | None = None) -> Conversion:
    """What to change so that every role at every place, and every task's choice (task id -> its project and the
    profile it chose per role), runs the profile and effort it ran under the old rule. ``profiles``: every
    registered one."""
    profiles = list(profiles)
    builtin = next((p for p in profiles if p.id == BUILTIN_PROFILE.id), BUILTIN_PROFILE)
    target = _Targets(profiles)
    selected: dict[Key, Choice | None] = {key: before.profile for key, before in layers.items()}

    def old(chain: Iterable[Key]) -> Choice:
        profile, effort = builtin, None
        for key in chain:
            before = layers.get(key, NOTHING)
            profile = before.profile or profile
            effort = before.effort or effort
        return target(profile, effort or profile.effort)

    def new(chain: Iterable[Key]) -> Choice:
        choice: Choice = builtin
        for key in chain:
            choice = selected.get(key) or choice
        return choice

    def settle(chain: list[Key]) -> None:
        """Select on the chain's last layer what the old rule gave there, unless the layers above give it: a layer
        that selected a profile keeps selecting one."""
        key = chain[-1]
        want = old(chain)
        selected[key] = None
        if layers.get(key, NOTHING).profile is not None or _ident(want) != _ident(new(chain)):
            selected[key] = want

    settle([SYSTEM])
    for role in AgentType:
        settle([SYSTEM, (Layer.ROLE, role, None)])

    for project in sorted({key[2] for key in layers if key[2] is not None}):
        place: Key = (Layer.PROJECT, None, project)
        chains = {role: [SYSTEM, (Layer.ROLE, role, None), place, (Layer.ROLE_PROJECT, role, project)]
                  for role in TASK_ROLES}
        before = layers.get(place, NOTHING)
        if before.profile is not None or before.effort is not None:
            # The project layer selects what most of its roles need (those that select no profile of their own);
            # each other role gets its own below. A tie keeps the layer's own profile, else selects none. Without
            # a profile of its own, it selects only one that every task role can run.
            options: list[Choice | None] = [] if before.profile is not None else [None]
            for role in TASK_ROLES:
                option = old(chains[role][:3])
                if _ident(option) not in {_ident(o) for o in options} and (
                        before.profile is not None or _runs_task_roles(option)):
                    options.append(option)
            if before.profile is not None:
                options.sort(key=lambda o: _ident(o) != _ident(before.profile))
            inheriting = [role for role in TASK_ROLES if layers.get(chains[role][3], NOTHING).profile is None]

            def misses(option: Choice | None) -> int:
                selected[place] = option
                return sum(_ident(new(chains[role])) != _ident(old(chains[role])) for role in inheriting)

            selected[place] = min(options, key=misses)
        for role in TASK_ROLES:
            settle(chains[role])

    conversion = Conversion()
    for key, choice in selected.items():
        before = layers.get(key, NOTHING)
        if before.effort is not None or _ident(choice) != _ident(before.profile):
            conversion.layers[key] = choice
    for task_id, (project, chosen) in (tasks or {}).items():
        for role, profile in chosen.items():
            chain = [SYSTEM, (Layer.ROLE, role, None)]
            if project:
                chain += [(Layer.PROJECT, None, project), (Layer.ROLE_PROJECT, role, project)]
            effort = next((layers[k].effort for k in reversed(chain) if layers.get(k, NOTHING).effort), None)
            want = target(profile, effort or profile.effort)
            if _ident(want) != _ident(profile):
                conversion.tasks[(task_id, role)] = want
    for choice in [*conversion.layers.values(), *conversion.tasks.values()]:
        if isinstance(choice, Derived) and choice not in conversion.derived:
            conversion.derived.append(choice)
    return conversion


def place_text(key: Key) -> str:
    """A layer in words, for the log: "the system layer", "the worker role layer", "the worker in project p"."""
    scope, role, project = key
    return {Layer.SYSTEM: "the system layer", Layer.ROLE: f"the {role.value if role else ''} role layer",
            Layer.PROJECT: f"the layer of project {project}",
            Layer.ROLE_PROJECT: f"the {role.value if role else ''} layer in project {project}"}[scope]


# --- Applying a conversion ---


async def _apply(conn: Any, layers: Mapping[Key, Before], conversion: Conversion, insert_profile: Any) -> None:
    ids: dict[str, str] = {}
    for derived in conversion.derived:
        base = derived.base
        ids[derived.name] = await insert_profile(
            {"name": derived.name, "runtime_id": base.runtime.id, **_model_fields(base),
             "effort": derived.effort, "agent_teams": base.agent_teams, "custom_prompt": base.custom_prompt},
            pool=conn)
        logger.info("the effort is the Agent Profile's now: added Agent Profile %r (%s with effort %s)",
                    derived.name, base.name, derived.effort)

    def profile_id(choice: Choice | None) -> str | None:
        return None if choice is None else ids[choice.name] if isinstance(choice, Derived) else choice.id

    for key, choice in conversion.layers.items():
        scope, role, project = key
        await conn.execute(
            "INSERT INTO agent_setting_layers (scope, role, project_id, profile_id) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (scope, (COALESCE(role, '')), (COALESCE(project_id, ''))) "
            "DO UPDATE SET profile_id = EXCLUDED.profile_id, updated_at = now()",
            scope.value, role.value if role else None, project, profile_id(choice))
        effort = layers.get(key, NOTHING).effort
        logger.info("the effort is the Agent Profile's now: %s selects %s%s", place_text(key),
                    repr(choice.name) if choice else "no Agent Profile of its own",
                    f" (it set effort {effort})" if effort else "")
    for (task_id, role), choice in conversion.tasks.items():
        await conn.execute("UPDATE task_profile_overrides SET profile_id = $3 WHERE task_id = $1 AND role = $2",
                           task_id, role.value, profile_id(choice))
        logger.info("the effort is the Agent Profile's now: the %s of task %s runs %r", role.value, task_id,
                    choice.name)
