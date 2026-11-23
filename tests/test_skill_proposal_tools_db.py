"""The Manager can draft project skills only in an operator turn, after that project opts in."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from looper.agents.ops import Audience, OpError, Risk, Turn, check_arguments
from looper.agents.ops import skill_proposals as tools
from looper.agents.ops.service import DEFAULT_SCOPE, ManagerOps, Platform
from looper.agents.tiers.manager import TOOLS_CONTRACT
from tests.db_support import run_db

BASELINE = Path(__file__).parent / "fixtures" / "skill_proposals_manager_off.json"
ITEM = {"action": "create", "name": "release-check", "description": "Use before a release.",
        "content": "# Release\nRun the project checks before publishing.",
        "why": "The same checks were repeated across releases.",
        "evidence": [{"task": "task-evidence", "step": "s1"}]}


def test_object_evidence_support_keeps_legacy_string_array_validation():
    schema = {"properties": {"items": {"type": "array", "items": {"type": "string", "minLength": 1}}}}
    # Existing string arrays accepted empty strings before object evidence was introduced.
    assert check_arguments(schema, {"items": [""]}) == {"items": [""]}


@pytest.mark.parametrize("field", ["task", "step"])
def test_manager_evidence_identifiers_have_the_same_64_character_cap(field):
    for length in (64, 65):
        arguments = {"project": "Demo", **ITEM, "evidence": [{"task": "task-evidence", field: "x" * length}]}
        if length == 64:
            assert check_arguments(tools.TOOL.schema, arguments) == arguments
        else:
            with pytest.raises(OpError):
                check_arguments(tools.TOOL.schema, arguments)


async def _seed(pool: Any, *, mode: str | None = "on") -> None:
    await pool.execute("INSERT INTO projects (id, name, skill_proposals) VALUES ('project-a', 'Demo', $1)", mode)
    await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ('task-evidence', 'Release', 'project-a')")
    await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                       "VALUES ('step-evidence', 'task-evidence', 0, 's1', 'Checks')")


def _ops() -> ManagerOps:
    return ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9")


async def _call(ops: ManagerOps, turn: Turn, item: dict[str, Any] | None = None) -> dict[str, Any]:
    ops.begin_turn(turn, binding="proposal-turn")
    try:
        return await ops.call(tools.TOOL_NAME, {"project": "Demo", **(ITEM if item is None else item)},
                              binding="proposal-turn")
    finally:
        ops.end_turn(turn)


def test_off_listing_and_contract_match_the_fixed_main_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(pool: Any) -> None:
        await _seed(pool, mode=None)
        ops, turn = _ops(), Turn("web", "synthetic-chat", message_id="m1")
        baseline = json.loads(BASELINE.read_text())
        assert baseline["commit"] == "6613380fa48d7e7d2a7103f53ad84621d9c5efb5"
        assert await ops.current_listing(turn) == baseline["listing"]
        assert TOOLS_CONTRACT == baseline["contract"]
        await pool.execute("UPDATE projects SET skill_proposals = 'on'")
        enabled = await ops.current_listing(turn)
        assert enabled[:-1] == baseline["listing"] and enabled[-1]["name"] == tools.TOOL_NAME
        assert tools.TOOL_NAME not in [row["name"] for row in await ops.current_listing(Turn(None, None))]
        await pool.execute("UPDATE projects SET skill_proposals = 'off'")
        assert await ops.current_listing(turn) == baseline["listing"]

    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel", ["web", "lark", "telegram"])
def test_an_operator_turn_creates_a_pending_manager_proposal(monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    async def check(pool: Any) -> None:
        await _seed(pool)
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, config) VALUES ($1, $2, $3)",
                           f"channel-{channel}", channel, json.dumps({"operator_sender_id": "synthetic-operator"}))
        turn = Turn(channel, "synthetic-dm", message_id="m1", sender="synthetic-operator",
                    channel_id=f"channel-{channel}")
        result = await _call(_ops(), turn)
        assert result["status"] == "ok", result
        assert "waits for confirmation" in result["result"]["message"]
        proposal = await pool.fetchrow("SELECT p.*, e.via FROM skill_proposals p JOIN skill_proposal_events e "
                                       "ON e.proposal_id = p.id WHERE e.action = 'propose'")
        assert (proposal["name"], proposal["status"], proposal["via"], proposal["proposer"]) == (
            ITEM["name"], "pending", "manager", "manager")
        assert await pool.fetchval("SELECT count(*) FROM library_items WHERE origin = 'project'") == 0
        assert tools.TOOL.risk is Risk.WRITE and tools.TOOL.audience is Audience.OPERATOR

    run_db(check, monkeypatch)


@pytest.mark.parametrize("own", [False, True])
def test_another_sender_and_an_autonomous_turn_cannot_propose(monkeypatch: pytest.MonkeyPatch, own: bool) -> None:
    async def check(pool: Any) -> None:
        await _seed(pool)
        turn = Turn(None, None) if own else Turn("lark", "synthetic-group", message_id="m1",
                                                  sender="synthetic-member", group=True)
        result = await _call(_ops(), turn)
        assert result["status"] == "error"
        assert result["error"]["code"] == ("needs_user_turn" if own else "forbidden")
        if not own:
            recorded = await pool.fetchval("SELECT result FROM manager_actions ORDER BY id DESC LIMIT 1")
            assert json.loads(recorded)["reason"] == "operator_only"
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 0

    run_db(check, monkeypatch)


def test_content_refusal_returns_the_proposal_rules_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(pool: Any) -> None:
        await _seed(pool)
        result = await _call(_ops(), Turn("web", "synthetic-chat", message_id="m1"),
                             {**ITEM, "content": "sk-" + "x" * 30})
        assert result["result"]["refusal"] == "secret"
        assert "sk-" + "x" * 30 not in json.dumps(result)
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 0

    run_db(check, monkeypatch)


def test_other_managers_propose_only_within_their_project_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(pool: Any) -> None:
        await _seed(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-b', 'Other')")
        turn = Turn("web", "synthetic-chat", message_id="m1")
        scope = replace(DEFAULT_SCOPE, manager_id="manager-secondary", tasks=replace(
            DEFAULT_SCOPE.tasks, project_ids=("project-a",)))
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", scope=scope)
        assert tools.TOOL_NAME in [row["name"] for row in await ops.current_listing(turn)]
        result = await _call(ops, turn)
        assert result["status"] == "ok", result
        assert await pool.fetchval("SELECT proposer FROM skill_proposals") == "manager-secondary"
        ops.set_scope(replace(scope, tasks=replace(scope.tasks, project_ids=("project-b",))))
        assert tools.TOOL_NAME not in [row["name"] for row in await ops.current_listing(turn)]
        assert (await _call(ops, Turn("web", "synthetic-chat", message_id="m2")))["status"] == "error"
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 1

    run_db(check, monkeypatch)


def test_task_only_manager_does_not_gain_a_skill_write_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.managers_api import ManagerTools

    async def check(pool: Any) -> None:
        await _seed(pool)
        scope = replace(DEFAULT_SCOPE, tool_scope=ManagerTools.TASKS)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", scope=scope)
        assert tools.TOOL_NAME not in [row["name"] for row in await ops.current_listing(Turn("web", "synthetic-chat"))]
    run_db(check, monkeypatch)
