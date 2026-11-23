"""Skill proposals: an agent drafts a project skill, only the operator's dashboard confirms it.

``propose`` is an internal function (no agent, tool or operation calls it yet). It checks a draft the way a skill
is written today (``project_skills.skill_name``, ``skill_description``, ``skill_content``), refuses a secret
shape in the description, the content or the reason, and stores a ``pending`` row. A proposal changes no
catalog item and reaches no run. ``confirm`` is the one way it becomes a skill version: the operator names the
content hash they saw, the skill is written with author kind ``proposal`` (or ``operator`` for an edit), and
the database ties the ``confirmed`` status to a dashboard confirmation event of that hash.

Typed states: ``Status``, ``Action``, ``Refusal``. A refusal is a value (``ProposalResult``) from ``propose``,
and an exception (``ProposalRefused``) from ``restore`` and ``edit_and_confirm``.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from ..db import skill_proposals as store
from . import skill_secrets
from .project_skills import (
    ProjectSkillConflict, ProjectSkillError, ProjectSkillNotFound, skill_content, skill_description, skill_md,
    skill_name, skill_view,
)

EXPIRY = timedelta(days=7)
WHY_MAX_CHARS = 500
NOTE_MAX_CHARS = 500
EVIDENCE_MAX = 5
LIST_MAX = 100
_ITEM_KEYS = frozenset({"action", "name", "description", "content", "why", "skill", "evidence"})
_EVIDENCE_KEYS = frozenset({"task", "step"})


class Status(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CONFIRMED_EDITED = "confirmed_edited"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"


class Via(StrEnum):
    """Where a proposal comes from. Only ``DASHBOARD`` can confirm; the database allows no other."""

    PLANNER = "planner"
    MANAGER = "manager"


class Refusal(StrEnum):
    SHAPE = "shape"
    NAME = "name"
    DESCRIPTION = "description"
    CONTENT = "content"
    SECRET = "secret"
    LIMIT = "limit"
    WHY = "why"
    EVIDENCE = "evidence"
    OFF = "off"
    UNKNOWN_SKILL = "unknown_skill"
    STALE = "stale"


class ProposalRefused(ProjectSkillError):
    """A draft the checks refuse, with the rule that refused it."""

    def __init__(self, reason: Refusal, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ProposalConflict(ProjectSkillConflict):
    """The proposal is not in a state the request fits (answered with 409)."""


class ProposalForbidden(PermissionError):
    """Only the operator's dashboard session confirms, rejects or restores."""


@dataclass(frozen=True)
class ProposalResult:
    """``proposal`` is the stored view when accepted; ``refusal`` and ``detail`` say why not."""

    proposal: dict[str, Any] | None
    refusal: Refusal | None = None
    detail: str = ""
    superseded: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Checked:
    action: Action
    name: str
    description: str
    content: str
    why: str
    evidence: list[dict[str, str]]
    sha256: str
    item_id: str | None
    base_sha256: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_operator(principal: Any) -> None:
    if getattr(principal, "kind", None) != "operator":
        raise ProposalForbidden("only the operator confirms skill proposals")


# --- Checks (D4) ---


def _field(reason: Refusal, check: Callable[[Any], str], value: Any) -> str:
    try:
        return check(value)
    except ProjectSkillError as exc:
        raise ProposalRefused(reason, str(exc)) from None


def _no_secrets(**fields: str) -> None:
    for field, text in fields.items():
        if found := skill_secrets.find(text):
            rules = ", ".join(sorted({finding.rule for finding in found}))
            raise ProposalRefused(
                Refusal.SECRET,
                f"the {field} has text shaped like a secret ({rules}, at character {found[0].start}); "
                "leave credentials out of a skill",
            )


