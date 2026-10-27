"""The profile and role choices shared by interactive and scheduled template execution."""
from __future__ import annotations

from typing import Any, Mapping

from ..db import projects as db_projects, role_templates as db_role_templates, library as db_library
from .profile_registry import RegistryError, check_selection
from .project_execution import ProjectExecution
from .task_templates import TaskTemplate, TemplateError


class TemplateConflict(TemplateError):
    """A dependency of a saved template can no longer execute."""


def fixed_profiles(template: TaskTemplate | None, profiles: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(profiles)
    if template is not None:
        for role, profile in template.profiles.items():
            if merged.get(role) and merged[role] != profile:
                raise TemplateError(f"the template {template.name} sets the {role}'s Agent Profile to {profile}")
            merged[role] = profile
    return merged


async def merged_profiles(template: TaskTemplate | None, profiles: Mapping[str, Any], *,
                          pool: Any = None, project: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = fixed_profiles(template, profiles)
    allowed = await db_projects.allowed_profiles(project["id"], pool=pool) if project else []
    for role, profile in merged.items():
        if not profile:
            continue
        if allowed and profile not in {p["id"] for p in allowed}:
            raise TemplateError(f"{role} Agent Profile {profile!r} is not on the project's list")
        try:
            await check_selection(profile, [role], pool=pool)
        except RegistryError as exc:
            raise TemplateConflict(f"the template {template.name if template else 'schedule'}'s {role} Agent Profile can no longer run the {role}: {exc}") from None
    return merged


async def template_roles(template: TaskTemplate, project: Mapping[str, Any], *,
                         pool: Any = None) -> list[dict[str, Any]]:
    keys = template.role_templates
    if len(set(keys)) != len(keys):
        duplicate = next(k for k in keys if keys.count(k) > 1)
        raise TemplateError(f"the template {template.name} lists the role template {duplicate} twice")
    if keys and not ProjectExecution.from_row(project).roles_on:
        raise TemplateError("the template's project has Planner-defined roles off")
    allowed = await db_projects.allowed_profiles(project["id"], pool=pool) if keys else []
    items = {r["id"] for r in await db_library.list_role_items(project["id"], pool=pool)} if keys else set()
    roles = []
    for key in sorted(keys):
        if hasattr(pool, "is_in_transaction") and pool.is_in_transaction():
            await pool.fetchrow("SELECT template_key FROM project_role_templates "
                                "WHERE project_id=$1 AND template_key=$2 FOR SHARE", project["id"], key)
    for key in keys:
        row = await db_role_templates.get_template(project["id"], key, pool=pool)
        if row is None:
            raise TemplateError(f"project {project['name']} has no role template {key} any more")
        if row["profile_id"]:
            if allowed and row["profile_id"] not in {p["id"] for p in allowed}:
                raise TemplateError(f"role {key}'s Agent Profile is not on the project's list")
            try:
                await check_selection(row["profile_id"], ["worker" if row["kind"] == "work" else "reviewer"], pool=pool)
            except RegistryError as exc:
                raise TemplateError(str(exc)) from None
        if set(row["items"] or ()) - items:
            raise TemplateError(f"role {key} names items no longer installed for Planner-defined roles in this project")
        roles.append({"role_key": key, **{k: row[k] for k in
                      ("kind", "profile_id", "role_prompt", "harness")},
                      "items": list(row["items"] or ()), "template_key": key})
    return roles
