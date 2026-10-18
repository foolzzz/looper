"""A profile's custom prompt leads every composed prompt without changing empty defaults."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from looper.agents.agent_settings import HARNESS_MAX_CHARS, Boundaries, Harness, Layer
from looper.agents.layer_effort import Before, Derived, _apply, convert
from looper.agents.profile_registry import BUILTIN_PROFILE, RegistryError
from looper.agents.system_prompt import build_role_prompt, build_system_prompt, compose_system_prompt, sha256_text
from looper.agents.task_roles import RoleKind, TaskRole


def _build(kind: str, **kwargs: Any) -> Any:
    common = {"harness": Harness("HARNESS", Layer.SYSTEM), "contract_name": "CONTRACT", "contract": "CONTRACT",
              "task": "## Task\nTASK", **kwargs}
    if kind == "role":
        return build_role_prompt(**common, boundaries=Boundaries("BOUNDARIES", Layer.SYSTEM),
                                 task_role=TaskRole("tester", RoleKind.WORK, 1, "ROLE"))
    return build_system_prompt(**common, role_id="worker", role="ROLE", recommended=False)


@pytest.mark.parametrize("empty", ["", " \n\t "])
@pytest.mark.parametrize("kind", ["system", "role"])
def test_empty_custom_prompt_keeps_text_and_layers_identical(kind: str, empty: str) -> None:
    before, after = _build(kind), _build(kind, custom_prompt=empty)
    assert after.text == before.text
    assert after.layers == before.layers
    assert "custom_prompt" not in after.layers
    assert compose_system_prompt("ROLE", harness="HARNESS", custom_prompt=empty) == compose_system_prompt(
        "ROLE", harness="HARNESS")


@pytest.mark.parametrize("kind", ["system", "role"])
def test_custom_prompt_is_stripped_and_precedes_every_other_layer(kind: str) -> None:
    prompt = _build(kind, custom_prompt=" \nCUSTOM\nSECOND LINE\n ")
    assert prompt.text == "CUSTOM\nSECOND LINE\n\n" + _build(kind).text
    assert prompt.text.startswith("CUSTOM\nSECOND LINE\n\nHARNESS\n\nROLE")
    assert prompt.text.endswith("CONTRACT")
    assert prompt.layers["custom_prompt"] == {"sha256": sha256_text("CUSTOM\nSECOND LINE")}
    assert compose_system_prompt("ROLE", harness="HARNESS", custom_prompt=" CUSTOM ") == "CUSTOM\n\nHARNESS\n\nROLE"


@pytest.mark.parametrize("value", [None, True, 1, [], {}])
def test_custom_prompt_requires_text(value: Any) -> None:
    from looper.agents.profile_registry import _custom_prompt

    with pytest.raises(RegistryError, match="custom_prompt must be a string"):
        _custom_prompt(value)


def test_custom_prompt_limit_and_whitespace() -> None:
    from looper.agents.profile_registry import _custom_prompt

    assert _custom_prompt(" \n ") == ""
    assert _custom_prompt(" " + "x" * HARNESS_MAX_CHARS + " ") == "x" * HARNESS_MAX_CHARS
    with pytest.raises(RegistryError, match=str(HARNESS_MAX_CHARS)):
        _custom_prompt("x" * (HARNESS_MAX_CHARS + 1))


def test_derived_profile_identity_and_insert_preserve_custom_prompt() -> None:
    custom = replace(BUILTIN_PROFILE, id="custom", name="custom", custom_prompt="CUSTOM")
    plain_high = replace(BUILTIN_PROFILE, id="plain-high", effort="high")
    key = (Layer.SYSTEM, None, None)
    before = {key: Before(custom, "high")}
    conversion = convert(before, [custom, plain_high])
    assert isinstance(conversion.layers[key], Derived)
    inserted = []

    async def insert(fields: dict[str, Any], **_kwargs: Any) -> str:
        inserted.append(fields)
        return "derived"

    class Connection:
        async def execute(self, *_args: Any) -> None:
            pass

    asyncio.run(_apply(Connection(), before, conversion, insert))
    assert inserted[0]["custom_prompt"] == "CUSTOM"


def test_manager_profile_tool_exposes_and_returns_custom_prompt() -> None:
    from looper.agents.ops.config import TOOLS, _profile_listed

    tool = next(tool for tool in TOOLS if tool.name == "looper_agent_profiles")
    assert tool.schema["properties"]["custom_prompt"]["type"] == "string"
    view = {"profile_id": "profile", "name": "profile", "runtime_name": "Claude Code", "provider_name": "login",
            "model": "sonnet", "effort": None, "agent_teams": False, "system_prompt_mode": "append",
            "custom_prompt": "CUSTOM", "roles": [], "builtin": False,
            "used_by": {"layers": [], "task_count": 0, "schedule_count": 0, "allowed_in": []}}
    assert _profile_listed(view)["custom_prompt"] == "CUSTOM"
    assert replace(BUILTIN_PROFILE, custom_prompt="CUSTOM").record()["custom_prompt"] == "CUSTOM"


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("task_role", [False, True])
def test_worker_launch_uses_its_selected_profiles_custom_prompt(
    monkeypatch: pytest.MonkeyPatch, agent_db: Any, verify: bool, task_role: bool,
) -> None:
    from unittest.mock import AsyncMock

    from looper.agents.agent_settings import AgentSettings
    from looper.agents.tiers import worker
    from tests.test_agent_planner_worker import _run_step

    settings = AgentSettings(harness=Harness("HARNESS", Layer.SYSTEM),
                             profile=replace(BUILTIN_PROFILE, custom_prompt="CUSTOM"))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    extra = {}
    if task_role:
        extra["task_role"] = TaskRole("tester", RoleKind.REVIEW if verify else RoleKind.WORK, 1, "ROLE").payload()
    result, seen = _run_step(verify=verify, **extra)
    if verify:
        assert result["passed"] is True
    else:
        assert result["status"] == "completed"
    assert seen["prompt"].startswith("CUSTOM\n\nHARNESS\n\n")
    metadata = agent_db.start_record.await_args.kwargs["metadata"]
    assert metadata["prompt_layers"]["custom_prompt"] == {"sha256": sha256_text("CUSTOM")}


def test_manager_composes_the_selected_profiles_custom_prompt() -> None:
    from types import SimpleNamespace

    from looper.agents.agent_settings import AgentSettings
    from looper.agents.tiers.manager import ManagerAgent

    settings = AgentSettings(harness=Harness("HARNESS", Layer.SYSTEM),
                             profile=replace(BUILTIN_PROFILE, custom_prompt="CUSTOM"))
    prompt = ManagerAgent._composed_prompt(SimpleNamespace(_system_prompt="ROLE", _prompt_recommended=False), settings)
    assert prompt.text.startswith("CUSTOM\n\nHARNESS\n\nROLE\n\n")
    assert prompt.layers["custom_prompt"] == {"sha256": sha256_text("CUSTOM")}


def test_profile_row_round_trip_and_legacy_default() -> None:
    from looper.agents.profile_registry import AgentProfile
    from tests.test_agent_settings import _row

    profile = replace(BUILTIN_PROFILE, custom_prompt="CUSTOM")
    assert AgentProfile.from_row({**_row(profile), "custom_prompt": "CUSTOM"}).custom_prompt == "CUSTOM"
    assert AgentProfile.from_row(_row(profile)).custom_prompt == ""
