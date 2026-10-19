"""The system prompt mode is a per-role setting that applies on every runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents import claude_cli
from looper.agents.agent_settings import (
    BUILTIN_SETTINGS, BUILTIN_SYSTEM_PROMPT_MODE, AgentSettings, AgentType, Launch, Layer, LayerValues, SettingsError,
    layer_update, resolve, role_system_prompt_mode, system_prompt_mode_choice,
)
from looper.agents.profile_registry import BUILTIN_PROFILE
from looper.agents.system_prompt_mode import SystemPromptMode

APPEND, REPLACE = SystemPromptMode.APPEND, SystemPromptMode.REPLACE


def _layers(role: AgentType, mode: SystemPromptMode | None) -> dict:
    return {(Layer.ROLE, role): LayerValues(system_prompt_mode=mode)}


@pytest.mark.parametrize("role, expected", [
    (AgentType.MANAGER, REPLACE), (AgentType.PLANNER, REPLACE), (AgentType.WORKER, APPEND),
    (AgentType.REVIEWER, APPEND), (None, APPEND),
])
def test_builtin_value_per_role(role: AgentType | None, expected: SystemPromptMode) -> None:
    assert role_system_prompt_mode(role, {}) is expected
    assert role is None or BUILTIN_SYSTEM_PROMPT_MODE[role] is expected
    assert resolve(role, {}).system_prompt_mode is expected


@pytest.mark.parametrize("role", list(AgentType))
@pytest.mark.parametrize("mode", [APPEND, REPLACE])
def test_role_layer_overrides_the_builtin_value(role: AgentType, mode: SystemPromptMode) -> None:
    assert resolve(role, _layers(role, mode)).system_prompt_mode is mode
    other = next(r for r in AgentType if r is not role)
    assert resolve(other, _layers(role, mode)).system_prompt_mode is BUILTIN_SYSTEM_PROMPT_MODE[other]


@pytest.mark.parametrize("scope", [Layer.SYSTEM, Layer.PROJECT, Layer.ROLE_PROJECT])
def test_only_a_role_layer_takes_a_mode(scope: Layer) -> None:
    with pytest.raises(SettingsError, match="set per role"):
        layer_update({"system_prompt_mode": "replace"}, scope, AgentType.WORKER if scope is Layer.ROLE_PROJECT else None)


@pytest.mark.parametrize("raw", ["auto", "default", "Replace", 1, True, ["append"]])
def test_only_append_or_replace(raw: object) -> None:
    with pytest.raises(SettingsError, match='"append" or "replace"'):
        layer_update({"system_prompt_mode": raw}, Layer.ROLE, AgentType.WORKER)


def test_role_layer_accepts_and_unsets() -> None:
    assert layer_update({"system_prompt_mode": "replace"}, Layer.ROLE, AgentType.WORKER) == {
        "system_prompt_mode": "replace"}
    assert layer_update({"system_prompt_mode": None}, Layer.ROLE, AgentType.WORKER) == {"system_prompt_mode": None}


def test_a_stored_value_that_no_longer_validates_is_ignored() -> None:
    assert LayerValues.from_row({"scope": "role", "role": "worker", "system_prompt_mode": "bogus"}) == LayerValues()


def _launch(role: AgentType | None, mode: SystemPromptMode | None, runtime_type: str = "claude-code") -> Launch:
    profile = replace(BUILTIN_PROFILE, runtime=replace(BUILTIN_PROFILE.runtime, type=runtime_type))
    settings = replace(resolve(role, _layers(role, mode) if role and mode else {}), profile=profile)
    return Launch(replace(BUILTIN_SETTINGS, **{k: getattr(settings, k) for k in ("profile", "system_prompt_mode")}),
                  role=role)


@pytest.mark.parametrize("role, mode, expected", [
    (AgentType.MANAGER, None, REPLACE), (AgentType.PLANNER, None, REPLACE),
    (AgentType.WORKER, None, APPEND), (AgentType.REVIEWER, None, APPEND),
    (AgentType.PLANNER, APPEND, APPEND), (AgentType.WORKER, REPLACE, REPLACE),
    (AgentType.REVIEWER, REPLACE, REPLACE), (AgentType.MANAGER, APPEND, APPEND),
])
def test_launch_mode_on_claude_code(role: AgentType, mode: SystemPromptMode | None, expected: SystemPromptMode) -> None:
    launch = _launch(role, mode)
    assert launch.system_prompt_mode is expected
    assert launch.record()["system_prompt_mode"] == expected.value


RUNTIMES = ["claude-code", "codex", "grok", "omp"]


@pytest.mark.parametrize("runtime_type", RUNTIMES)
@pytest.mark.parametrize("role", list(AgentType))
@pytest.mark.parametrize("task, layer", [(None, None), (None, APPEND), (None, REPLACE), (APPEND, REPLACE),
                                         (REPLACE, APPEND), (REPLACE, None), (APPEND, None)])
def test_launch_mode_is_the_same_on_every_runtime(role: AgentType, runtime_type: str, task: SystemPromptMode | None,
                                                  layer: SystemPromptMode | None) -> None:
    layers = {(Layer.TASK, role): LayerValues(system_prompt_mode=task),
              (Layer.ROLE, role): LayerValues(system_prompt_mode=layer)}
    profile = replace(BUILTIN_PROFILE, runtime=replace(BUILTIN_PROFILE.runtime, type=runtime_type))
    settings = replace(resolve(role, layers), profile=profile)
    expected = task or layer or BUILTIN_SYSTEM_PROMPT_MODE[role]
    assert Launch(settings, role=role).system_prompt_mode is expected
    assert Launch(settings, role=role).record()["system_prompt_mode"] == expected.value


@pytest.mark.parametrize("runtime_type", RUNTIMES)
def test_a_launch_without_a_role_appends_on_every_runtime(runtime_type: str) -> None:
    profile = replace(BUILTIN_PROFILE, runtime=replace(BUILTIN_PROFILE.runtime, type=runtime_type))
    assert Launch(AgentSettings(profile=profile, system_prompt_mode=REPLACE)).system_prompt_mode is APPEND


def test_a_runtime_that_cannot_replace_appends(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes import ADAPTERS

    monkeypatch.setattr(ADAPTERS["codex"], "can_replace_system_prompt", False)
    profile = replace(BUILTIN_PROFILE, runtime=replace(BUILTIN_PROFILE.runtime, type="codex"))
    settings = replace(resolve(AgentType.PLANNER, {}), profile=profile)
    assert settings.system_prompt_mode is REPLACE
    assert Launch(settings, role=AgentType.PLANNER).system_prompt_mode is APPEND


def _task_role_layers(role: AgentType) -> dict:
    return {(Layer.TASK_ROLE, role): LayerValues(profile=BUILTIN_PROFILE)}


@pytest.mark.parametrize("kind", ["worker", "reviewer", "task-role-worker", "task-role-reviewer",
                                  "consult-worker", "consult-reviewer", "no-role", "system-settings"])
def test_every_other_claude_code_launch_appends(kind: str) -> None:
    from looper.agents.runtimes import adapter

    role = {"worker": AgentType.WORKER, "reviewer": AgentType.REVIEWER}.get(kind.split("-")[-1])
    layers = _task_role_layers(role) if kind.startswith("task-role") and role else {}
    launch = Launch(resolve(role, layers), role=role, consult=kind.startswith("consult"))
    runtime = adapter("claude-code")
    command = runtime.run_command(runtime.default_executable, cwd=Path("."), prompt_file=Path("prompt.md"),
                                  launch=launch)
    assert "--append-system-prompt-file" in command and "--system-prompt-file" not in command


@pytest.mark.parametrize("role, flag", [(AgentType.PLANNER, "--system-prompt-file"),
                                        (AgentType.MANAGER, "--system-prompt-file"),
                                        (AgentType.WORKER, "--append-system-prompt-file"),
                                        (AgentType.REVIEWER, "--append-system-prompt-file")])
def test_claude_code_argv_follows_the_role(role: AgentType, flag: str) -> None:
    from looper.agents.runtimes import adapter

    launch = _launch(role, None)
    runtime = adapter("claude-code")
    command = runtime.run_command(runtime.default_executable, cwd=Path("."), prompt_file=Path("prompt.md"),
                                  launch=launch)
    assert flag in command
    assert claude_cli.system_prompt_args(Path("prompt.md"), launch.system_prompt_mode)[0] == flag


@pytest.mark.parametrize("role", [AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER])
@pytest.mark.parametrize("task, layer, expected", [
    (None, None, BUILTIN_SYSTEM_PROMPT_MODE), (None, APPEND, APPEND), (None, REPLACE, REPLACE),
    (APPEND, REPLACE, APPEND), (REPLACE, APPEND, REPLACE), (REPLACE, None, REPLACE), (APPEND, None, APPEND),
])
def test_task_choice_beats_the_role_layer_beats_the_builtin(
    role: AgentType, task: SystemPromptMode | None, layer: SystemPromptMode | None, expected,
) -> None:
    layers = {(Layer.TASK, role): LayerValues(system_prompt_mode=task),
              (Layer.ROLE, role): LayerValues(system_prompt_mode=layer)}
    want = BUILTIN_SYSTEM_PROMPT_MODE[role] if expected is BUILTIN_SYSTEM_PROMPT_MODE else expected
    assert resolve(role, layers).system_prompt_mode is want
    assert system_prompt_mode_choice(role, layers)[1] is (
        Layer.TASK if task else Layer.ROLE if layer else Layer.BUILTIN)


def test_the_manager_has_no_task_level_and_other_roles_choices_do_not_leak() -> None:
    # The Manager has no task rows (the table and the API refuse its role), so a task's choices never reach it.
    layers = {(Layer.TASK, AgentType.WORKER): LayerValues(system_prompt_mode=REPLACE)}
    assert resolve(AgentType.MANAGER, layers).system_prompt_mode is REPLACE
    assert resolve(AgentType.REVIEWER, layers).system_prompt_mode is APPEND
    assert resolve(AgentType.PLANNER, layers).system_prompt_mode is REPLACE


def test_a_task_role_seat_follows_its_seat_kind() -> None:
    # A Planner-defined role runs as a Worker (a work seat) or a Reviewer (a review seat): the task's choice for
    # that seat kind applies, with the seat's own profile.
    layers = {**_task_role_layers(AgentType.WORKER), (Layer.TASK, AgentType.WORKER): LayerValues(
        system_prompt_mode=REPLACE), (Layer.TASK, AgentType.REVIEWER): LayerValues(system_prompt_mode=APPEND)}
    assert resolve(AgentType.WORKER, layers).system_prompt_mode is REPLACE
    assert resolve(AgentType.REVIEWER, layers).system_prompt_mode is APPEND


def test_a_consult_appends_whatever_the_seats_mode() -> None:
    layers = {(Layer.TASK, AgentType.WORKER): LayerValues(system_prompt_mode=REPLACE)}
    settings = resolve(AgentType.WORKER, layers)
    assert Launch(settings, role=AgentType.WORKER).system_prompt_mode is REPLACE
    assert Launch(settings, role=AgentType.WORKER, consult=True).system_prompt_mode is APPEND


@pytest.mark.parametrize("role", list(AgentType))
def test_unreadable_settings_keep_the_roles_builtin_mode(role: AgentType, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from looper.agents.agent_settings import load_agent_settings
    from looper.db import agent_setting_layers

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(agent_setting_layers, "list_layers", boom)
    settings = asyncio.run(load_agent_settings(role))
    assert settings.error and settings.system_prompt_mode is BUILTIN_SYSTEM_PROMPT_MODE[role]
