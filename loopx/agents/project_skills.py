"""A project's own Claude Code skills: loaded by its task agents only, never written into its repository.

A project can carry skills (RFC section 6, M4). Each has a name (a lowercase
slug, unique in the project), a one-line description, the body of its
``SKILL.md`` and an enabled switch. They live in ``project_skills``.

Every Planner turn and every Worker and Reviewer run reads its project's
enabled skills as it starts (``load_project_skills``), so a change reaches the
next run. They are written as one Claude Code plugin, ``project``, under the
runtime root:

    <runtime_root>/.local/projects/<project_id>/skills/<sha256>/project/
        .claude-plugin/plugin.json
        skills/<name>/SKILL.md    frontmatter (name, description) from the fields, then the body

``<sha256>`` names the set of skills (``AgentProjectSkills.sha256``), so the
directory is written once, read-only, through the library's ``write_once``,
and a change makes a new one. The run mounts it with one ``--plugin-dir``
(``claude_cli.project_skill_args``, through ``agent_settings.Launch``) beside
the library's plugins, and Claude Code names each skill
``project:<name>``. Claude Code lists the skills to the model itself, so the
system prompt does not repeat them. The Manager spans every project and gets
none.

Caps, each refused with a message that states it: a name at most
``NAME_MAX_CHARS`` (Claude Code's limit for a skill name), a description at
most ``DESCRIPTION_MAX_CHARS`` (the limit Claude Code's skill format sets; the
longest of 104 real personal skills has 975), a body at most
``CONTENT_MAX_BYTES`` (it enters the context whenever the skill is used; the
largest of those 104 is 61 KB), and at most ``PROJECT_MAX_SKILLS`` skills in a
project (every enabled skill's name and description is listed in every run's
context). Changes to one project's skills hold its row lock, so two of them
cannot both pass a cap or take one name.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .knowledge import knowledge_dir
from .library import write_once

logger = logging.getLogger(__name__)

NAME_MAX_CHARS = 64
DESCRIPTION_MAX_CHARS = 1024
CONTENT_MAX_BYTES = 64 * 1024
PROJECT_MAX_SKILLS = 20

# The plugin a project's skills are loaded from: Claude Code names them project:<name>.
PLUGIN_NAME = "project"
_PLUGIN_MANIFEST = json.dumps({
    "name": PLUGIN_NAME,
    "description": "This project's own skills, managed by LoopX.",
}, indent=2) + "\n"
# Names the directory layout in every set's hash; change it whenever the files written change.
_LAYOUT_ID = "project-skills/v1"

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIELDS = frozenset({"name", "description", "content", "enabled"})


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


# --- What one agent run gets ---


def skill_file(name: str, description: str, content: str) -> str:
    """The ``SKILL.md`` a skill is written as: frontmatter from its fields, then its body.

    The description is a double-quoted string (JSON, which YAML reads the
    same), so a colon, a quote or a ``#`` in it stays text.
    """
    body = content if content.endswith("\n") else content + "\n"
    return (f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
            f"{body}")


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    sha256: str  # of its SKILL.md as written


def skills_sha256(skills: Sequence[LoadedSkill]) -> str | None:
    """One hash of a set of skills (and the layout they are written in); None for none."""
    if not skills:
        return None
    listing = sorted([skill.name, skill.sha256] for skill in skills)
    return _sha256(json.dumps([_LAYOUT_ID, listing], separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class AgentProjectSkills:
    """What one task agent run loads of its project's skills: the skills and the plugin directory holding them."""

    project_id: str | None = None
    skills: tuple[LoadedSkill, ...] = ()
    plugin_dir: str | None = None  # one --plugin-dir, absolute
    error: str | None = None  # why the run goes without its project's skills

    @property
    def sha256(self) -> str | None:
        """The set's hash, which also names its directory."""
        return skills_sha256(self.skills)

    def record(self) -> dict[str, Any] | None:
        """Its entry in a run's metadata (``project_skills``); None when nothing was to be loaded."""
        if not self.skills and self.error is None:
            return None
        return {
            "project_id": self.project_id,
            "sha256": self.sha256,
            "plugin": PLUGIN_NAME,
            "skills": [{"name": skill.name, "sha256": skill.sha256} for skill in self.skills],
            **({"error": self.error} if self.error is not None else {}),
        }


EMPTY_PROJECT_SKILLS = AgentProjectSkills()


def skills_dir(runtime_root: Path, project_id: str) -> Path:
    """``<runtime_root>/.local/projects/<project_id>/skills``, beside the project's knowledge."""
    return knowledge_dir(runtime_root, project_id).with_name("skills")


def materialize(runtime_root: Path, project_id: str, rows: Sequence[Mapping[str, Any]]) -> AgentProjectSkills:
    """Write the skills in ``rows`` (``name``, ``description``, ``content``) as the project plugin, once.

    Raises ``ProjectSkillError`` for a row the API would not have stored.
    """
    files: dict[str, str] = {}
    for row in rows:
        name = skill_name(row["name"])
        files[name] = skill_file(name, skill_description(row["description"]), skill_content(row["content"]))
    if len(files) != len(rows):
        raise ProjectSkillError("two skills have the same name")
    skills = tuple(LoadedSkill(name, _sha256(text.encode("utf-8"))) for name, text in sorted(files.items()))
    key = skills_sha256(skills)
    assert key is not None

    def build(directory: Path) -> None:
        base = directory / PLUGIN_NAME
        (base / ".claude-plugin").mkdir(parents=True)
        _write_read_only(base / ".claude-plugin" / "plugin.json", _PLUGIN_MANIFEST)
        for name, text in files.items():
            (base / "skills" / name).mkdir(parents=True)
            _write_read_only(base / "skills" / name / "SKILL.md", text)

    target = skills_dir(runtime_root, project_id) / key
    write_once(target, build)
    return AgentProjectSkills(project_id, skills, str(target / PLUGIN_NAME))


