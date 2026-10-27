"""The profile and role choices shared by interactive and scheduled template execution."""
from __future__ import annotations

from typing import Any, Callable, Mapping

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


def validate_role_dependencies(template: TaskTemplate, project: Mapping[str, Any],
                               rows: Mapping[str, Mapping[str, Any]], allowed: set[str], items: set[str],
                               profile_check: Callable[[str, str], None], *, interactive: bool = False,
                               fallbacks: list[str] | None = None) -> list[dict[str, Any]]:
    """Validate stored or projected dependencies; only interactive creation drops stale optional choices."""
    from .task_roles import RoleError, check_definition, role_key

    keys = template.role_templates
    execution = ProjectExecution.from_row(project)
    if len(set(keys)) != len(keys):
        duplicate = next(k for k in keys if keys.count(k) > 1)
        raise TemplateError(f"the template {template.name} lists the role template {duplicate} twice")
    if keys and not execution.roles_on:
        raise TemplateError("the template's project has Planner-defined roles off")
    roles = []
    for key in keys:
        row = rows.get(key)
        if row is None:
            raise TemplateError(f"project {project['name']} has no role template {key} any more")
        try:
            role_key(key)
            kind, _, _ = check_definition(row["kind"], row["role_prompt"], row["harness"], execution.role_text_limit)
        except RoleError as exc:
            raise TemplateError(str(exc)) from None
        profile = row["profile_id"]
        if profile:
            try:
                if allowed and profile not in allowed:
                    raise RegistryError(f"role {key}'s Agent Profile is not on the project's list")
                profile_check(profile, kind.agent.value)
            except RegistryError as exc:
                if not interactive:
                    raise TemplateError(str(exc)) from None
                profile = None
                if fallbacks is not None:
                    fallbacks.append(f"{key}：原 Agent Profile 已不可选，使用内置 {kind.agent.value.capitalize()} 的常规 Profile")
        selected_items = list(row["items"] or ())
        if set(selected_items) - items:
            if not interactive:
                raise TemplateError(f"role {key} names items no longer installed for Planner-defined roles in this project")
            selected_items = [item for item in selected_items if item in items]
            if fallbacks is not None:
                fallbacks.append(f"{key}：已移除未安装的角色条目（{len(row['items']) - len(selected_items)} 项）")
        roles.append({"role_key": key, **{k: row[k] for k in ("kind", "role_prompt", "harness")},
                      "profile_id": profile, "items": selected_items, "template_key": key})
    return roles


async def template_roles(template: TaskTemplate, project: Mapping[str, Any], *, pool: Any = None,
                         interactive: bool = False, fallbacks: list[str] | None = None) -> list[dict[str, Any]]:
    keys = template.role_templates
    allowed = await db_projects.allowed_profiles(project["id"], pool=pool) if keys else []
    items = {r["id"] for r in await db_library.list_role_items(project["id"], pool=pool)} if keys else set()
    for key in sorted(set(keys)):
        if hasattr(pool, "is_in_transaction") and pool.is_in_transaction():
            await pool.fetchrow("SELECT template_key FROM project_role_templates "
                                "WHERE project_id=$1 AND template_key=$2 FOR SHARE", project["id"], key)
    rows, errors = {}, {}
    for key in keys:
        row = await db_role_templates.get_template(project["id"], key, pool=pool)
        if row is None:
            continue
        rows[key] = row
        if row["profile_id"]:
            role = "worker" if row["kind"] == "work" else "reviewer"
            try:
                await check_selection(row["profile_id"], [role], pool=pool)
            except RegistryError as exc:
                errors[(row["profile_id"], role)] = str(exc)

    def check(profile_id: str, role: str) -> None:
        if error := errors.get((profile_id, role)):
            raise RegistryError(error)

    return validate_role_dependencies(template, project, rows, {p["id"] for p in allowed}, items, check,
                                      interactive=interactive, fallbacks=fallbacks)
