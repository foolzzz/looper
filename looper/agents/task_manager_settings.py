"""The Task Manager's independent role and per-task Agent Profile selection.

Task profile overrides keep the selection independent of Manager sessions.
"""

from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from ..db import agent_setting_layers as db_layers, role_configs as db_role_configs
from .agent_settings import AgentSettings, AgentType, load_agent_settings
from .profile_registry import builtin_profile, check_selection
from .runtimes import RuntimeUnsupported

DEFAULT_PROFILE_ID = builtin_profile("task_manager").id


async def seed_defaults(pool: Any = None) -> None:
    """Insert the role once; never overwrite the operator's saved profile."""
    await db_role_configs.seed_defaults([
        {"role": "task_manager", "display_name": "Task Manager", "timeout_seconds": 0}], pool=pool)


async def load_task_manager_settings(task: Mapping[str, Any]) -> AgentSettings:
    """Read the independent role and task profile on every turn; never borrow a Manager's profile."""
    return await load_agent_settings(AgentType.TASK_MANAGER, project_id=task.get("project_id"),
                                     task_id=task["id"], manager_session=False)


async def set_task_manager_profile(task_id: str, profile_id: str | None) -> None:
    """Select a registered profile for this task only; None restores the role's default.

    The pool observes the changed profile in its next launch signature, restarts
    the process and resumes the existing session. Runtime refusals belong there.
    """
    from .projects import HTTP_409, RequestError
    from .tiers.task_manager import current_pool

    pool = current_pool()
    async with AsyncExitStack() as stack:
        if pool is not None:
            # Share the admission gate: validation and persistence may yield.
            await stack.enter_async_context(pool._lock)
            if pool.in_turn(task_id):
                raise RequestError(HTTP_409, "the task Manager is working in this task: stop its turn first")
        if profile_id is not None:
            profile = await check_selection(profile_id, ["task_manager"])
            if profile.runtime.type != "claude-code":
                raise RuntimeUnsupported(f"Profile {profile.name}: the task Manager runs only on Claude Code for now.")
        await db_layers.set_task_layer(task_id, profiles={"task_manager": profile_id})
        if pool is not None:
            await pool.close(task_id)