def _why(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ProposalRefused(Refusal.WHY, "a proposal needs a reason: what repeated and why it holds for later tasks")
    text = raw.strip()
    if len(text) > WHY_MAX_CHARS:
        raise ProposalRefused(Refusal.WHY, f"the reason has at most {WHY_MAX_CHARS} characters, got {len(text)}")
    return text


def _evidence_shape(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not 1 <= len(raw) <= EVIDENCE_MAX:
        raise ProposalRefused(Refusal.EVIDENCE, f"evidence is a list of 1 to {EVIDENCE_MAX} entries, each with a task")
    found: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) - _EVIDENCE_KEYS:
            raise ProposalRefused(Refusal.EVIDENCE, "an evidence entry is an object with a task and optionally a step")
        task, step = entry.get("task"), entry.get("step")
        if not isinstance(task, str) or not task.strip() or (
                step is not None and (not isinstance(step, str) or not step.strip())):
            raise ProposalRefused(Refusal.EVIDENCE, "an evidence entry names its task, and its step by key if given")
        item = {"task": task.strip(), **({"step": step.strip()} if step is not None else {})}
        if item not in found:
            found.append(item)
    return found


async def _evidence_owned(conn: Any, project_id: str, evidence: list[dict[str, str]]) -> None:
    for entry in evidence:
        owner = await conn.fetchval("SELECT project_id FROM tasks WHERE id = $1", entry["task"])
        if owner != project_id:
            raise ProposalRefused(Refusal.EVIDENCE, f"task {entry['task']!r} is not a task of this project")
        if "step" in entry and not await conn.fetchval(
                "SELECT 1 FROM task_steps WHERE task_id = $1 AND step_key = $2", entry["task"], entry["step"]):
            raise ProposalRefused(Refusal.EVIDENCE, f"task {entry['task']!r} has no step {entry['step']!r}")


async def _check(conn: Any, project_id: str, item: Any, evidence: Any) -> _Checked:
    """All the rules on one draft. Reads only; raises ``ProposalRefused``."""
    from ..db import library as db_library
    from .catalog import name_conflict
    from .project_skills import PROJECT_MAX_SKILLS, skills_label

    if not isinstance(item, Mapping) or set(item) - _ITEM_KEYS:
        raise ProposalRefused(Refusal.SHAPE, f"a proposal is an object with the keys {', '.join(sorted(_ITEM_KEYS))}")
    try:
        action = Action(item.get("action"))
    except ValueError:
        raise ProposalRefused(Refusal.SHAPE, "action is create or update") from None
    skill = item.get("skill")
    if action is Action.UPDATE and (not isinstance(skill, str) or not skill.strip()):
        raise ProposalRefused(Refusal.UNKNOWN_SKILL, "an update names the skill it changes")
    if action is Action.CREATE and skill is not None:
        raise ProposalRefused(Refusal.SHAPE, "skill is for an update; a new skill gives its name")
    name = _field(Refusal.NAME, skill_name, item.get("name") if "name" in item or action is Action.CREATE else skill)
    if action is Action.UPDATE and name != skill.strip():
        raise ProposalRefused(Refusal.NAME, "an update keeps the skill's name; propose a new skill to rename")
    description = _field(Refusal.DESCRIPTION, skill_description, item.get("description"))
    content = _field(Refusal.CONTENT, skill_content, item.get("content"))
    why = _why(item.get("why"))
    _no_secrets(description=description, content=content, reason=why)
    shaped = _evidence_shape(item.get("evidence") if evidence is None else evidence)
    await _evidence_owned(conn, project_id, shaped)

    owned = await db_library.list_owned_items(project_id, pool=conn)
    _, sha256 = skill_md(name, description, content)
    if action is Action.CREATE:
        if any(row["name"] == name for row in owned):
            raise ProposalRefused(Refusal.NAME, f"the project already has a skill named {name!r}; propose an update")
        if len(owned) >= PROJECT_MAX_SKILLS:
            raise ProposalRefused(
                Refusal.LIMIT, f"a project has at most {skills_label(PROJECT_MAX_SKILLS)}; only an update fits")
        if reason := await name_conflict(conn, project_id, None, name):
            raise ProposalRefused(Refusal.NAME, reason)
        return _Checked(action, name, description, content, why, shaped, sha256, None, None)
    current = next((row for row in owned if row["name"] == name), None)
    if current is None:
        raise ProposalRefused(Refusal.UNKNOWN_SKILL, f"the project has no skill named {name!r}")
    if current["sha256"] == sha256:
        raise ProposalRefused(Refusal.CONTENT, "the proposal is identical to the skill's current version")
    return _Checked(action, name, description, content, why, shaped, sha256, current["id"], current["sha256"])


# --- Writes ---


async def _note(conn: Any, row: Mapping[str, Any], text: str) -> None:
    """One activity line on each task the proposal cites."""
    from ..db import activity

    tasks = sorted({entry["task"] for entry in row["evidence"]})
    for task_id in await conn.fetchval("SELECT COALESCE(array_agg(id), '{}') FROM tasks WHERE id = ANY($1)", tasks):
        steps = sorted({e["step"] for e in row["evidence"] if e["task"] == task_id and "step" in e})
        origin = f" from step {', '.join(steps)}" if steps else ""
        await activity.insert_activity(
            task_id=task_id, kind="phase", summary=f"Skill proposal #{row['id']}{origin} {text}"[:500], pool=conn)


async def _supersede(conn: Any, row: Mapping[str, Any], *, at: datetime, note: str) -> None:
    await store.set_status(conn, row["id"], Status.SUPERSEDED, at=at, note=note)
    await store.add_event(conn, row["id"], action="supersede", actor_kind="system", via="system",
                          content_sha256=row["content_sha256"], note=note)


async def propose(
    project_id: str, item: Any, *, proposer: str, evidence: Any = None, via: Via = Via.PLANNER,
    now: datetime | None = None,
) -> ProposalResult:
    """Check a draft and store it as pending; an older pending proposal of the same name becomes ``superseded``.

    ``item`` is ``{action, name, description, content, why}`` (an update gives ``skill`` too); ``evidence`` is a
    list of ``{task, step?}`` and defaults to ``item['evidence']``. A refusal is a value, not an exception. An
    unknown project raises ``ProjectSkillNotFound``.
    """
    from ..db.project_documents import locked_project

    now = now or _now()
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        try:
            if await store.project_mode(conn, project_id) != "on":
                raise ProposalRefused(Refusal.OFF, "skill proposals are off for this project")
            checked = await _check(conn, project_id, item, evidence)
        except ProposalRefused as exc:
            return ProposalResult(None, exc.reason, exc.detail)
        superseded: list[int] = []
        if older := await store.pending_named(conn, project_id, checked.name):
            await _supersede(conn, older, at=now, note="a newer proposal for this name replaced it")
            superseded.append(older["id"])
        row = await store.insert(
            conn, project_id=project_id, action=checked.action, item_id=checked.item_id,
            base_sha256=checked.base_sha256, name=checked.name, description=checked.description,
            content=checked.content, content_sha256=checked.sha256, why=checked.why, evidence=checked.evidence,
            proposer=proposer, expires_at=now + EXPIRY,
        )
        await store.add_event(conn, row["id"], action="propose", actor_kind="agent", via=Via(via).value,
                              content_sha256=checked.sha256)
        await _note(conn, row, f"for {checked.name!r} was created")
    return ProposalResult(await view(row), superseded=tuple(superseded))


async def _locked(conn: Any, project_id: str, proposal_id: int) -> dict[str, Any]:
    row = await store.get(conn, proposal_id, lock=True)
    if row is None or row["project_id"] != project_id:
        raise ProjectSkillNotFound(f"project {project_id!r} has no skill proposal {proposal_id}")
    return row


async def _stale(conn: Any, project_id: str, row: Mapping[str, Any]) -> str | None:
    """Why an update proposal can no longer apply: the skill is gone, or is no longer the version it was drafted on."""
    if row["action"] != Action.UPDATE:
        return None
    from ..db import library as db_library

    item = await db_library.get_item(row["item_id"], pool=conn)
    if item is None or item["origin"] != "project" or item["owner_project_id"] != project_id:
        return "the skill no longer exists"
    if item["sha256"] != row["base_sha256"]:
        return "the skill changed after this was drafted"
    return None


async def _decide_pending(
    conn: Any, project_id: str, proposal_id: int, content_sha256: str, *, at: datetime,
) -> tuple[dict[str, Any], str | None]:
    """The locked pending proposal whose hash is the displayed one, and why it went stale (then it is superseded)."""
    row = await _locked(conn, project_id, proposal_id)
    if row["status"] != Status.PENDING:
        raise ProposalConflict(f"the proposal is {row['status']}, not pending")
    if row["content_sha256"] != content_sha256:
        raise ProposalConflict("the proposal changed after it was displayed; reload it and read it again")
    if reason := await _stale(conn, project_id, row):
        await _supersede(conn, row, at=at, note=reason)
        await _note(conn, row, f"for {row['name']!r} was superseded: {reason}")
    return row, reason


def _superseded_conflict(reason: str) -> ProposalConflict:
    return ProposalConflict(f"{reason}: the proposal is superseded; draft it again from the current version")


async def _write_skill(
    conn: Any, project_id: str, row: Mapping[str, Any], *, kind: Any, ref: str | None,
) -> Mapping[str, Any]:
    from .project_skills import _change_skill, _insert_skill

    fields = {"name": row["name"], "description": row["description"], "content": row["content"]}
    if row["action"] == Action.CREATE:
        return await _insert_skill(conn, project_id, fields, author=kind, author_ref=ref)
    return await _change_skill(conn, project_id, row["item_id"], fields, author=kind, author_ref=ref)


async def _confirmed(
    conn: Any, project_id: str, row: Mapping[str, Any], *, status: Status, at: datetime, kind: Any, ref: str | None,
    sha256: str,
) -> dict[str, Any]:
    from ..db import skill_versions as version_store

    item = await _write_skill(conn, project_id, row, kind=kind, ref=ref)
    event_id = await store.add_event(
        conn, row["id"], action="confirm", actor_kind="user", via="dashboard", content_sha256=sha256)
    await store.set_confirmed(conn, row["id"], status, event_id=event_id, content_sha256=sha256, at=at)
    await _note(conn, row, f"for {row['name']!r} was confirmed")
    return {
        "skill": skill_view(item), "version": await version_store.pointer(conn, item["id"]),
        "affected_projects": await version_store.affected_projects(conn, item["id"]),
    }


async def confirm(
    project_id: str, proposal_id: int, content_sha256: str, *, principal: Any, now: datetime | None = None,
) -> dict[str, Any]:
    """Write the proposal as a skill version, if ``content_sha256`` is the hash the operator saw.

    A different hash is a 409. An update whose skill changed since the draft is marked ``superseded``,
    writes nothing, and answers 409 too.
    """
    from ..db.project_documents import locked_project
    from .skill_versions import AuthorKind

    _require_operator(principal)
    now = now or _now()
    stale: str | None = None
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row, stale = await _decide_pending(conn, project_id, proposal_id, content_sha256, at=now)
        if stale is None:
            result = await _confirmed(
                conn, project_id, row, status=Status.CONFIRMED, at=now, kind=AuthorKind.PROPOSAL,
                ref=str(proposal_id), sha256=row["content_sha256"])
            result["proposal"] = await view(await store.get(conn, proposal_id) or row, conn=conn)
    if stale is not None:
        raise _superseded_conflict(stale)
    return result


async def edit_and_confirm(
    project_id: str, proposal_id: int, content_sha256: str, fields: Any, *, principal: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The operator changes the proposal's name (a new skill only), description or content, then confirms it.

    The result is the operator's own write: an ``edit`` and a ``confirm`` event of the new hash, a version
    of author kind ``operator``, status ``confirmed_edited``.
    """
    import asyncpg

    from ..db.project_documents import locked_project
    from .skill_versions import AuthorKind

    _require_operator(principal)
    now = now or _now()
    if not isinstance(fields, Mapping) or not fields or set(fields) - {"name", "description", "content"}:
        raise ProjectSkillError("give at least one of name, description and content")
    stale: str | None = None
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row, stale = await _decide_pending(conn, project_id, proposal_id, content_sha256, at=now)
        if stale is None:
            if "name" in fields and row["action"] == Action.UPDATE and fields["name"] != row["name"]:
                raise ProposalRefused(Refusal.NAME, "an update keeps the skill's name")
            name = _field(Refusal.NAME, skill_name, fields.get("name", row["name"]))
            description = _field(Refusal.DESCRIPTION, skill_description, fields.get("description", row["description"]))
            content = _field(Refusal.CONTENT, skill_content, fields.get("content", row["content"]))
            _no_secrets(description=description, content=content)
            _, new_sha = skill_md(name, description, content)
            if row["action"] == Action.UPDATE and new_sha == row["base_sha256"]:
                raise ProposalRefused(Refusal.CONTENT, "the edit is identical to the skill's current version")
            try:
                await store.set_content(
                    conn, proposal_id, name=name, description=description, content=content, content_sha256=new_sha)
            except asyncpg.UniqueViolationError:
                raise ProposalConflict(f"another proposal for {name!r} is pending") from None
            await store.add_event(conn, proposal_id, action="edit", actor_kind="user", via="dashboard",
                                  content_sha256=new_sha)
            row = {**row, "name": name, "description": description, "content": content, "content_sha256": new_sha}
            result = await _confirmed(
                conn, project_id, row, status=Status.CONFIRMED_EDITED, at=now, kind=AuthorKind.OPERATOR, ref=None,
                sha256=new_sha)
            result["proposal"] = await view(await store.get(conn, proposal_id) or row, conn=conn)
    if stale is not None:
        raise _superseded_conflict(stale)
    return result


async def reject(
    project_id: str, proposal_id: int, reason: str = "", *, principal: Any, now: datetime | None = None,
) -> dict[str, Any]:
    """Reject a pending proposal; the reason is kept (at most ``NOTE_MAX_CHARS``)."""
    from ..db.project_documents import locked_project

    _require_operator(principal)
    if not isinstance(reason, str) or len(reason.strip()) > NOTE_MAX_CHARS:
        raise ProjectSkillError(f"a reason is text of at most {NOTE_MAX_CHARS} characters")
    now = now or _now()
    note = reason.strip() or None
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row = await _locked(conn, project_id, proposal_id)
        if row["status"] != Status.PENDING:
            raise ProposalConflict(f"the proposal is {row['status']}, not pending")
        await store.set_status(conn, proposal_id, Status.REJECTED, at=now, note=note)
        await store.add_event(conn, proposal_id, action="reject", actor_kind="user", via="dashboard",
                              content_sha256=row["content_sha256"], note=note)
        await _note(conn, row, f"for {row['name']!r} was rejected")
        return {"proposal": await view(await store.get(conn, proposal_id) or row, conn=conn)}


async def restore(
    project_id: str, proposal_id: int, *, principal: Any, now: datetime | None = None,
) -> dict[str, Any]:
    """Make an expired proposal pending again, after every rule is checked against today's state.

    The skill an update was drafted on must still be the same version, and no newer proposal of the name may
    be pending; otherwise ``ProposalRefused`` (``stale``, or the rule the draft no longer passes).
    """
    from ..db.project_documents import locked_project

    _require_operator(principal)
    now = now or _now()
    async with locked_project(project_id) as (conn, project):
        if project is None:
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        row = await _locked(conn, project_id, proposal_id)
        if row["status"] != Status.EXPIRED:
            raise ProposalConflict(f"the proposal is {row['status']}, not expired")
        if await store.project_mode(conn, project_id) != "on":
            raise ProposalRefused(Refusal.OFF, "skill proposals are off for this project")
        checked = await _check(
            conn, project_id,
            {"action": row["action"], "name": row["name"], "description": row["description"],
             "content": row["content"], "why": row["why"], **({"skill": row["name"]} if row["action"] == "update" else {})},
            row["evidence"])
        if checked.sha256 != row["content_sha256"] or checked.base_sha256 != row["base_sha256"]:
            raise ProposalRefused(Refusal.STALE, "the skill changed after this was drafted; draft it again")
        if await store.pending_named(conn, project_id, row["name"]):
            raise ProposalRefused(Refusal.STALE, "a newer proposal for this name is pending")
        await store.restore_pending(conn, proposal_id, expires_at=now + EXPIRY)
        await store.add_event(conn, proposal_id, action="restore", actor_kind="user", via="dashboard",
                              content_sha256=row["content_sha256"])
        return {"proposal": await view(await store.get(conn, proposal_id) or row, conn=conn)}


async def expire_due(conn: Any, now: datetime) -> int:
    """Expire the pending proposals past their seven days. Housekeeping runs this; restoring is the operator's."""
    rows = await store.expire_before(conn, now)
    for row in rows:
        await store.add_event(conn, row["id"], action="expire", actor_kind="system", via="system",
                              content_sha256=row["content_sha256"])
    return len(rows)


# --- Reads ---


def _unified(before: str, after: str, label: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=f"current {label}",
        tofile=f"proposed {label}"))


async def view(row: Mapping[str, Any], *, conn: Any = None) -> dict[str, Any]:
    """A proposal as the API shows it. An update carries the difference from the skill's current version."""
    from ..db import library as db_library
    from ..db.connection import get_pool
    from ..db.skill_versions import affected_projects
    from .project_skills import SKILL_FILE, skill_body

    conn = conn or get_pool()
    shown: dict[str, Any] = {
        "proposal_id": int(row["id"]), "project_id": row["project_id"], "action": row["action"],
        "skill_id": row["item_id"], "name": row["name"], "description": row["description"], "why": row["why"],
        "evidence": list(row["evidence"]), "proposer": row["proposer"], "status": row["status"],
        "content_sha256": row["content_sha256"], "base_sha256": row["base_sha256"], "content": row["content"],
        "created_at": row["created_at"], "expires_at": row["expires_at"], "decided_at": row["decided_at"],
        "decision_note": row["decision_note"], "current": None, "diff": None, "stale": False,
        "affected_projects": [],
    }
    if row["action"] != Action.UPDATE:
        return shown
    item = await db_library.get_item(row["item_id"], pool=conn)
    if item is None or item["origin"] != "project" or item["owner_project_id"] != row["project_id"]:
        return {**shown, "stale": True}
    text = await db_library.get_file(item["id"], SKILL_FILE, pool=conn) or ""
    body = skill_body(text, item["name"], item["description"])
    return {
        **shown, "stale": item["sha256"] != row["base_sha256"],
        "current": {"name": item["name"], "description": item["description"], "sha256": item["sha256"]},
        "diff": {
            "name": {"from": item["name"], "to": row["name"]},
            "description": {"from": item["description"], "to": row["description"]},
            "content": _unified(body, row["content"], "content"),
        },
        "affected_projects": await affected_projects(conn, item["id"]),
    }


DEFAULT_LIST = (Status.PENDING, Status.EXPIRED)


async def list_view(project_id: str, *, status: Status | None = None, limit: int = 50) -> dict[str, Any]:
    """The project's switch and its proposals, newest first: pending and expired ones unless a ``status`` is given."""
    from ..db.connection import get_pool

    if not 1 <= limit <= LIST_MAX:
        raise ProjectSkillError(f"limit is 1 to {LIST_MAX}")
    async with get_pool().acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM projects WHERE id = $1)", project_id):
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
        mode = await store.project_mode(conn, project_id) or "off"
        wanted = [status.value] if status else [s.value for s in DEFAULT_LIST]
        rows = await store.list_rows(conn, project_id, wanted, limit)
        return {"mode": mode, "proposals": [await view(row, conn=conn) for row in rows]}


async def get_view(project_id: str, proposal_id: int) -> dict[str, Any]:
    from ..db.connection import get_pool

    async with get_pool().acquire() as conn:
        row = await store.get(conn, proposal_id)
        if row is None or row["project_id"] != project_id:
            raise ProjectSkillNotFound(f"project {project_id!r} has no skill proposal {proposal_id}")
        events = await store.events(conn, proposal_id)
        return {"proposal": await view(row, conn=conn), "events": events}


async def set_mode(project_id: str, mode: str) -> dict[str, str]:
    """Switch proposals on or off for a project. ``off`` stores NULL, so an unused project reads as before."""
    from ..db.connection import get_pool

    if mode not in ("off", "on"):
        raise ProjectSkillError("mode is off or on")
    async with get_pool().acquire() as conn:
        if not await store.set_project_mode(conn, project_id, None if mode == "off" else mode):
            raise ProjectSkillNotFound(f"project {project_id!r} not found")
    return {"project_id": project_id, "mode": mode}
