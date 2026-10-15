"""The effort is the Agent Profile's: ``layer_effort.convert`` keeps what every run gets.

For each set of layers as the old rule read them (a layer's effort won over
its profile's), every role at every place runs, after the conversion and under
the new rule (``agent_settings.resolve``: the effort is the profile's), the
same runtime, provider, model, agent teams and effort; converting the result
again changes nothing. The old rule is written out here (``_old``), apart from
the code under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.agent_settings import AgentType, Layer, LayerValues, resolve
from looper.agents.layer_effort import Before, Derived, convert
from looper.agents.profile_registry import BUILTIN_PROFILE, BUILTIN_PROVIDER, BUILTIN_RUNTIME, AgentProfile

S = (Layer.SYSTEM, None, None)
ROLES = tuple(AgentType)
TASK_ROLES = (AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER)
P = "proj-a"


def _profile(name: str, model: str, effort: str | None = None, *, agent_teams: bool = True) -> AgentProfile:
    return AgentProfile(f"agent-{name}", name, BUILTIN_RUNTIME, BUILTIN_PROVIDER, model, effort, agent_teams)


SONNET, HAIKU = _profile("claude-code/sonnet", "sonnet"), _profile("claude-code/haiku", "haiku")
CATALOG = [BUILTIN_PROFILE, SONNET, HAIKU]


def _role(role: AgentType) -> tuple[Layer, AgentType, None]:
    return Layer.ROLE, role, None


def _in_project(role: AgentType | None) -> tuple[Layer, AgentType | None, str]:
    return (Layer.ROLE_PROJECT, role, P) if role else (Layer.PROJECT, None, P)


def _old(layers: dict[Any, Before], role: AgentType | None, project: str | None) -> tuple[Any, ...]:
    """What ``role`` (None: the system layer alone) ran in ``project`` under the old rule: the last profile and the
    last effort its layers set, else the profile's own."""
    chain = [S] if role is None else [S, _role(role)]
    if project and role not in (None, AgentType.MANAGER):
        chain += [(Layer.PROJECT, None, project), (Layer.ROLE_PROJECT, role, project)]
    profile, effort = BUILTIN_PROFILE, None
    for key in chain:
        before = layers.get(key, Before())
        profile, effort = before.profile or profile, before.effort or effort
    return profile.runtime.id, profile.provider.id, profile.model, profile.agent_teams, effort or profile.effort


def _new(layers: dict[Any, AgentProfile | None], role: AgentType | None, project: str | None) -> tuple[Any, ...]:
    index = {(scope, r): LayerValues(profile=profile) for (scope, r, _), profile in layers.items()}
    settings = resolve(role, index, project)
    profile = settings.profile
    return profile.runtime.id, profile.provider.id, profile.model, profile.agent_teams, settings.effort or None


def _converted(layers: dict[Any, Before]) -> tuple[dict[Any, AgentProfile | None], dict[Any, str | None], list[str]]:
    """The layers after ``convert`` (what each selects), the names of the changed ones' profiles, and the names of
    the profiles it adds."""
    conversion = convert(layers, CATALOG)
    added = {d.name: AgentProfile(f"agent-{d.name}", d.name, d.base.runtime, d.base.provider, d.base.model, d.effort,
                                  d.base.agent_teams) for d in conversion.derived}

    def real(choice: Any) -> AgentProfile | None:
        return added[choice.name] if isinstance(choice, Derived) else choice

    after = {key: before.profile for key, before in layers.items()}
    after.update({key: real(choice) for key, choice in conversion.layers.items()})
    changed = {key: choice.name if choice else None for key, choice in conversion.layers.items()}
    return after, changed, list(added)


