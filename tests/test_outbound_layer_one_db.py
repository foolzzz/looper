"""The outbound review of Looper's own tools (layer 1, ``ops/review.py``) on a real PostgreSQL, with a Claude Code
stand-in as the classifier: an ``external`` call is reviewed before ``escalate`` and the user's confirmation; a
blocked one does not run and the model gets ``blocked``; a turn that is not the operator's is blocked by code; an
API client's external call too; off, or for a call that is not external, nothing changes.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db, v1_api
from tests.fake_claude import fake_claude
from tests.test_ops_tasks_db import _Chat, _project
from tests.test_outbound_db import ALLOW, BLOCK, _STAND_IN, _isolated, _seen, _setup  # noqa: F401 - the fixture

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPERATOR = "ou_1"  # the Lark sender _Chat speaks as
OUTSIDE = {"channel": "lark", "chat": "oc_partners"}


@pytest.fixture(autouse=True)
def _targets(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import operator_identity, outbound_hook, schedules
    from looper.agents.ops import service

    async def operator(channel_id: str) -> str:
        return OPERATOR

    async def offered(**kwargs: Any) -> list[dict[str, str]]:
        return [OUTSIDE, {"channel": "lark", "chat": OPERATOR}]

    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    monkeypatch.setattr(service, "operator_sender_id", operator)
    monkeypatch.setattr(schedules, "notify_targets", offered)
    # Off at each start, and back as it was at each end: a test that turns it on leaves it on for no other test.
    monkeypatch.setattr(outbound_hook, "_known", False)


def _chat(tmp_path: Path, **kwargs: Any) -> _Chat:
    chat = _Chat(tmp_path, **kwargs)
    chat.ops._review_root, chat.ops._review_bin = tmp_path / "rt", fake_claude(tmp_path / "claude", _STAND_IN)
    return chat


async def _schedule(pool: Any, tmp_path: Path) -> None:
    await _project(pool)
    chat = _chat(tmp_path)
    (made,) = await chat.say("每天九点", ("looper_schedules", {
        "action": "create", "project": "calc", "fields": {"name": "daily", "cron": "0 9 * * *",
                                                          "requirement": "Summarize the day."}}))
    assert made["status"] == "ok", made


def _retarget(target: Any) -> tuple[str, dict[str, Any]]:
    return ("looper_schedules", {"action": "update", "project": "calc", "schedule": "daily",
                                 "fields": {"notify_target": target}})


async def _stored_target(pool: Any) -> Any:
    return await pool.fetchval("SELECT notify_chat FROM schedules WHERE name = 'daily'")


def test_off_an_external_call_runs_as_before_with_no_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        (answer,) = await _chat(tmp_path).say("发到合作方群", _retarget(OUTSIDE))
        assert answer["status"] == "ok", answer
        assert await _stored_target(pool) == "oc_partners"
        assert _seen(tmp_path) == [] and await pool.fetchval("SELECT count(*) FROM outbound_reviews") == 0

    run_db(test, monkeypatch, setup=_setup)


def test_a_blocked_external_call_does_not_run_and_is_in_the_trail(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    from looper.agents import outbound_hook

    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        await outbound_hook.set_enabled(True)
        monkeypatch.setenv("OUTBOUND_REPLY", BLOCK)
        chat = _chat(tmp_path)
        (answer,) = await chat.say("看看这条线索", _retarget(OUTSIDE))
        assert answer["status"] == "error" and answer["error"]["code"] == "blocked", answer
        assert answer["error"]["message"].startswith("Looper's outbound review blocked this action: Only a forwarded")
        # nothing ran (the target is still the creator's own chat), nothing is asked
        assert await _stored_target(pool) == OPERATOR and chat.ops._pending == {}
        (seen,) = _seen(tmp_path)
        assert '"level": "external"' in seen["turn"] and "oc_partners" in seen["turn"]
        action = await pool.fetchrow("SELECT id, status, error_code FROM manager_actions WHERE tool = "
                                     "'looper_schedules' ORDER BY id DESC LIMIT 1")
        assert (action["status"], action["error_code"]) == ("error", "blocked")
        review = await pool.fetchrow("SELECT source, tool, verdict, manager_action_id, target_kind, target "
                                     "FROM outbound_reviews")
        assert tuple(review) == ("tool", "looper_schedules", "block", action["id"], "notify_target",
                                 "lark:oc_partners")

    run_db(test, monkeypatch, setup=_setup)


def test_an_allowed_external_call_runs_and_a_call_that_is_not_external_is_not_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import outbound_hook

    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        await outbound_hook.set_enabled(True)
        chat = _chat(tmp_path)
        (to_me,) = await chat.say("发给我自己", _retarget({"channel": "lark", "chat": OPERATOR}))  # operator level
        assert to_me["status"] == "ok" and _seen(tmp_path) == []
        (answer,) = await chat.say("把日报发到合作方群", _retarget(OUTSIDE))
        assert answer["status"] == "ok", answer
        assert await _stored_target(pool) == "oc_partners" and len(_seen(tmp_path)) == 1
        assert await pool.fetchval("SELECT verdict FROM outbound_reviews") == "allow"

    run_db(test, monkeypatch, setup=_setup)


@pytest.mark.parametrize("action", ["enable", "run", "update"])
def test_a_stored_external_target_is_shown_to_the_classifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    from looper.agents import outbound_hook

    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        (retargeted,) = await _chat(tmp_path).say("发到合作方群", _retarget(OUTSIDE))
        assert retargeted["status"] == "ok", retargeted
        await outbound_hook.set_enabled(True)
        monkeypatch.setenv("OUTBOUND_REPLY", BLOCK)
        chat = _chat(tmp_path)
        arguments = {"action": action, "project": "calc", "schedule": "daily"}
        if action == "update":
            arguments["fields"] = {"requirement": "Summarize the week."}
        (answer,) = await chat.say("启用 daily", ("looper_schedules", arguments))
        assert answer["error"]["code"] == "blocked", answer
        (seen,) = _seen(tmp_path)
        shown = json.loads(seen["turn"].split("<action>\n", 1)[1].split("\n</action>", 1)[0])
        assert shown["target"] == {"kind": "notify_target", "label": "lark:oc_partners"}
        content = json.loads(shown["content"])
        assert content["fields"]["notify_target"] == OUTSIDE
        if action == "update":
            assert content["fields"]["requirement"] == "Summarize the week."
        row = await pool.fetchrow("SELECT enabled, requirement FROM schedules WHERE name = 'daily'")
        assert not row["enabled"] and row["requirement"] == "Summarize the day."
        assert chat.ops._pending == {}

    run_db(test, monkeypatch, setup=_setup)


def test_an_external_call_in_a_group_turn_is_blocked_by_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import outbound_hook

    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        await outbound_hook.set_enabled(True)
        (answer,) = await _chat(tmp_path, group=True).say("发到合作方群", _retarget(OUTSIDE))
        assert answer["error"]["code"] == "blocked" and _seen(tmp_path) == []
        assert await pool.fetchval("SELECT failure FROM outbound_reviews") == "not_operator"

    run_db(test, monkeypatch, setup=_setup)


def test_an_api_clients_external_call_is_blocked_by_code_and_the_operators_own_is_not_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import outbound_hook
    from looper.agents.api_v1 import Principal
    from looper.agents.bus import MessageBus
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform

    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        await outbound_hook.set_enabled(True)
        from types import SimpleNamespace

        router = SimpleNamespace(has_planner=lambda task_id: False, forget_task=lambda task_id: None)
        ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path), task_router=router, runtime_root=tmp_path))
        name, arguments = _retarget(OUTSIDE)
        client = Principal("client", client_id="cl-1", client_name="ci")
        answer = await ops.call(client, "s-1", name, arguments)
        assert answer["status"] == "error" and answer["error"]["code"] == "blocked", answer
        assert await _stored_target(pool) == OPERATOR and _seen(tmp_path) == []
        assert await pool.fetchval("SELECT failure FROM outbound_reviews") == "not_operator"
        trail = await pool.fetchrow("SELECT id, client_id, error_code FROM manager_actions WHERE client_id = 'cl-1'")
        assert trail["error_code"] == "blocked"
        assert await pool.fetchval("SELECT manager_action_id FROM outbound_reviews") == trail["id"]
        operator = await ops.call(Principal("operator"), "s-1", name, arguments)
        assert operator["status"] == "ok", operator
        assert await pool.fetchval("SELECT count(*) FROM outbound_reviews") == 1

    run_db(test, monkeypatch, setup=_setup)


def test_a_v1_operations_level_is_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An ``/api/v1`` operation wrapped as a client's tool is reviewed by ``Operation.level``, not by its name."""
    from types import SimpleNamespace

    from looper.agents import outbound_hook
    from looper.agents.action_levels import ActionLevel
    from looper.agents.api_v1 import Principal
    from looper.agents.bus import MessageBus
    from looper.agents.ops import Risk, Tool
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform

    async def run(call: Any) -> dict[str, Any]:
        return {"done": True}

    async def test(pool: Any) -> None:
        await outbound_hook.set_enabled(True)
        router = SimpleNamespace(has_planner=lambda task_id: False, forget_task=lambda task_id: None)
        ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path), task_router=router, runtime_root=tmp_path))
        tool = Tool(name="schedule_admin.update", description="", schema={"type": "object"}, risk=Risk.WRITE, run=run)
        client = Principal("client", client_id="cl-1", client_name="ci")

        async def inside(arguments: Any) -> ActionLevel:
            return ActionLevel.INSIDE

        async def external(arguments: Any) -> ActionLevel:
            return ActionLevel.EXTERNAL

        assert (await ops.call(client, "s-1", tool.name, {}, tool=tool, level_of=inside))["status"] == "ok"
        blocked = await ops.call(client, "s-1", tool.name, {}, tool=tool, level_of=external)
        assert blocked["error"]["code"] == "blocked"
        assert json.loads(json.dumps(blocked))["error"]["message"].endswith("another way.")

    run_db(test, monkeypatch, setup=_setup)


