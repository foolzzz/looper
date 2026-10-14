"""A project's own Claude Code skills: catalog items the project writes, never written into its repository.

A project can write skills (RFC section 6, M4; RFC decision 9). Each is a
catalog item (``library_items``) of origin ``project``, owned by the project
(``owner_project_id``): a name (a lowercase slug, unique among the project's
skills), a one-line description and one file, its ``SKILL.md``, written
from the fields (``skill_file``: frontmatter, then the body). Writing one
also installs it in its project for the Planner, Workers and Reviewers; it can
be installed in other projects too (``catalog.install``), never globally.
Deleting one removes it from the catalog and from every project it is
installed in; deleting its project deletes it.

A run loads it like any installed skill (``library.load_installed``), in the
generated plugin ``project``, so Claude Code names it ``project:<name>``.
Two project skills of one name cannot be installed in one project (409).

Caps, each refused with a message that states it: a name at most
``NAME_MAX_CHARS`` (Claude Code's limit for a skill name), a description at
most ``DESCRIPTION_MAX_CHARS`` (the limit Claude Code's skill format sets; the
longest of 104 real personal skills has 975), a body at most
``CONTENT_MAX_BYTES`` (it enters the context whenever the skill is used; the
largest of those 104 is 61 KB), and at most ``PROJECT_MAX_SKILLS`` skills
written in a project (every installed skill's name and description is listed
in every run's context). Changes to one project's skills hold its row lock, so
two of them cannot both pass a cap or take one name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

NAME_MAX_CHARS = 64
DESCRIPTION_MAX_CHARS = 1024
CONTENT_MAX_BYTES = 64 * 1024
PROJECT_MAX_SKILLS = 20

# The file a project skill is: its SKILL.md.
SKILL_FILE = "SKILL.md"

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIELDS = frozenset({"name", "description", "content", "enabled"})
_CHANGE_FIELDS = frozenset({"name", "description", "content"})


class ProjectSkillError(ValueError):
    """A skill or a request the API refuses (answered with 400)."""


class ProjectSkillNotFound(LookupError):
    """No such project, or no such skill in it (answered with 404)."""


class ProjectSkillConflict(Exception):
    """The project already has a skill of that name (answered with 409)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Validation: each raises ProjectSkillError ---


def skill_name(raw: Any) -> str:
    """A skill's name: a lowercase slug (``deploy-check``) of at most ``NAME_MAX_CHARS``."""
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectSkillError("a skill needs a name")
    name = raw.strip()
    if len(name) > NAME_MAX_CHARS:
        raise ProjectSkillError(f"a skill name has at most {NAME_MAX_CHARS} characters, got {len(name)}")
    if not _NAME.fullmatch(name):
        raise ProjectSkillError(
            f"skill name {name[:40]!r} must be lowercase letters, digits and hyphens, such as deploy-check "
            "(no spaces, no hyphen at either end or twice in a row)"
        )
    return name


def skill_description(raw: Any) -> str:
    """A skill's description: one line, which Claude Code shows the model to decide when to use it."""
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectSkillError("a skill needs a description: one line on what it does and when to use it")
    text = raw.strip()
    if "\n" in text or "\r" in text:
        raise ProjectSkillError("a skill's description is one line")
    if _CONTROL.search(text):
        raise ProjectSkillError("a skill's description cannot hold control characters")
    if len(text) > DESCRIPTION_MAX_CHARS:
        raise ProjectSkillError(f"a skill's description has at most {DESCRIPTION_MAX_CHARS} characters, got {len(text)}")
    return text


def skill_content(raw: Any) -> str:
    """The body of a skill's ``SKILL.md``, without frontmatter (the fields make it), at most ``CONTENT_MAX_BYTES``."""
    if not isinstance(raw, str) or not raw.strip():
        raise ProjectSkillError("a skill needs its content: the instructions of its SKILL.md")
    if "\x00" in raw:
        raise ProjectSkillError("a skill's content cannot hold NUL characters")
    if raw.lstrip("﻿").splitlines()[0].strip() == "---":
        raise ProjectSkillError(
            "give the content without frontmatter: the name and description fields make the SKILL.md frontmatter"
        )
    size = len(raw.encode("utf-8"))
    if size > CONTENT_MAX_BYTES:
        raise ProjectSkillError(
            f"a skill's content has at most {CONTENT_MAX_BYTES // 1024} KiB ({CONTENT_MAX_BYTES} bytes); "
            f"this has {size} bytes: put long reference material in the project's knowledge instead"
        )
    return raw