SCENARIOS: dict[str, tuple[dict[Any, Before], dict[Any, str | None], list[str]]] = {
    # The maintainer's install: the built-in profile with xhigh for everyone, and high for the Workers.
    "maintainer": (
        {S: Before(BUILTIN_PROFILE, "xhigh"), _role(AgentType.WORKER): Before(effort="high")},
        {S: "claude-code/opus · xhigh", _role(AgentType.WORKER): "claude-code/opus · high"},
        ["claude-code/opus · xhigh", "claude-code/opus · high"],
    ),
    # A role's profile ran with the system's effort, which won over the profile's own.
    "role profile under a system effort": (
        {S: Before(effort="high"), _role(AgentType.PLANNER): Before(SONNET),
         _role(AgentType.REVIEWER): Before(_profile("claude-code/opus-max", "opus", "max"))},
        {S: "claude-code/opus · high", _role(AgentType.PLANNER): "claude-code/sonnet · high",
         _role(AgentType.REVIEWER): "claude-code/opus · high"},  # opus with high, as the system layer takes it
        ["claude-code/opus · high", "claude-code/sonnet · high"],
    ),
    # A project's effort over three roles that run three profiles: the project selects one, the others their own.
    "project effort over differing role profiles": (
        {_role(AgentType.PLANNER): Before(SONNET), _role(AgentType.WORKER): Before(HAIKU),
         _in_project(None): Before(effort="low")},
        {_in_project(None): "claude-code/sonnet · low", _in_project(AgentType.WORKER): "claude-code/haiku · low",
         _in_project(AgentType.REVIEWER): "claude-code/opus · low"},
        ["claude-code/sonnet · low", "claude-code/haiku · low", "claude-code/opus · low"],
    ),
    # A project's profile with the role layers' efforts: what most roles need, and the Workers their own.
    "project profile under role efforts": (
        {S: Before(effort="xhigh"), _role(AgentType.WORKER): Before(effort="high"), _in_project(None): Before(SONNET),
         _in_project(AgentType.REVIEWER): Before(HAIKU)},
        {S: "claude-code/opus · xhigh", _role(AgentType.WORKER): "claude-code/opus · high",
         _in_project(None): "claude-code/sonnet · xhigh", _in_project(AgentType.WORKER): "claude-code/sonnet · high",
         _in_project(AgentType.REVIEWER): "claude-code/haiku · xhigh"},
        ["claude-code/opus · xhigh", "claude-code/opus · high", "claude-code/sonnet · xhigh",
         "claude-code/haiku · xhigh", "claude-code/sonnet · high"],
    ),
    # A role's effort in a project, on the profile it inherits.
    "role in a project effort": (
        {_role(AgentType.WORKER): Before(SONNET), _in_project(AgentType.WORKER): Before(effort="max")},
        {_in_project(AgentType.WORKER): "claude-code/sonnet · max"},
        ["claude-code/sonnet · max"],
    ),
    # An effort the role runs with already: the layer's effort goes, and it keeps its profile or selects none.
    "redundant efforts": (
        {S: Before(effort="high"), _role(AgentType.WORKER): Before(effort="high"),
         _role(AgentType.REVIEWER): Before(_profile("w", "sonnet", "low"), "low"), _in_project(None): Before()},
        {S: "claude-code/opus · high", _role(AgentType.WORKER): None, _role(AgentType.REVIEWER): "w"},
        ["claude-code/opus · high"],
    ),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_role_at_every_place_runs_the_profile_and_effort_it_ran(name: str) -> None:
    layers, changed, added = SCENARIOS[name]
    after, conversion_changed, conversion_added = _converted(layers)
    assert (conversion_changed, conversion_added) == (changed, added)
    places: list[tuple[AgentType | None, str | None]] = [(None, None), *((role, None) for role in ROLES),
                                                         *((role, P) for role in TASK_ROLES)]
    for role, project in places:
        assert _new(after, role, project) == _old(layers, role, project), (role, project)
    # Converted again (nothing sets an effort now, and the profiles it added are registered), nothing changes.
    again = convert({key: Before(profile) for key, profile in after.items()},
                    [*CATALOG, *(p for p in after.values() if p and p.name in added)])
    assert (again.layers, again.derived) == ({}, [])


def test_an_existing_profile_like_it_is_reused_and_a_taken_name_gets_a_number() -> None:
    opus_high = _profile("fast opus", "opus", "high")
    taken = _profile("claude-code/sonnet · high", "sonnet", "high", agent_teams=False)  # not like sonnet
    conversion = convert({S: Before(effort="high"), _role(AgentType.WORKER): Before(SONNET)},
                         [*CATALOG, opus_high, taken])
    assert {key: choice.name for key, choice in conversion.layers.items()} == {
        S: "fast opus", _role(AgentType.WORKER): "claude-code/sonnet · high (2)"}
    assert [(d.base, d.effort) for d in conversion.derived] == [(SONNET, "high")]


def test_a_tasks_choice_keeps_the_effort_its_layers_gave_it() -> None:
    layers = {S: Before(effort="high"), _in_project(AgentType.WORKER): Before(effort="low")}
    conversion = convert(layers, CATALOG, {"task-a": (P, {AgentType.WORKER: SONNET, AgentType.REVIEWER: HAIKU}),
                                           "task-b": (None, {AgentType.WORKER: SONNET})})
    assert {key: choice.name for key, choice in conversion.tasks.items()} == {
        ("task-a", AgentType.WORKER): "claude-code/sonnet · low",
        ("task-a", AgentType.REVIEWER): "claude-code/haiku · high",
        ("task-b", AgentType.WORKER): "claude-code/sonnet · high"}
