"""Skill proposals through authenticated v1 dispatch: who may read, who may decide, and what the CLI offers."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper import loopercli
from looper.agents import api_v1, skill_proposals as sp
from looper.agents.v1_domains.project_skills import OPERATIONS
from looper.db import projects
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PROPOSALS = [op for op in OPERATIONS if op.operation_id.startswith("project_skills.proposals.")]
ITEM = {"action": "create", "name": "release-check", "description": "Use before a release.",
        "content": "# Release check\n1. Run the tests.\n", "why": "It came up three times."}


def _valid(operation_id: str, result: Any) -> None:
    operation = next(op for op in OPERATIONS if op.operation_id == operation_id)
    api_v1.validate_arguments({"type": "object", "properties": {"result": operation.result}}, {"result": result})


async def _setup(pool: Any) -> tuple[str, dict[str, Any]]:
    project = await projects.create_project(name="calc", pool=pool)
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ('task-1', $1, 'r', 'done')",
                       project["id"])
    await sp.set_mode(project["id"], "on")
    result = await sp.propose(project["id"], ITEM, proposer="planner:task-1", evidence=[{"task": "task-1"}])
    assert result.proposal is not None
    return project["id"], result.proposal


def test_the_operations_are_declared_and_no_agent_tool_reaches_them():
    assert {op.operation_id.rsplit(".", 1)[1] for op in PROPOSALS} == {
        "list", "get", "confirm", "reject", "restore", "set_mode"}
    for op in PROPOSALS:
        assert op.tool is None and op.action is None, op.operation_id
        assert 0 < len(op.summary) <= 120 and op.description.startswith(op.summary)
    operator_only = {op.operation_id.rsplit(".", 1)[1] for op in PROPOSALS if op.operator_only}
    assert operator_only == {"confirm", "reject", "restore", "set_mode"}  # list and get stay readable by a client
    assert {op.operation_id.rsplit(".", 1)[1]: op.risk for op in PROPOSALS} == {
        "list": "read", "get": "read", "confirm": "write", "reject": "write", "restore": "write", "set_mode": "write"}


def test_confirm_has_no_cli_command_and_the_reads_do():
    document = api_v1.document()
    path = "/project_skills/proposals/confirm"
    assert document["paths"][path]["post"]["x-looper-cli"] is False
    commands = {(head, tail) for head, tail, *_ in loopercli.generated_operations(document)}
    assert ("project-skills", "proposals.list") in commands and ("project-skills", "proposals.get") in commands
    assert ("project-skills", "proposals.confirm") not in commands
    assert not any("confirm" in tail for head, tail in commands if head == "project-skills")


def test_a_client_reads_proposals_and_cannot_decide_or_switch_them(tmp_path, monkeypatch):
    async def test(pool):
        project_id, proposal = await _setup(pool)
        ids = {"project_id": project_id, "proposal_id": proposal["proposal_id"]}
        async with v1_api(tmp_path, pool) as call:
            status, listed = await call("project_skills.proposals.list", {"project_id": project_id}, client=True)
            assert status == 200, listed
            _valid("project_skills.proposals.list", listed["result"])
            assert listed["result"]["mode"] == "on" and listed["result"]["proposals"][0]["name"] == "release-check"
            status, shown = await call("project_skills.proposals.get", ids, client=True)
            assert status == 200 and shown["result"]["proposal"]["content"] == ITEM["content"]
            _valid("project_skills.proposals.get", shown["result"])
            for action, body in (
                ("confirm", {**ids, "content_sha256": proposal["content_sha256"]}),
                ("confirm", {**ids, "content_sha256": proposal["content_sha256"], "fields": {"content": "x\n"}}),
                ("reject", {**ids, "reason": "no"}),
                ("restore", ids),
                ("set_mode", {"project_id": project_id, "mode": "off"}),
            ):
                status, answer = await call(f"project_skills.proposals.{action}", body, client=True)
                assert status == 403 and answer["error"] == {
                    "code": "forbidden", "message": "operator identity is required"}, (action, answer)
        assert await pool.fetchval("SELECT status FROM skill_proposals") == "pending"
        assert await pool.fetchval("SELECT count(*) FROM library_items") == 0
        assert await pool.fetchval("SELECT skill_proposals FROM projects") == "on"

    run_db(test, monkeypatch, timeout=60)


def test_the_operator_confirms_with_the_displayed_hash_and_a_stale_hash_is_409(tmp_path, monkeypatch):
    async def test(pool):
        project_id, proposal = await _setup(pool)
        ids = {"project_id": project_id, "proposal_id": proposal["proposal_id"]}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("project_skills.proposals.confirm", {**ids, "content_sha256": "0" * 64})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            status, answer = await call("project_skills.proposals.confirm", {**ids, "content_sha256": "short"})
            assert status == 400
            status, answer = await call("project_skills.proposals.confirm",
                                        {**ids, "content_sha256": proposal["content_sha256"]})
            assert status == 200, answer
            _valid("project_skills.proposals.confirm", answer["result"])
            assert answer["result"]["proposal"]["status"] == "confirmed" and answer["result"]["version"] == 1
            status, answer = await call("project_skills.proposals.confirm",
                                        {**ids, "content_sha256": proposal["content_sha256"]})
            assert status == 409
            status, answer = await call("project_skills.proposals.get", {**ids, "project_id": "proj-other"})
            assert status == 404
        assert await pool.fetchval("SELECT author_kind FROM skill_versions") == "proposal"

    run_db(test, monkeypatch, timeout=60)


def test_edit_reject_restore_and_the_switch_through_the_operator(tmp_path, monkeypatch):
    async def test(pool):
        project_id, proposal = await _setup(pool)
        ids = {"project_id": project_id, "proposal_id": proposal["proposal_id"]}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("project_skills.proposals.confirm", {
                **ids, "content_sha256": proposal["content_sha256"], "fields": {"content": "Edited.\n"}})
            assert status == 200, answer
            assert answer["result"]["proposal"]["status"] == "confirmed_edited"
            assert await pool.fetchval("SELECT author_kind FROM skill_versions") == "operator"

            second = (await sp.propose(project_id, {**ITEM, "name": "other-skill"}, proposer="p",
                                       evidence=[{"task": "task-1"}])).proposal
            other = {"project_id": project_id, "proposal_id": second["proposal_id"]}
            status, answer = await call("project_skills.proposals.reject", {**other, "reason": "not general"})
            assert status == 200 and answer["result"]["proposal"]["decision_note"] == "not general"
            _valid("project_skills.proposals.reject", answer["result"])
            await pool.execute("UPDATE skill_proposals SET status = 'expired' WHERE id = $1", second["proposal_id"])
            status, answer = await call("project_skills.proposals.restore", other)
            assert status == 200 and answer["result"]["proposal"]["status"] == "pending", answer
            _valid("project_skills.proposals.restore", answer["result"])

            status, answer = await call("project_skills.proposals.set_mode", {"project_id": project_id, "mode": "off"})
            assert status == 200 and answer["result"] == {"project_id": project_id, "mode": "off"}
            assert await pool.fetchval("SELECT skill_proposals FROM projects") is None
            status, listed = await call("project_skills.proposals.list", {"project_id": project_id})
            assert listed["result"]["mode"] == "off"
            status, answer = await call("project_skills.proposals.set_mode", {"project_id": "nope", "mode": "on"})
            assert status == 404
            status, answer = await call("project_skills.proposals.set_mode", {"project_id": project_id, "mode": "maybe"})
            assert status == 400

    run_db(test, monkeypatch, timeout=60)


def test_the_project_operations_show_the_switch_only_when_it_is_on(tmp_path, monkeypatch):
    async def test(pool):
        project = await projects.create_project(name="calc", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            status, listed = await call("project_admin.list", {})
            assert status == 200 and "skill_proposals" not in json.dumps(listed["result"])
            await call("project_skills.proposals.set_mode", {"project_id": project["id"], "mode": "on"})
            status, listed = await call("project_admin.list", {})
            assert listed["result"]["projects"][0]["skill_proposals"] == "on"
            await call("project_skills.proposals.set_mode", {"project_id": project["id"], "mode": "off"})
            status, listed = await call("project_admin.list", {})
            assert "skill_proposals" not in json.dumps(listed["result"])

    run_db(test, monkeypatch, timeout=60)
