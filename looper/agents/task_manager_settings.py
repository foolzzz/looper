"""The Task Manager's independent role and per-task Agent Profile selection.

Task profile overrides keep the selection independent of Manager sessions.
"""

from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from ..db import agent_setting_layers as db_layers, profile_registry as db_registry, role_configs as db_role_configs
from .agent_settings import AgentSettings, AgentType, Layer, load_agent_settings
from .ops import ErrorCode, OpError
from .profile_registry import AgentProfile, RegistryError, builtin_profile, check_selection

DEFAULT_PROFILE_ID = builtin_profile("task_manager").id


async def seed_defaults(pool: Any = None) -> None:
    """Insert the role once; never overwrite the operator's saved profile."""
    await db_role_configs.seed_defaults([
        {"role": "task_manager", "display_name": "Task Manager", "timeout_seconds": 0}], pool=pool)


async def load_task_manager_settings(task: Mapping[str, Any]) -> AgentSettings:
    """Read the independent role and task profile on every turn; never borrow a Manager's profile."""
    return await load_agent_settings(AgentType.TASK_MANAGER, project_id=task.get("project_id"),
                                     task_id=task["id"], manager_session=False)


def _check_runtime(profile: Any) -> None:
    if profile.runtime.type != "claude-code":
        raise OpError(ErrorCode.TASK_MANAGER_RUNTIME_UNSUPPORTED,
                      f"Profile {profile.name}: the task Manager runs only on Claude Code for now.",
                      details={"requested_runtime": profile.runtime.type, "supported": "claude_code"})


async def validate_task_manager_profile(profile_id: str | None) -> None:
    """Validate before creating or writing; clearing a choice must also yield a supported role profile."""
    if profile_id is None:
        chosen = await load_task_manager_settings({"id": None, "project_id": None})
        if chosen.error:
            raise OpError(ErrorCode.UNAVAILABLE, chosen.error)
        _check_runtime(chosen.profile)
        profile_id = chosen.profile.id
    try:
        profile = await check_selection(profile_id, ["task_manager"])
    except RegistryError as exc:
        # Even a runtime lacking role capabilities has the same typed runtime refusal.
        if row := await db_registry.get_profile(profile_id):
            _check_runtime(AgentProfile.from_row(row))
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    _check_runtime(profile)


async def task_manager_profile_get(task: Mapping[str, Any]) -> dict[str, Any]:
    """Read the task Manager's own effective profile and the registry's role-compatible choices."""
    chosen = await load_task_manager_settings(task)
    if chosen.error:
        raise OpError(ErrorCode.UNAVAILABLE, chosen.error)
    overrides = await db_layers.profile_overrides(task["id"])
    choices = []
    for row in await db_registry.list_profiles():
        try:
            profile = await check_selection(row["id"], ["task_manager"])
        except RegistryError:
            continue
        choices.append({"profile_id": profile.id, "label": profile.name,
                        "model": row["model"]["name"], "runtime": profile.runtime.type})
    profile = chosen.profile
    return {"task_id": task["id"], "override_profile_id": overrides.get("task_manager", {}).get("id"),
            "effective": {"profile_id": profile.id, "model": chosen.model, "runtime": profile.runtime.type,
                          "provider": profile.provider.id, "effort": chosen.effort or None},
            "source": "default" if chosen.profile_source is Layer.BUILTIN else chosen.profile_source.value,
            "choices": choices}


async def set_task_manager_profile(task_id: str, profile_id: str | None) -> None:
    """Select a registered profile for this task only; None restores the role's default.

    The pool observes the changed profile in its next launch signature and resumes
    the existing session. Validation shares the turn admission gate with persistence.
    """
    from .tiers.task_manager import current_pool

    pool = current_pool()
    async with AsyncExitStack() as stack:
        if pool is not None:
            # Share the admission gate: validation and persistence may yield.
            await stack.enter_async_context(pool._lock)
            if pool.in_turn(task_id):
                raise OpError(ErrorCode.TASK_MANAGER_BUSY,
                              "the task Manager is working in this task: stop its turn first")
        await validate_task_manager_profile(profile_id)
        await db_layers.set_task_layer(task_id, profiles={"task_manager": profile_id})
        if pool is not None and (agent := pool.agents.get(task_id)) is not None:
            # Idle only: no driver needs joining, and admission stays excluded until stop finishes.
            await agent.resident.stop(kill=True)