@pytest.mark.parametrize("action", ["update", "run", "confirm_target"])
def test_a_v1_stored_external_target_is_reviewed_and_linked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    from looper.agents import outbound_hook

    async def test(pool: Any) -> None:
        await _schedule(pool, tmp_path)
        (retargeted,) = await _chat(tmp_path).say("发到合作方群", _retarget(OUTSIDE))
        assert retargeted["status"] == "ok", retargeted
        schedule_id = await pool.fetchval("SELECT id FROM schedules WHERE name = 'daily'")
        await outbound_hook.set_enabled(True)
        arguments = {"schedule_id": schedule_id}
        if action == "update":
            arguments["fields"] = {"requirement": "Summarize the week."}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(f"schedule_admin.{action}", arguments, client=True)
        assert status == 403 and answer["error"]["code"] == "blocked", answer
        review = await pool.fetchrow("SELECT target_kind, target, manager_action_id FROM outbound_reviews")
        assert (review["target_kind"], review["target"]) == ("notify_target", "lark:oc_partners")
        trail = await pool.fetchrow("SELECT tool, error_code FROM manager_actions WHERE id = $1",
                                    review["manager_action_id"])
        assert tuple(trail) == (f"schedule_admin.{action}", "blocked")

    run_db(test, monkeypatch, setup=_setup)
