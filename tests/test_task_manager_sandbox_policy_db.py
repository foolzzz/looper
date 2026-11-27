"""The per-project network allowlist of the task Manager's sandbox: storage, audit and operator-only operations."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, task_manager_network
from looper.agents.ops import ErrorCode, OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.task_manager_network import SCOPE_TEXT
from looper.db import projects as db_projects
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN

GET, UPDATE = "projects.task_manager_sandbox_get", "projects.task_manager_sandbox_update"


def _call(name: str, **arguments):
    operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
    return api_v1.domain_for(operation).run(None, operation, arguments, api_v1.OPERATOR, {})


async def _project(pool, name: str = "Sandbox policy") -> str:
    return (await db_projects.create_project(name=name, work_dir="/tmp/policy", pool=pool))["id"]


async def _rows(pool, project_id: str):
    policy = await pool.fetch("SELECT * FROM task_manager_sandbox_policies WHERE project_id = $1", project_id)
    audit = await pool.fetch("SELECT * FROM task_manager_sandbox_policy_audit WHERE project_id = $1 ORDER BY id",
                             project_id)
    return policy, audit


def test_a_project_without_a_row_has_the_default_and_a_read_writes_nothing(monkeypatch):
    async def check(pool):
        project = await _project(pool)
        assert await _call("projects.task_manager_sandbox_get", project=project) == {
            "project": project, "network_allowed_domains": [], "revision": 0}
        assert await _rows(pool, project) == ([], [])
        assert await task_manager_network.allowed_domains(project) == ()
    run_db(check, monkeypatch)


def test_an_update_replaces_the_list_reads_it_back_and_writes_one_audit_row(monkeypatch):
    async def check(pool):
        project = await _project(pool)
        first = await _call(UPDATE, project=project, network_allowed_domains=["Registry.NPMjs.org.", "*.pypi.org"])
        assert first == {"project": project, "network_allowed_domains": ["registry.npmjs.org", "*.pypi.org"],
                         "revision": 1, "added": ["registry.npmjs.org", "*.pypi.org"], "removed": []}
        second = await _call(UPDATE, project=project, network_allowed_domains=["*.pypi.org", "example.com"])
        assert (second["revision"], second["added"], second["removed"]) == (2, ["example.com"],
                                                                           ["registry.npmjs.org"])
        assert await _call(GET, project=project) == {
            "project": project, "network_allowed_domains": ["*.pypi.org", "example.com"], "revision": 2}
        _, audit = await _rows(pool, project)
        assert [(row["actor"], row["field"], row["revision"], json.loads(row["old"]), json.loads(row["new"]))
                for row in audit] == [
            ("operator", "network_allowed_domains", 1, [], ["registry.npmjs.org", "*.pypi.org"]),
            ("operator", "network_allowed_domains", 2, ["registry.npmjs.org", "*.pypi.org"],
             ["*.pypi.org", "example.com"])]
        assert await task_manager_network.allowed_domains(project) == ("*.pypi.org", "example.com")
    run_db(check, monkeypatch)


def test_the_project_can_be_named_instead_of_identified(monkeypatch):
    async def check(pool):
        project = await _project(pool, "Named project")
        result = await _call(UPDATE, project="Named project", network_allowed_domains=["example.com"])
        assert result["project"] == project and result["revision"] == 1
        with pytest.raises(OpError) as missing:
            await _call(GET, project="no such project")
        assert missing.value.code is ErrorCode.NOT_FOUND
    run_db(check, monkeypatch)


@pytest.mark.parametrize("values, entry", [
    (["example.com", "10.0.0.1"], "10.0.0.1"), (["a.com", "A.com"], "a.com"), (["intranet"], "intranet"),
    (["example.com:443"], "example.com:443"), ("example.com", None)])
def test_an_invalid_list_is_refused_and_nothing_is_written(monkeypatch, values, entry):
    async def check(pool):
        project = await _project(pool)
        await _call(UPDATE, project=project, network_allowed_domains=["keep.example.com"])
        with pytest.raises(OpError) as refused:
            await _call(UPDATE, project=project, network_allowed_domains=values)
        assert refused.value.code is ErrorCode.NETWORK_POLICY_INVALID
        assert refused.value.field == "network_allowed_domains"
        assert entry is None or entry in str(refused.value)
        policy, audit = await _rows(pool, project)
        assert (json.loads(policy[0]["network_allowed_domains"]), policy[0]["revision"], len(audit)) == (
            ["keep.example.com"], 1, 1)
    run_db(check, monkeypatch)


def test_expected_revision_must_match_and_a_mismatch_writes_nothing(monkeypatch):
    async def check(pool):
        project = await _project(pool)
        with pytest.raises(OpError) as stale:
            await _call(UPDATE, project=project, network_allowed_domains=["a.example.com"], expected_revision=3)
        assert stale.value.code is ErrorCode.CONFLICT and stale.value.details == {"current_version": 0}
        assert await _rows(pool, project) == ([], [])  # no row, not even the default one
        await _call(UPDATE, project=project, network_allowed_domains=["a.example.com"], expected_revision=0)
        with pytest.raises(OpError) as stale:
            await _call(UPDATE, project=project, network_allowed_domains=["b.example.com"], expected_revision=0)
        assert stale.value.code is ErrorCode.CONFLICT and stale.value.details == {"current_version": 1}
        assert (await _call(GET, project=project))["network_allowed_domains"] == ["a.example.com"]
        assert len((await _rows(pool, project))[1]) == 1
    run_db(check, monkeypatch)


def test_a_stored_entry_this_version_refuses_is_not_returned_as_an_allowlist(monkeypatch):
    async def check(pool):
        project = await _project(pool)
        await pool.execute("INSERT INTO task_manager_sandbox_policies (project_id, network_allowed_domains, revision, "
                           "updated_by) VALUES ($1, '[\"127.0.0.1\"]', 1, 'newer version')", project)
        with pytest.raises(task_manager_network.PolicyInvalid) as refused:
            await task_manager_network.allowed_domains(project)
        assert refused.value.entry == "127.0.0.1"
    run_db(check, monkeypatch)


def test_deleting_the_project_removes_its_policy_and_audit(monkeypatch):
    async def check(pool):
        project = await _project(pool)
        await _call(UPDATE, project=project, network_allowed_domains=["example.com"])
        await pool.execute("DELETE FROM projects WHERE id = $1", project)
        assert await _rows(pool, project) == ([], [])
    run_db(check, monkeypatch)


@pytest.mark.parametrize("name", [GET, UPDATE])
def test_the_operations_are_operator_only_inside_and_describe_the_scope(name):
    operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
    assert operation.operator_only and operation.tool is None and operation.level.value == "inside"
    assert len(operation.summary) <= 120 and operation.description.startswith(operation.summary)
    assert SCOPE_TEXT in operation.description


@pytest.mark.parametrize("name", [GET, UPDATE])
@pytest.mark.usefixtures("clients")
def test_an_agent_token_cannot_read_or_change_it(tmp_path, monkeypatch, name):
    operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
    run = AsyncMock(side_effect=AssertionError("agent token reached the policy"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            body = {"project": "p", "network_allowed_domains": ["example.com"]} if name == UPDATE else {"project": "p"}
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
    asyncio.run(check())


def test_the_operator_gets_a_typed_error_over_http(tmp_path, monkeypatch):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check(pool):
        project = await _project(pool)
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, "POST /api/v1/projects/task_manager_sandbox_update",
                                            _bearer(TOKEN), json.dumps({
                                                "project": project, "network_allowed_domains": ["10.0.0.1"]}))
            assert status == 400 and answer["error"]["code"] == "network_policy_invalid", answer
            status, _, answer = await _send(server, "POST /api/v1/projects/task_manager_sandbox_update",
                                            _bearer(TOKEN), json.dumps({
                                                "project": project, "network_allowed_domains": ["example.com"]}))
            assert status == 200 and answer["result"]["revision"] == 1, answer
    run_db(check, monkeypatch)
