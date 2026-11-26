"""The Task Manager's independent role and per-task Agent Profile selection.

Task profile overrides keep the selection independent of Manager sessions.
"""

from collections.abc import Mapping
from typing import Any

from ..db import agent_setting_layers as db_layers, role_configs as db_role_configs
from .agent_settings import AgentSettings, AgentType, load_agent_settings
from .profile_registry import builtin_profile, check_selection

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
    if profile_id is not None:
        await check_selection(profile_id, ["task_manager"])
    await db_layers.set_task_layer(task_id, profiles={"task_manager": profile_id})