def _write_read_only(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o444)


async def load_project_skills(runtime_root: Path, project: Mapping[str, Any] | None) -> AgentProjectSkills:
    """The enabled skills of ``project`` (its row; only ``id`` is read), materialized for one run.

    When the skills cannot be read or written, the run goes without them and
    ``error`` says why.
    """
    from ..db import project_skills as db_skills

    project_id = str((project or {}).get("id") or "")
    if not project_id:
        return EMPTY_PROJECT_SKILLS
    try:
        rows = await db_skills.list_enabled_skills(project_id)
    except Exception:
        logger.warning("cannot read the skills of project %s; the run goes without them", project_id, exc_info=True)
        return AgentProjectSkills(project_id, error="the project's skills could not be read")
    if not rows:
        return EMPTY_PROJECT_SKILLS
    try:
        return await asyncio.to_thread(materialize, runtime_root, project_id, rows)
    except Exception as exc:
        logger.warning("cannot prepare the skills of project %s; the run goes without them", project_id,
                       exc_info=True)
        return AgentProjectSkills(project_id, error=f"the project's skills could not be prepared: {exc}")


# --- The API: list, add, change and delete a project's skills ---


def skill_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A skill as the API shows it: no content, its size; ``qualified_name`` is how agents see it."""
    return {
        "skill_id": row["id"],
        "name": row["name"],
        "qualified_name": f"{PLUGIN_NAME}:{row['name']}",
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "size_bytes": int(row["size_bytes"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _view(project_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "skills": [skill_view(row) for row in rows],
        "count": len(rows),
        "enabled_count": sum(bool(row["enabled"]) for row in rows),
        "plugin": PLUGIN_NAME,
        "caps": {
            "name_max_chars": NAME_MAX_CHARS,
            "description_max_chars": DESCRIPTION_MAX_CHARS,
            "content_max_bytes": CONTENT_MAX_BYTES,
            "project_max_skills": PROJECT_MAX_SKILLS,
        },
    }


async def skills_view(project_id: str) -> dict[str, Any]:
    """The project's skills, their counts and the caps."""
    from ..db import project_skills as db_skills
    from ..db import projects as db_projects

    if await db_projects.get_project(project_id) is None:
        raise ProjectSkillNotFound(f"project {project_id!r} not found")
    return _view(project_id, await db_skills.list_skills(project_id))


def _fields(data: Any, *, adding: bool) -> dict[str, Any]:
    """The validated fields of a request; adding needs name, description and content."""
    if not isinstance(data, Mapping):
        raise ProjectSkillError("body must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise ProjectSkillError(f"unknown field {unknown[0]!r}; the fields are {', '.join(sorted(_FIELDS))}")
    if not adding and not data:
        raise ProjectSkillError(f"give at least one of {', '.join(sorted(_FIELDS))}")
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


async def add_skill(project_id: str, data: Any) -> dict[str, Any]:
    """Add a skill (``name``, ``description``, ``content``, optionally ``enabled``); returns it and the view."""
    import asyncpg

    from ..db import project_skills as db_skills

    fields = _fields(data, adding=True)
    async with db_skills.locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        rows = await db_skills.list_skills(project_id, pool=conn)
        if any(row["name"] == fields["name"] for row in rows):
            raise _name_taken(fields["name"])
        if len(rows) >= PROJECT_MAX_SKILLS:
            raise ProjectSkillError(f"a project has at most {skills_label(PROJECT_MAX_SKILLS)}")
        try:
            row = await db_skills.insert_skill(conn, project_id=project_id, **fields)
        except asyncpg.UniqueViolationError:  # pragma: no cover - the project's row lock orders adds
            raise _name_taken(fields["name"]) from None
    return {"skill": skill_view(row), **await skills_view(project_id)}


async def update_skill(project_id: str, skill_id: str, data: Any) -> dict[str, Any]:
    """Change a skill's name, description, content or enabled switch; returns it and the view."""
    import asyncpg

    from ..db import project_skills as db_skills

    fields = _fields(data, adding=False)
    async with db_skills.locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        rows = await db_skills.list_skills(project_id, pool=conn)
        if not any(row["id"] == skill_id for row in rows):
            raise ProjectSkillNotFound(f"project {project_id!r} has no skill {skill_id!r}")
        if "name" in fields and any(row["name"] == fields["name"] and row["id"] != skill_id for row in rows):
            raise _name_taken(fields["name"])
        try:
            row = await db_skills.update_skill(conn, project_id, skill_id, **fields)
        except asyncpg.UniqueViolationError:  # pragma: no cover - the project's row lock orders changes
            raise _name_taken(fields["name"]) from None
        if row is None:  # pragma: no cover - the row is locked through its project
            raise ProjectSkillNotFound(f"project {project_id!r} has no skill {skill_id!r}")
    return {"skill": skill_view(row), **await skills_view(project_id)}


async def delete_skill(project_id: str, skill_id: str) -> dict[str, Any]:
    """Remove a skill; returns the view. Its materialized copy stays (a running agent may be using it)."""
    from ..db import project_skills as db_skills

    async with db_skills.locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        if not await db_skills.delete_skill(project_id, skill_id, pool=conn):
            raise ProjectSkillNotFound(f"project {project_id!r} has no skill {skill_id!r}")
    return {"deleted": skill_id, **await skills_view(project_id)}
