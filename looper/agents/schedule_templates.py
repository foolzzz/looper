"""Resolve static schedule template selection once for validation, gates and task creation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..db import connection, managers, projects, schedules
from .project_execution import ProjectExecution
from .task_templates import KEY_PATTERN, TaskTemplate, TemplateError, check_values, render_requirement
from .task_template_execution import merged_profiles, template_roles
from .tiers.task import resolve_work_dir


class MissingDirectory(TemplateError):
    """Placement needs an explicit or inherited working directory."""


class ConfigurationChanged(TemplateError):
    """A gate result no longer belongs to the current schedule configuration."""


def selection_patch(data: Mapping[str, Any]) -> dict[str, Any]:
    fields = {k: data[k] for k in ("template_key", "template_inputs") if k in data}
    key = fields.get("template_key")
    if key is not None and (not isinstance(key, str) or not re.fullmatch(KEY_PATTERN, key)):
        raise TemplateError(f"template_key must match {KEY_PATTERN}, or be null")
    if "template_inputs" in fields:
        values = fields["template_inputs"]
        if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
            raise TemplateError("template_inputs must be an object of static strings")
    if "template_key" in fields and key is None:
        fields["template_inputs"] = {}
    return fields


def merge_selection(stored: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    key = patch.get("template_key", stored.get("template_key"))
    if "template_inputs" in patch and not key and not ("template_key" in patch and patch["template_key"] is None):
        raise TemplateError("template_inputs go with a template_key")
    previous = stored.get("template_inputs") or {} if key == stored.get("template_key") else {}
    inputs = patch.get("template_inputs", previous)
    if "template_key" in patch and key is None:
        inputs = {}
    return {"template_key": key, "template_inputs": inputs}


def template_directory(template: TaskTemplate | None, schedule: Mapping[str, Any], project_dir: str = "") -> str:
    if template:
        if template.project_id and template.project_id != schedule["project_id"]:
            raise TemplateError(f"template {template.name} fixes a different project")
        if template.work_dir and schedule.get("work_dir") and resolve_work_dir(template.work_dir) != schedule["work_dir"]:
            raise TemplateError(f"template {template.name} sets work_dir to {template.work_dir}")
    return (template.work_dir if template else "") or schedule.get("work_dir") or project_dir


@dataclass
class EffectiveSchedule:
    template: TaskTemplate | None
    values: dict[str, str]
    work_dir: str
    workspace_mode: str
    profiles: dict[str, Any]
    roles: list[dict[str, Any]]
    requirement: str


async def effective_schedule(conn: Any, schedule: Mapping[str, Any], *, enabled: bool = True) -> EffectiveSchedule:
    conn = conn or connection.get_pool()
    key = schedule.get("template_key")
    if error := await schedules.owner_error(conn, schedule["manager_id"], enabled=enabled, template_key=key):
        raise TemplateError(error)
    # Hold the placement and template rows through the firing's writes. Manager template edits also
    # lock the owner, so the owner SHARE above serializes policy/template updates with creation.
    if hasattr(conn, "is_in_transaction") and conn.is_in_transaction():
        await conn.fetchrow("SELECT id FROM projects WHERE id=$1 FOR SHARE", schedule["project_id"])
    project = await projects.get_project(schedule["project_id"], pool=conn)
    if project is None:
        raise TemplateError("the schedule's project no longer exists")
    template = None
    values = schedule.get("template_inputs") or {}
    if key:
        if hasattr(conn, "is_in_transaction") and conn.is_in_transaction():
            await conn.fetchrow("SELECT key FROM manager_task_templates WHERE manager_id=$1 AND key=$2 FOR SHARE",
                                schedule["manager_id"], key)
        row = await managers.get_template(schedule["manager_id"], key, pool=conn)
        if row is None:
            raise TemplateError(f"schedule owner {schedule['manager_id']} has no template {key!r}")
        template = TaskTemplate.from_row(row)
        values = await check_values(template, values)
    elif values:
        raise TemplateError("template_inputs go with a template_key")
    work_dir = template_directory(template, schedule, project["work_dir"])
    if not work_dir:
        raise MissingDirectory("a work directory is required: set one on the schedule, template or its project")
    # Preserve the existing default path's exact requirement/profile behavior.
    profiles = dict(schedule.get("profile_overrides") or {})
    roles = []
    requirement = schedule["requirement"]
    if template:
        profiles = await merged_profiles(template, profiles, pool=conn, project=project)
        roles = await template_roles(template, project, pool=conn)
        requirement = render_requirement(template, values, requirement)
        from .schedules import REQUIREMENT_MAX_CHARS
        if len(requirement) > REQUIREMENT_MAX_CHARS:
            raise TemplateError(f"the rendered schedule requirement has at most {REQUIREMENT_MAX_CHARS} characters")
    mode = template.workspace_mode if template and template.workspace_mode else ProjectExecution.from_row(project).task_mode.value
    return EffectiveSchedule(template, values, work_dir, mode, profiles, roles, requirement)


def fingerprint(schedule: Mapping[str, Any], effective: EffectiveSchedule) -> str:
    import dataclasses
    import json
    settings = {k: schedule.get(k) for k in (
        "manager_id", "project_id", "requirement", "work_dir", "template_key", "template_inputs",
        "profile_overrides", "task_limits", "gate_command", "gate_timeout_seconds", "run_timeout_mode",
        "run_timeout_seconds", "run_budget_usd", "daily_budget_usd", "cron", "timezone", "overlap",
        "max_parallel_runs", "catch_up")}
    resolved = dataclasses.asdict(effective)
    if resolved["template"]:
        # Restoring the same execution settings must match despite the edit's timestamp.
        resolved["template"].pop("updated_at", None)
    return json.dumps({"settings": settings, "effective": resolved}, sort_keys=True, default=str)