def _enabled(raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise ProjectSkillError("enabled must be true or false")
    return raw


def skills_label(count: int) -> str:
    """``1 skill``, ``2 skills``."""
    return f"{count} skill{'' if count == 1 else 's'}"


# --- The SKILL.md a skill is written as ---


def _frontmatter(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"


def skill_file(name: str, description: str, content: str) -> str:
    """The ``SKILL.md`` a skill is written as: frontmatter from its fields, then its body.

    The description is a double-quoted string (JSON, which YAML reads the
    same), so a colon, a quote or a ``#`` in it stays text. The schema's
    one-time move of the M4 skills into the catalog writes the same text.
    """
    body = content if content.endswith("\n") else content + "\n"
    return _frontmatter(name, description) + body


def skill_body(text: str, name: str, description: str) -> str:
    """The body of a ``SKILL.md`` that ``skill_file`` wrote for ``name`` and ``description``."""
    head = _frontmatter(name, description)
    return text[len(head):] if text.startswith(head) else text


def skill_md(name: str, description: str, content: str) -> tuple[dict[str, Any], str]:
    """The skill's one file, as ``library_files`` stores it, and the item's version (``library.item_sha256``)."""
    from .library import item_sha256

    text = skill_file(name, description, content)
    file = {"path": SKILL_FILE, "content": text, "sha256": _sha256(text.encode("utf-8")), "executable": False}
    return file, item_sha256([file])


# --- The API: add, read, change and delete a project's skills ---


def skill_caps() -> dict[str, int]:
    return {
        "name_max_chars": NAME_MAX_CHARS,
        "description_max_chars": DESCRIPTION_MAX_CHARS,
        "content_max_bytes": CONTENT_MAX_BYTES,
        "project_max_skills": PROJECT_MAX_SKILLS,
    }


def skill_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A skill as the API shows it: no content, its size; ``qualified_name`` is how agents see it."""
    from .library import PROJECT_SKILLS_PLUGIN

    return {
        "skill_id": row["id"],
        "name": row["name"],
        "qualified_name": f"{PROJECT_SKILLS_PLUGIN}:{row['name']}",
        "description": row["description"],
        "size_bytes": int(row["size_bytes"]),
        "created_at": row["imported_at"],
        "updated_at": row["updated_at"],
    }


def _fields(data: Any, *, adding: bool) -> dict[str, Any]:
    """The validated fields of a request; adding needs name, description and content."""
    if not isinstance(data, Mapping):
        raise ProjectSkillError("body must be a JSON object")
    allowed = _FIELDS if adding else _CHANGE_FIELDS
    if not adding and "enabled" in data:
        raise ProjectSkillError(
            "a skill is switched on or off where it is installed: PUT /api/installs/<install_id> with enabled"
        )
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProjectSkillError(f"unknown field {unknown[0]!r}; the fields are {', '.join(sorted(allowed))}")
    if not adding and not data:
        raise ProjectSkillError(f"give at least one of {', '.join(sorted(allowed))}")
    fields: dict[str, Any] = {}
    if adding or "name" in data:
        fields["name"] = skill_name(data.get("name"))
    if adding or "description" in data:
        fields["description"] = skill_description(data.get("description"))
    if adding or "content" in data:
        fields["content"] = skill_content(data.get("content"))
    if "enabled" in data:
        fields["enabled"] = _enabled(data["enabled"])
    return fields


def _name_taken(name: str) -> ProjectSkillConflict:
    return ProjectSkillConflict(f"the project already has a skill named {name!r}")


def _owned(row: Mapping[str, Any] | None, project_id: str, item_id: str) -> Mapping[str, Any]:
    if row is None or row["origin"] != "project" or row["owner_project_id"] != project_id:
        raise ProjectSkillNotFound(f"project {project_id!r} has no skill {item_id!r}")
    return row


async def _answer(project_id: str, **extra: Any) -> dict[str, Any]:
    from .catalog import project_view

    return {**extra, **await project_view(project_id)}


async def add_skill(project_id: str, data: Any) -> dict[str, Any]:
    """Write a skill (``name``, ``description``, ``content``, optionally ``enabled``) and install it in the project.

    The install names the Planner, Workers and Reviewers and is ``enabled`` as
    given (default on). Returns the skill and the project's view
    (``catalog.project_view``).
    """
    import asyncpg

    from ..db import library as db_library
    from ..db.project_documents import locked_project
    from .catalog import TASK_ROLES, name_conflict

    fields = _fields(data, adding=True)
    file, sha256 = skill_md(fields["name"], fields["description"], fields["content"])
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        owned = await db_library.list_owned_items(project_id, pool=conn)
        if any(row["name"] == fields["name"] for row in owned):
            raise _name_taken(fields["name"])
        if len(owned) >= PROJECT_MAX_SKILLS:
            raise ProjectSkillError(f"a project has at most {skills_label(PROJECT_MAX_SKILLS)}")
        if reason := await name_conflict(conn, project_id, None, fields["name"]):
            raise ProjectSkillConflict(reason)
        try:
            row = await db_library.insert_owned_skill(
                conn, item_id=f"skill-{uuid.uuid4().hex[:12]}", project_id=project_id, name=fields["name"],
                description=fields["description"], file=file, sha256=sha256, roles=TASK_ROLES,
                enabled=fields.get("enabled", True),
            )
        except asyncpg.UniqueViolationError:  # pragma: no cover - the project's row lock orders adds
            raise _name_taken(fields["name"]) from None
    return await _answer(project_id, skill=skill_view(row))


async def get_skill(project_id: str, skill_id: str) -> dict[str, Any]:
    """One skill the project wrote, with its ``content``, the ``SKILL.md`` body."""
    from ..db import library as db_library

    row = _owned(await db_library.get_item(skill_id), project_id, skill_id)
    text = await db_library.get_file(skill_id, SKILL_FILE) or ""
    return {"skill": skill_view(row), "content": skill_body(text, row["name"], row["description"])}


async def update_skill(project_id: str, skill_id: str, data: Any) -> dict[str, Any]:
    """Change a skill's name, description or content; returns it and the project's view.

    A new name must be free in the project and in every project the skill is installed in.
    """
    import asyncpg

    from ..db import library as db_library
    from ..db.project_documents import locked_project
    from .catalog import name_conflict

    fields = _fields(data, adding=False)
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row = _owned(await db_library.get_item(skill_id, pool=conn), project_id, skill_id)
        name = fields.get("name", row["name"])
        description = fields.get("description", row["description"])
        content = fields.get("content")
        if content is None:
            text = await db_library.get_file(skill_id, SKILL_FILE, pool=conn) or ""
            content = skill_body(text, row["name"], row["description"])
        if name != row["name"]:
            owned = await db_library.list_owned_items(project_id, pool=conn)
            if any(other["name"] == name and other["id"] != skill_id for other in owned):
                raise _name_taken(name)
            places = {install["project_id"] for install in await db_library.list_installs(item_ids=[skill_id], pool=conn)
                      if install["project_id"]}
            for place in sorted(places - {project_id}):
                if reason := await name_conflict(conn, place, skill_id, name):
                    raise ProjectSkillConflict(f"the skill is installed there: {reason}")
        file, sha256 = skill_md(name, description, content)
        try:
            row = await db_library.update_owned_skill(conn, skill_id, name=name, description=description, file=file,
                                                      sha256=sha256)
        except asyncpg.UniqueViolationError:  # pragma: no cover - the project's row lock orders changes
            raise _name_taken(name) from None
    return await _answer(project_id, skill=skill_view(row))


async def delete_skill(project_id: str, skill_id: str) -> dict[str, Any]:
    """Remove a skill the project wrote from the catalog and from every project it is installed in.

    Returns the project's view. Its materialized copy stays (a running agent may be using it).
    """
    from ..db import library as db_library
    from ..db.project_documents import locked_project

    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        _owned(await db_library.get_item(skill_id, pool=conn), project_id, skill_id)
        await db_library.delete_item(skill_id, pool=conn)
    return await _answer(project_id, deleted=skill_id)
