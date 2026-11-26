"""The Task Manager's independent role and per-task Agent Profile selection.

Task profile overrides keep the selection independent of Manager sessions.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from enum import Enum
from typing import Any
from uuid import uuid4

from ..db import agent_setting_layers as db_layers, chat, profile_registry as db_registry, role_configs as db_role_configs, tasks
from ..db.connection import get_pool
from .agent_settings import AgentSettings, AgentType, Layer, index_layers, load_agent_settings, resolve
from .ops import ErrorCode, OpError
from .profile_registry import AgentProfile, RegistryError, builtin_profile, check_selection

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_ID = builtin_profile("task_manager").id


class ModelSwitchRejection(str, Enum):
    BUSY = "busy"
    INVALID_PROFILE = "invalid_profile"
    RUNTIME_UNSUPPORTED = "runtime_unsupported"


def _rejection_reason(code: ErrorCode) -> ModelSwitchRejection | None:
    """The closed conversation reason for a profile refusal, or None when the refusal is not one of those three."""
    return {
        ErrorCode.TASK_MANAGER_BUSY: ModelSwitchRejection.BUSY,
        ErrorCode.INVALID: ModelSwitchRejection.INVALID_PROFILE,
        ErrorCode.TASK_MANAGER_RUNTIME_UNSUPPORTED: ModelSwitchRejection.RUNTIME_UNSUPPORTED,
    }.get(code)


async def _effective_profile(task_id: str, *, connection=None) -> str:
    task = await tasks.get_task(task_id, pool=connection)
    layers = await db_layers.list_layers(task.get("project_id"), task_id=task_id, pool=connection)
    return resolve(AgentType.TASK_MANAGER, index_layers(layers, task.get("project_id")), task.get("project_id")).profile.id


async def _save_event(task_id: str, body: Mapping[str, Any], *, connection=None) -> None:
    await chat.save_message(session_id=chat.task_thread(task_id), message_id=f"profile-event:{uuid4().hex}",
                            role="event", content=json.dumps(body, ensure_ascii=False), pool=connection)


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


async def set_task_manager_profile(task_id: str, profile_id: str | None, *,
                                   publish: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None) -> None:
    """Select a registered profile for this task only; None restores the role's default.

    The pool observes the changed profile in its next launch signature and resumes
    the existing session. Validation shares the turn admission gate with persistence.
    A real change records one conversation event; a typed refusal records its reason.
    """
    from .tiers.task_manager import TaskManagerEvent, current_pool

    pool = current_pool()
    async with AsyncExitStack() as stack:
        try:
            if pool is not None:
                # Share the admission gate: validation and persistence may yield.
                await stack.enter_async_context(pool._lock)
                if pool.in_turn(task_id):
                    raise OpError(ErrorCode.TASK_MANAGER_BUSY,
                                  "the task Manager is working in this task: stop its turn first")
            await validate_task_manager_profile(profile_id)
        except OpError as exc:
            # unavailable is not a conversation reason. The typed refusal is still the answer.
            if (reason := _rejection_reason(exc.code)) is not None:
                body = {"kind": TaskManagerEvent.MODEL_SWITCH_REJECTED.value,
                        "profile_id": profile_id, "reason": reason.value}
                try:
                    await _save_event(task_id, body)
                except Exception:
                    # A failed event write must not replace the typed refusal the caller is waiting for.
                    logger.warning("task %s: could not save the model switch refusal", task_id, exc_info=True)
                else:
                    if publish is not None:
                        try:
                            await publish(task_id, body)
                        except Exception:
                            logger.warning("task %s: could not publish the model switch refusal", task_id,
                                           exc_info=True)
            raise
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.fetchval("SELECT id FROM tasks WHERE id = $1 FOR UPDATE", task_id)
            previous = await _effective_profile(task_id, connection=conn)
            await db_layers.set_task_layer(task_id, profiles={"task_manager": profile_id}, pool=conn)
            # Resolve through the same connection so clearing an override uses the effective role default.
            selected = await _effective_profile(task_id, connection=conn)
            changed = previous != selected
            if changed:
                body = {"kind": TaskManagerEvent.MODEL_SWITCHED.value,
                        "from_profile_id": previous, "to_profile_id": selected}
                await _save_event(task_id, body, connection=conn)
        if changed and pool is not None and (agent := pool.agents.get(task_id)) is not None:
            # Idle only: no driver needs joining, and admission stays excluded until stop finishes.
            await agent.resident.stop(kill=True)
        if changed and publish is not None:
            try:
                await publish(task_id, body)
            except Exception:
                # The profile change and the event row are already committed. History can be re-read.
                logger.warning("task %s: could not publish the model switch", task_id, exc_info=True)
