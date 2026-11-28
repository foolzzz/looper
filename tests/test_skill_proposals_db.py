"""Skill proposals against PostgreSQL: the checks, confirmation, the database's own refusals, expiry, the daily note.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each test works in a new schema of its
own, dropped afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from looper.agents import api_v1, project_skills, skill_proposals as sp
from looper.agents.project_skills import skill_md
from looper.db import projects
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPERATOR = api_v1.OPERATOR
CLIENT = api_v1.Principal("client", "client-1", "reader")
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
# The exact text of a project's view and of its config export chunk for a project that never used proposals, as
# the commit this branch started from (dbc14152b) writes them for fixed input.
OFF_BASELINE = json.loads((Path(__file__).parent / "fixtures" / "skill_proposals_off_bytes.json").read_text())
FIXED_ROW = {"id": "proj-fixed", "created_at": "2026-11-20T12:00:00+00:00", "updated_at": "2026-11-20T12:00:00+00:00"}


@pytest.fixture(autouse=True)
def _clock_inside_the_validity_window(monkeypatch):
    # Calls without an explicit now= read the module clock. Pin it one day after T0 so these tests do not
    # depend on the wall clock (proposals made at T0 expire at T0 + EXPIRY, which real time has passed).
    monkeypatch.setattr(sp, "_now", lambda: T0 + timedelta(days=1))


def _item(**over: Any) -> dict[str, Any]:
    item = {"action": "create", "name": "release-check", "description": "Use before cutting a release.",
            "content": "# Release check\n1. Run the tests.\n", "why": "Three tasks re-derived these steps."}
    item.update(over)
    return {key: value for key, value in item.items() if value is not None}


async def _project(pool: Any, *, mode: str | None = "on", name: str = "calc") -> str:
    project = await projects.create_project(name=name, pool=pool)
    if mode:
        await sp.set_mode(project["id"], mode)
    return project["id"]


async def _task(pool: Any, project_id: str, task_id: str = "task-1", step_key: str | None = "s1") -> dict[str, str]:
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ($1, $2, 'r', 'completed')",
                       task_id, project_id)
    if step_key:
        await pool.execute(
            "INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) VALUES ($1, $2, 0, $3, 'x')",
            f"{task_id}-{step_key}", task_id, step_key)
    return {"task": task_id, **({"step": step_key} if step_key else {})}


async def _propose(pool: Any, project_id: str, evidence: Any = None, **over: Any) -> sp.ProposalResult:
    if evidence is None:
        evidence = [await _task(pool, project_id, f"task-{await pool.fetchval('SELECT count(*) FROM tasks') + 1}")]
    return await sp.propose(project_id, _item(**over), proposer=f"planner:{evidence[0]['task']}",
                            evidence=evidence, now=T0)


async def _accepted(pool: Any, project_id: str, evidence: Any = None, **over: Any) -> dict[str, Any]:
    result = await _propose(pool, project_id, evidence, **over)
    assert result.proposal is not None, (result.refusal, result.detail)
    return result.proposal


async def _count(pool: Any, table: str = "skill_proposals") -> int:
    return await pool.fetchval(f"SELECT count(*) FROM {table}")


# --- The checks (RFC D4): a typed refusal for each rule, and nothing stored ---


def test_a_project_without_the_switch_refuses_everything_with_off(monkeypatch):
    async def test(pool):
        for mode in (None, "off"):
            project_id = await _project(pool, mode=mode, name=f"p-{mode}")
            result = await _propose(pool, project_id)
            assert result.proposal is None and result.refusal is sp.Refusal.OFF
        assert await _count(pool) == 0 and await _count(pool, "skill_proposal_events") == 0
        assert await pool.fetchval("SELECT count(*) FROM projects WHERE skill_proposals IS NOT NULL") == 0

    run_db(test, monkeypatch)


_LONG = "x" * 600
_GH = "ghp_" + "a1B2" * 9
CASES = [
    ("shape-key", {"colour": "red"}, sp.Refusal.SHAPE),
    ("shape-action", {"action": "delete"}, sp.Refusal.SHAPE),
    ("name-slug", {"name": "Release Check"}, sp.Refusal.NAME),
    ("name-exists", {"name": "existing"}, sp.Refusal.NAME),
    ("description-lines", {"description": "one\ntwo"}, sp.Refusal.DESCRIPTION),
    ("content-frontmatter", {"content": "---\nname: x\n---\nbody"}, sp.Refusal.CONTENT),
    ("content-empty", {"content": "  "}, sp.Refusal.CONTENT),
    ("secret-description", {"description": f"Use with token = {_GH}"}, sp.Refusal.SECRET),
    ("secret-content", {"content": f"Run with key {_GH}\n"}, sp.Refusal.SECRET),
    ("secret-why", {"why": "AKIAABCDEFGHIJKLMNOP leaked"}, sp.Refusal.SECRET),
    ("why-empty", {"why": " "}, sp.Refusal.WHY),
    ("why-long", {"why": _LONG}, sp.Refusal.WHY),
    ("update-without-skill", {"action": "update"}, sp.Refusal.UNKNOWN_SKILL),
    ("update-unknown", {"action": "update", "skill": "nothing", "name": "nothing"}, sp.Refusal.UNKNOWN_SKILL),
    ("update-rename", {"action": "update", "skill": "existing", "name": "other"}, sp.Refusal.NAME),
    ("update-identical", {"action": "update", "skill": "existing", "name": "existing",
                          "description": "Use for existing.", "content": "Reply existing.\n"}, sp.Refusal.CONTENT),
    ("create-with-skill", {"skill": "existing"}, sp.Refusal.SHAPE),
]


@pytest.mark.parametrize("over,reason", [(over, reason) for _, over, reason in CASES], ids=[c[0] for c in CASES])
def test_each_rule_refuses_with_its_typed_reason_and_stores_nothing(monkeypatch, over, reason):
    async def test(pool):
        project_id = await _project(pool)
        await project_skills.add_skill(project_id, {"name": "existing", "description": "Use for existing.",
                                                    "content": "Reply existing.\n"})
        before = await pool.fetch("SELECT id, sha256, skill_version FROM library_items ORDER BY id")
        result = await _propose(pool, project_id, **over)
        assert result.proposal is None and result.refusal is reason, (result.refusal, result.detail)
        assert result.detail
        if reason is sp.Refusal.SECRET:
            assert _GH not in result.detail and "AKIA" not in result.detail
        assert await _count(pool) == 0 and await _count(pool, "skill_proposal_events") == 0
        assert await pool.fetch("SELECT id, sha256, skill_version FROM library_items ORDER BY id") == before

    run_db(test, monkeypatch)


def test_evidence_must_name_this_projects_tasks_and_steps(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        other = await _project(pool, name="other")
        mine = await _task(pool, project_id, "task-mine")
        theirs = await _task(pool, other, "task-theirs")
        for evidence in ([], "task-mine", [{"task": "missing"}], [theirs], [{"task": "task-mine", "step": "s9"}],
                         [{"task": "task-mine", "colour": "red"}], [mine] * 6 + [theirs]):
            result = await sp.propose(project_id, _item(), proposer="p", evidence=evidence, now=T0)
            assert result.refusal is sp.Refusal.EVIDENCE, (evidence, result)
        assert await _count(pool) == 0
        accepted = await sp.propose(project_id, _item(), proposer="p", evidence=[mine, mine], now=T0)
        assert accepted.proposal["evidence"] == [mine]

    run_db(test, monkeypatch)


@pytest.mark.parametrize("field", ["task", "step"])
def test_evidence_shape_refuses_identifiers_over_64_characters(field):
    valid = {"task": "task-one", field: "a" * 64}
    assert sp._evidence_shape([valid]) == [valid]
    with pytest.raises(sp.ProposalRefused) as error:
        sp._evidence_shape([{"task": "task-one", field: "a" * 65}])
    assert error.value.reason is sp.Refusal.EVIDENCE


def test_twenty_skills_refuse_a_create_and_still_allow_an_update(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        for number in range(project_skills.PROJECT_MAX_SKILLS):
            await project_skills.add_skill(project_id, {"name": f"skill-{number}", "description": "d", "content": "c\n"})
        created = await _propose(pool, project_id)
        assert created.refusal is sp.Refusal.LIMIT and created.proposal is None
        updated = await _propose(pool, project_id, action="update", skill="skill-3", name="skill-3",
                                 content="Changed.\n")
        assert updated.proposal is not None and updated.proposal["action"] == "update"

    run_db(test, monkeypatch)


def test_a_valid_proposal_is_pending_hashed_and_touches_no_skill(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        task = await _task(pool, project_id)
        accepted = await sp.propose(project_id, _item(), proposer="planner:task-1", evidence=[task], now=T0)
        proposal = accepted.proposal
        _, sha = skill_md("release-check", "Use before cutting a release.", "# Release check\n1. Run the tests.\n")
        assert proposal["status"] == "pending" and proposal["content_sha256"] == sha
        assert proposal["base_sha256"] is None and proposal["skill_id"] is None
        assert proposal["expires_at"] == T0 + sp.EXPIRY and proposal["proposer"] == "planner:task-1"
        assert await pool.fetchval("SELECT count(*) FROM library_items") == 0
        assert await pool.fetchval("SELECT count(*) FROM skill_versions") == 0
        events = await pool.fetch("SELECT action, actor_kind, via, content_sha256 FROM skill_proposal_events")
        assert [dict(e) for e in events] == [
            {"action": "propose", "actor_kind": "agent", "via": "planner", "content_sha256": sha}]
        line = await pool.fetchval("SELECT summary FROM activity WHERE task_id = 'task-1'")
        assert "release-check" in line and "step s1" in line and "created" in line

    run_db(test, monkeypatch)


def test_an_update_records_the_skills_current_hash_and_a_newer_proposal_supersedes_the_older(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        added = await project_skills.add_skill(project_id, {
            "name": "release-check", "description": "Use for releases.", "content": "Old.\n"})
        item = await pool.fetchrow("SELECT id, sha256 FROM library_items")
        first = await _accepted(pool, project_id, action="update", skill="release-check", content="One.\n")
        assert first["base_sha256"] == item["sha256"] and first["skill_id"] == item["id"]
        second = await _propose(pool, project_id, action="update", skill="release-check", content="Two.\n")
        assert second.superseded == (first["proposal_id"],)
        statuses = {r["id"]: r["status"] for r in await pool.fetch("SELECT id, status FROM skill_proposals")}
        assert statuses == {first["proposal_id"]: "superseded", second.proposal["proposal_id"]: "pending"}
        assert [e["action"] for e in await pool.fetch(
            "SELECT action FROM skill_proposal_events WHERE proposal_id = $1 ORDER BY id", first["proposal_id"])
        ] == ["propose", "supersede"]
        assert added["skill"]["name"] == "release-check"

    run_db(test, monkeypatch)


# --- Confirmation (RFC D2, D5, section 8) ---


async def _version_rows(pool: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in await pool.fetch(
        "SELECT item_id, version, author_kind, author_ref, sha256 FROM skill_versions ORDER BY id")]


def test_confirm_writes_one_proposal_version_bound_to_the_hash(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        with pytest.raises(sp.ProposalConflict):
            await sp.confirm(project_id, proposal["proposal_id"], "0" * 64, principal=OPERATOR)
        assert await _count(pool, "library_items") == 0
        assert await pool.fetchval("SELECT status FROM skill_proposals") == "pending"

        done = await sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"],
                                principal=OPERATOR, now=T0)
        assert done["proposal"]["status"] == "confirmed" and done["version"] == 1
        item = await pool.fetchrow("SELECT id, name, sha256, skill_version FROM library_items")
        assert item["name"] == "release-check" and item["sha256"] == proposal["content_sha256"]
        assert await _version_rows(pool) == [{
            "item_id": item["id"], "version": 1, "author_kind": "proposal",
            "author_ref": str(proposal["proposal_id"]), "sha256": proposal["content_sha256"]}]
        row = await pool.fetchrow("SELECT * FROM skill_proposals")
        event = await pool.fetchrow("SELECT * FROM skill_proposal_events WHERE id = $1", row["confirmed_event_id"])
        assert (row["confirmed_actor"], row["confirmed_sha256"]) == ("user", proposal["content_sha256"])
        assert (event["action"], event["actor_kind"], event["via"], event["content_sha256"]) == (
            "confirm", "user", "dashboard", proposal["content_sha256"])
        with pytest.raises(sp.ProposalConflict):  # no second confirmation
            await sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"], principal=OPERATOR)
        assert len(await _version_rows(pool)) == 1

    run_db(test, monkeypatch)


def test_confirming_an_update_after_the_skill_changed_supersedes_and_writes_nothing(monkeypatch):
    from looper.agents import skill_versions

    async def test(pool):
        project_id = await _project(pool)
        added = await project_skills.add_skill(project_id, {
            "name": "release-check", "description": "Use for releases.", "content": "Old.\n"})
        skill_id = added["skill"]["skill_id"]
        for edit in ("edit", "rollback"):
            proposal = await _accepted(pool, project_id, action="update", skill="release-check", content=f"{edit}!\n")
            if edit == "edit":
                await project_skills.update_skill(project_id, skill_id, {"content": "Someone else.\n"})
            else:
                await skill_versions.rollback(project_id, skill_id, 1)
            before = (await _version_rows(pool), await pool.fetchval("SELECT sha256 FROM library_items"))
            with pytest.raises(sp.ProposalConflict, match="superseded"):
                await sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"], principal=OPERATOR)
            assert (await _version_rows(pool), await pool.fetchval("SELECT sha256 FROM library_items")) == before
            row = await pool.fetchrow("SELECT status, confirmed_event_id FROM skill_proposals WHERE id = $1",
                                      proposal["proposal_id"])
            assert (row["status"], row["confirmed_event_id"]) == ("superseded", None)
        assert [r["author_kind"] for r in await _version_rows(pool)] == ["operator", "operator", "rollback"]

    run_db(test, monkeypatch)


def test_confirming_an_update_writes_a_proposal_version_when_the_base_is_unchanged(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        await project_skills.add_skill(project_id, {
            "name": "release-check", "description": "Use for releases.", "content": "Old.\n"})
        proposal = await _accepted(pool, project_id, action="update", skill="release-check", content="New.\n",
                                   description="Use for every release.")
        assert proposal["stale"] is False and "+New." in proposal["diff"]["content"]
        assert proposal["diff"]["description"] == {"from": "Use for releases.", "to": "Use for every release."}
        done = await sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"], principal=OPERATOR)
        assert done["version"] == 2 and done["skill"]["description"] == "Use for every release."
        assert [(r["version"], r["author_kind"]) for r in await _version_rows(pool)] == [(1, "operator"),
                                                                                       (2, "proposal")]

    run_db(test, monkeypatch)


def test_edit_then_confirm_is_the_operators_own_version(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        with pytest.raises(sp.ProposalRefused) as refused:  # the edited text goes through the same rules
            await sp.edit_and_confirm(project_id, proposal["proposal_id"], proposal["content_sha256"],
                                      {"content": f"key {_GH}\n"}, principal=OPERATOR)
        assert refused.value.reason is sp.Refusal.SECRET
        assert await pool.fetchval("SELECT status FROM skill_proposals") == "pending"
        done = await sp.edit_and_confirm(project_id, proposal["proposal_id"], proposal["content_sha256"],
                                         {"content": "# Release check\nRun the tests twice.\n"}, principal=OPERATOR)
        _, new_sha = skill_md("release-check", "Use before cutting a release.", "# Release check\nRun the tests twice.\n")
        assert done["proposal"]["status"] == "confirmed_edited" and done["proposal"]["content_sha256"] == new_sha
        assert await _version_rows(pool) == [{
            "item_id": done["skill"]["skill_id"], "version": 1, "author_kind": "operator", "author_ref": None,
            "sha256": new_sha}]
        actions = [(e["action"], e["actor_kind"], e["via"], e["content_sha256"]) for e in await pool.fetch(
            "SELECT * FROM skill_proposal_events ORDER BY id")]
        assert actions[1:] == [("edit", "user", "dashboard", new_sha), ("confirm", "user", "dashboard", new_sha)]
        row = await pool.fetchrow("SELECT status, confirmed_sha256, content FROM skill_proposals")
        assert row["status"] == "confirmed_edited" and row["confirmed_sha256"] == new_sha
        assert "twice" in row["content"]

    run_db(test, monkeypatch)


def test_only_the_operator_confirms_rejects_or_restores(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        pid, sha = proposal["proposal_id"], proposal["content_sha256"]
        for principal in (CLIENT, None, SimpleNamespace(kind="manager")):
            for call in (sp.confirm(project_id, pid, sha, principal=principal),
                         sp.edit_and_confirm(project_id, pid, sha, {"content": "x\n"}, principal=principal),
                         sp.reject(project_id, pid, "no", principal=principal),
                         sp.restore(project_id, pid, principal=principal)):
                with pytest.raises(sp.ProposalForbidden):
                    await call
        assert await pool.fetchval("SELECT status FROM skill_proposals") == "pending"
        assert await _count(pool, "skill_proposal_events") == 1 and await _count(pool, "library_items") == 0

    run_db(test, monkeypatch)


def test_reject_keeps_the_reason_and_a_decided_proposal_cannot_be_decided_again(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        with pytest.raises(project_skills.ProjectSkillError):
            await sp.reject(project_id, proposal["proposal_id"], "x" * 501, principal=OPERATOR)
        done = await sp.reject(project_id, proposal["proposal_id"], " too task specific ", principal=OPERATOR, now=T0)
        assert done["proposal"]["status"] == "rejected" and done["proposal"]["decision_note"] == "too task specific"
        for call in (sp.reject(project_id, proposal["proposal_id"], principal=OPERATOR),
                     sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"], principal=OPERATOR)):
            with pytest.raises(sp.ProposalConflict):
                await call
        with pytest.raises(project_skills.ProjectSkillNotFound):
            await sp.reject(await _project(pool, name="other"), proposal["proposal_id"], principal=OPERATOR)
        shown = await sp.get_view(project_id, proposal["proposal_id"])
        assert [e["action"] for e in shown["events"]] == ["propose", "reject"] and shown["events"][1]["note"]

    run_db(test, monkeypatch)


# --- The database refuses a forged confirmation, whoever writes it ---


def test_the_database_refuses_a_confirmation_that_is_not_the_operators_dashboard_event(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        pid, sha = proposal["proposal_id"], proposal["content_sha256"]

        async def event(action, actor, via, content=sha):
            return await pool.fetchval(
                "INSERT INTO skill_proposal_events (proposal_id, action, actor_kind, via, content_sha256) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id", pid, action, actor, via, content)

        for action, actor, via, content in (
            ("confirm", "user", "planner", sha), ("confirm", "user", "manager", sha),
            ("confirm", "user", "system", sha), ("confirm", "agent", "dashboard", sha),
            ("confirm", "system", "dashboard", sha), ("confirm", "user", "dashboard", None),
            ("reject", "agent", "planner", sha), ("edit", "system", "system", sha),
            ("restore", "agent", "manager", sha),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await event(action, actor, via, content)
        with pytest.raises(asyncpg.CheckViolationError):  # via is a closed list: no channel can name itself
            await event("confirm", "user", "lark")

        async def mark(status, event_id, actor="user", confirmed=sha):
            return await pool.execute(
                "UPDATE skill_proposals SET status = $2, confirmed_event_id = $3, confirmed_actor = $4, "
                "confirmed_sha256 = $5 WHERE id = $1", pid, status, event_id, actor, confirmed)

        with pytest.raises(asyncpg.CheckViolationError):  # "confirmed" with no event at all
            await pool.execute("UPDATE skill_proposals SET status = 'confirmed' WHERE id = $1", pid)
        with pytest.raises(asyncpg.CheckViolationError):  # "confirmed_edited" with no event at all
            await pool.execute("UPDATE skill_proposals SET status = 'confirmed_edited' WHERE id = $1", pid)
        planner_event = await event("propose", "agent", "planner")
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # an agent's event is not a user's confirmation
            await mark("confirmed", planner_event)
        with pytest.raises(asyncpg.CheckViolationError):
            await mark("confirmed", planner_event, actor="agent")
        other = await event("confirm", "user", "dashboard", "f" * 64)
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # a confirmation of another hash
            await mark("confirmed", other)
        with pytest.raises(asyncpg.CheckViolationError):  # a half-filled confirmation
            await pool.execute("UPDATE skill_proposals SET confirmed_event_id = $2 WHERE id = $1", pid, other)
        good = await event("confirm", "user", "dashboard")
        await mark("confirmed", good)
        assert await pool.fetchval("SELECT status FROM skill_proposals") == "confirmed"
        with pytest.raises(asyncpg.CheckViolationError):  # new content with the old confirmation
            await pool.execute("UPDATE skill_proposals SET content_sha256 = $2 WHERE id = $1", pid, "e" * 64)

    run_db(test, monkeypatch)


def test_the_database_allows_one_pending_proposal_per_name_and_checks_the_target(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)

        async def insert(**over):
            values = {"project_id": project_id, "action": "create", "item_id": None, "base_sha256": None,
                      "name": "release-check", "status": "pending", "why": "w", **over}
            return await pool.execute(
                "INSERT INTO skill_proposals (project_id, action, item_id, base_sha256, name, description, content, "
                "content_sha256, why, proposer, status, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, 'd', 'c', 'h', $6, 'p', $7, now())",
                values["project_id"], values["action"], values["item_id"], values["base_sha256"], values["name"],
                values["why"], values["status"])

        with pytest.raises(asyncpg.UniqueViolationError):
            await insert()
        await insert(status="rejected")
        await insert(name="another")
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(name="third", action="update")
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(name="third", item_id="skill-x", base_sha256="h")
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(name="third", why="x" * 501)
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(name="third", status="approved")
        assert proposal["status"] == "pending"

    run_db(test, monkeypatch)


def test_a_confirmed_status_must_point_at_this_proposals_dashboard_confirm_event(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        first = await _accepted(pool, project_id)
        second = await _accepted(pool, project_id, name="other-skill")
        sha = first["content_sha256"]

        async def event(proposal, action, actor, via, content):
            return await pool.fetchval(
                "INSERT INTO skill_proposal_events (proposal_id, action, actor_kind, via, content_sha256) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id", proposal, action, actor, via, content)

        async def mark(proposal, event_id, content):
            await pool.execute(
                "UPDATE skill_proposals SET status = 'confirmed', confirmed_event_id = $2, confirmed_actor = 'user', "
                "confirmed_sha256 = $3 WHERE id = $1", proposal, event_id, content)

        # A user's reject, a user's edit and a user's restore are not confirmations, even with the right hash.
        for action in ("reject", "edit", "restore"):
            other = await event(first["proposal_id"], action, "user", "dashboard", sha)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await mark(first["proposal_id"], other, sha)
        # A confirmation of another proposal cannot be reused, even when the hash matches.
        foreign = await event(second["proposal_id"], "confirm", "user", "dashboard", sha)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await mark(first["proposal_id"], foreign, sha)
        assert await pool.fetchval("SELECT status FROM skill_proposals WHERE id = $1", first["proposal_id"]) == "pending"
        own = await event(first["proposal_id"], "confirm", "user", "dashboard", sha)
        await mark(first["proposal_id"], own, sha)
        assert await pool.fetchval("SELECT status FROM skill_proposals WHERE id = $1", first["proposal_id"]) == "confirmed"
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # nor may the first's confirmation move to the second
            await pool.execute(
                "UPDATE skill_proposals SET status = 'confirmed', confirmed_event_id = $2, confirmed_actor = 'user', "
                "confirmed_sha256 = $3 WHERE id = $1", second["proposal_id"], own, second["content_sha256"])

    run_db(test, monkeypatch)


def test_confirming_after_the_expiry_expires_the_proposal_and_writes_nothing(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        with pytest.raises(sp.ProposalConflict, match="expired"):
            await sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"],
                             principal=OPERATOR, now=T0 + sp.EXPIRY)
        row = await pool.fetchrow("SELECT status, confirmed_event_id FROM skill_proposals")
        assert (row["status"], row["confirmed_event_id"]) == ("expired", None)
        assert await _count(pool, "library_items") == 0
        assert [e["action"] for e in await pool.fetch(
            "SELECT action FROM skill_proposal_events ORDER BY id")] == ["propose", "expire"]
        with pytest.raises(sp.ProposalConflict, match="expired"):
            await sp.edit_and_confirm(project_id, proposal["proposal_id"], proposal["content_sha256"],
                                      {"content": "x\n"}, principal=OPERATOR, now=T0 + sp.EXPIRY)

    run_db(test, monkeypatch)


# --- Expiry and restore ---


def test_seven_days_expire_a_pending_proposal_and_restore_checks_it_again(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        proposal = await _accepted(pool, project_id)
        async with pool.acquire() as conn:
            assert await sp.expire_due(conn, T0 + sp.EXPIRY - timedelta(seconds=1)) == 0
            assert await sp.expire_due(conn, T0 + sp.EXPIRY) == 1
            assert await sp.expire_due(conn, T0 + sp.EXPIRY) == 0
        assert (await pool.fetchrow("SELECT status, decided_at FROM skill_proposals"))["status"] == "expired"
        listed = await sp.list_view(project_id)
        assert [p["status"] for p in listed["proposals"]] == ["expired"] and listed["mode"] == "on"
        with pytest.raises(sp.ProposalConflict):  # an expired one is not confirmable
            await sp.confirm(project_id, proposal["proposal_id"], proposal["content_sha256"], principal=OPERATOR)

        later = T0 + timedelta(days=9)
        restored = await sp.restore(project_id, proposal["proposal_id"], principal=OPERATOR, now=later)
        assert restored["proposal"]["status"] == "pending" and restored["proposal"]["expires_at"] == later + sp.EXPIRY
        assert [e["action"] for e in await pool.fetch(
            "SELECT action FROM skill_proposal_events ORDER BY id")] == ["propose", "expire", "restore"]
        with pytest.raises(sp.ProposalConflict):  # only an expired one is restorable
            await sp.restore(project_id, proposal["proposal_id"], principal=OPERATOR)

    run_db(test, monkeypatch)


def test_restore_refuses_when_the_world_moved_on(monkeypatch):
    async def test(pool):
        project_id = await _project(pool)
        added = await project_skills.add_skill(project_id, {
            "name": "release-check", "description": "Use for releases.", "content": "Old.\n"})
        stale = await _accepted(pool, project_id, action="update", skill="release-check", content="New.\n")
        newer = await _accepted(pool, project_id, name="other-skill")
        duplicate = await _accepted(pool, project_id, name="third-skill")
        async with pool.acquire() as conn:
            await sp.expire_due(conn, T0 + sp.EXPIRY)
        await project_skills.update_skill(project_id, added["skill"]["skill_id"], {"content": "Meanwhile.\n"})
        with pytest.raises(sp.ProposalRefused) as refused:
            await sp.restore(project_id, stale["proposal_id"], principal=OPERATOR)
        assert refused.value.reason is sp.Refusal.STALE
        # A newer proposal of the same name is pending: the older one is not brought back over it.
        again = await sp.propose(project_id, _item(name="third-skill"), proposer="p",
                                 evidence=[{"task": "task-3"}], now=T0 + sp.EXPIRY)
        assert again.proposal is not None
        with pytest.raises(sp.ProposalRefused) as refused:
            await sp.restore(project_id, duplicate["proposal_id"], principal=OPERATOR)
        assert refused.value.reason is sp.Refusal.STALE
        await sp.set_mode(project_id, "off")
        with pytest.raises(sp.ProposalRefused) as refused:
            await sp.restore(project_id, newer["proposal_id"], principal=OPERATOR)
        assert refused.value.reason is sp.Refusal.OFF
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals WHERE status = 'expired'") == 3

    run_db(test, monkeypatch)


def test_housekeeping_expires_overdue_proposals(monkeypatch, tmp_path):
    from looper.agents import housekeeping

    async def test(pool):
        project_id = await _project(pool)
        await _accepted(pool, project_id)
        await pool.execute("UPDATE skill_proposals SET expires_at = now() - interval '1 minute'")
        report = await housekeeping.run_once(tmp_path, datetime.now(timezone.utc), current_runner=None, commands=None)
        assert report.skill_proposals_expired == 1 and "skill proposals" not in report.failed_steps
        assert await pool.fetchval("SELECT status FROM skill_proposals") == "expired"

    run_db(test, monkeypatch)


# --- The daily note (RFC D21) ---


def _notice_env(monkeypatch, *, chat_id="ou_operator", group=False, operator="ou_operator"):
    from looper.agents import gate_service, operator_identity

    sent: list[tuple] = []

    async def sender(channel_id, chat, text, **kw):
        sent.append((channel_id, chat, text))
        return True, None

    async def target():
        return SimpleNamespace(channel_id="lark-test", in_group=group,
                               target=SimpleNamespace(id=chat_id, group=group))

    async def operator_id(channel_id):
        return operator

    monkeypatch.setattr(gate_service, "dashboard_target", target)
    monkeypatch.setattr(operator_identity, "operator_sender_id", operator_id)
    return sent, sender


def test_the_operator_gets_one_note_a_day_with_names_and_reasons_and_no_content(monkeypatch):
    from looper.agents import skill_proposal_notice as notice
    from looper.agents.skill_proposal_notice import Notice

    async def test(pool):
        project_id = await _project(pool)
        secretish = "PRIVATE-BODY-MARKER"
        await _accepted(pool, project_id, description="Describes a thing.",
                        content=f"# Title\n{secretish}\n", why="Same release steps came up three times.")
        sent, sender = _notice_env(monkeypatch)
        notice.configure(None)
        assert await notice.notify_pending() is Notice.NO_SENDER and not sent
        notice.configure(sender, dashboard_url="https://looper.example")
        day = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
        assert await notice.notify_pending(now=day) is Notice.SENT
        assert await notice.notify_pending(now=day + timedelta(hours=3)) is Notice.ALREADY_SENT
        assert len(sent) == 1 and sent[0][1] == "ou_operator"
        text = sent[0][2]
        assert "release-check" in text and "Same release steps" in text and f"/#projects/{project_id}/skills" in text
        assert secretish not in text and "Describes a thing" not in text and "Run the tests" not in text
        assert await notice.notify_pending(now=day + timedelta(days=1)) is Notice.SENT and len(sent) == 2
        await pool.execute("UPDATE skill_proposals SET status = 'rejected'")
        assert await notice.notify_pending(now=day + timedelta(days=2)) is Notice.NOTHING_PENDING and len(sent) == 2
        notice.configure(None)

    run_db(test, monkeypatch)


@pytest.mark.parametrize("env,expected", [
    ({"group": True, "chat_id": "oc_group"}, "GROUP"),
    ({"chat_id": "ou_someone_else"}, "NOT_OPERATOR"),
    ({"operator": None}, "NOT_OPERATOR"),
], ids=["group-chat", "another-users-dm", "operator-unknown"])
def test_the_note_never_goes_to_a_group_or_to_anyone_but_the_operators_dm(monkeypatch, env, expected):
    from looper.agents import skill_proposal_notice as notice
    from looper.agents.skill_proposal_notice import Notice

    async def test(pool):
        project_id = await _project(pool)
        await _accepted(pool, project_id)
        sent, sender = _notice_env(monkeypatch, **env)
        notice.configure(sender, dashboard_url="https://looper.example")
        day = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
        assert await notice.notify_pending(now=day) is Notice[expected] and not sent
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1", notice.SENT_KEY) == 0
        notice.configure(None)

    run_db(test, monkeypatch)


# --- Off changes nothing a project, its export or a run shows ---


def test_a_project_that_never_enabled_proposals_reads_and_exports_as_before(monkeypatch):
    from looper.agents import config_transfer, projects as agent_projects

    async def exported(pool):
        async with pool.acquire() as conn:
            chunks = [c async for c in config_transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]
        return [c for c in chunks if '"Baseline"' in c]

    async def view(pool, project_id):
        row = {**await projects.get_project(project_id, pool=pool), **FIXED_ROW}
        return json.dumps(agent_projects.project_view(row), sort_keys=True, default=str)

    async def test(pool):
        project_id = await _project(pool, mode=None, name="Baseline")
        await pool.execute("UPDATE projects SET description = 'd', background = 'b' WHERE id = $1", project_id)
        assert await view(pool, project_id) == OFF_BASELINE["project_view"]
        assert await exported(pool) == OFF_BASELINE["export_chunks"]
        # On: the key appears. Off again: NULL, and both texts are the baseline's once more.
        await sp.set_mode(project_id, "on")
        assert '"skill_proposals": "on"' in await view(pool, project_id)
        assert '"skill_proposals":"on"' in "".join(await exported(pool))
        await sp.set_mode(project_id, "off")
        assert await pool.fetchval("SELECT skill_proposals FROM projects") is None
        assert await view(pool, project_id) == OFF_BASELINE["project_view"]
        assert await exported(pool) == OFF_BASELINE["export_chunks"]
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE projects SET skill_proposals = 'maybe'")

    run_db(test, monkeypatch)


# --- The migration ---


def test_the_migration_checks_the_objects_not_only_its_marker(monkeypatch):
    from looper.db.connection import SCHEMA_PATH
    from looper.db.migrations import skill_proposals as migration

    async def test(pool):
        assert "skill_proposals" not in SCHEMA_PATH.read_text(encoding="utf-8")
        project_id = await _project(pool)
        await _accepted(pool, project_id)
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'skill_proposals'")
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)  # a fresh database records only the baseline marker
        await pool.execute("DROP TABLE skill_proposals CASCADE")
        await pool.execute("ALTER TABLE projects DROP COLUMN skill_proposals")
        async with pool.acquire() as conn, conn.transaction():
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state="current")
            assert not await migration.pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 0
        async with pool.acquire() as conn, conn.transaction():
            await migration.apply(conn, sql="", state="current")  # a second pass changes nothing
        async with fresh_schema() as other:
            await other.execute("CREATE TABLE unrelated (id int, "
                                "CONSTRAINT skill_proposals_confirmed_event_fk CHECK (id > 0))")
            assert await pool.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'skill_proposals_confirmed_event_fk' "
                "AND conrelid = to_regclass('skill_proposals')") == 1

    run_db(test, monkeypatch)
