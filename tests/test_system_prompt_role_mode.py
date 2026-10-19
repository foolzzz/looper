"""The system prompt mode is a per-role setting that applies on Claude Code only."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents import claude_cli
from looper.agents.agent_settings import (
    BUILTIN_SETTINGS, BUILTIN_SYSTEM_PROMPT_MODE, AgentSettings, AgentType, Launch, Layer, LayerValues, SettingsError,
    layer_update, resolve, role_system_prompt_mode,
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


@pytest.mark.parametrize("runtime_type", ["codex", "grok", "omp"])
@pytest.mark.parametrize("role", list(AgentType))
@pytest.mark.parametrize("mode", [APPEND, REPLACE])
def test_other_runtimes_keep_their_profiles_mode(role: AgentType, runtime_type: str, mode: SystemPromptMode) -> None:
    profile = replace(BUILTIN_PROFILE, system_prompt_mode=mode,
                      runtime=replace(BUILTIN_PROFILE.runtime, type=runtime_type))
    for layers in ({}, _layers(role, REPLACE), _layers(role, APPEND)):  # the role's setting is Claude Code's only
        settings = replace(resolve(role, layers), profile=profile)
        assert Launch(settings, role=role).system_prompt_mode is mode
    assert Launch(AgentSettings(profile=profile)).system_prompt_mode is mode


def test_a_launch_without_a_role_appends_on_claude_code() -> None:
    launch = Launch(AgentSettings(system_prompt_mode=REPLACE))
    assert launch.role is None and launch.system_prompt_mode is APPEND


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
