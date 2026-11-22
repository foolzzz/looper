"""The level of a call on a stored schedule (``ops/levels.py``, ``schedule_writes``) on a real PostgreSQL: enabling,
running or changing a schedule reaches wherever the schedule does, its stored command and notify target merged
with the change; one Looper cannot find is ``unknown``.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.action_levels import ActionLevel
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PARTNERS = {"channel": "lark", "chat": "oc_partners"}


@pytest.fixture(autouse=True)
def _targets(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import operator_identity, schedules

    async def nobody(channel_id: str) -> None:
        return None

    async def offered(**kwargs: Any) -> list[dict[str, str]]:
        return [PARTNERS]

    monkeypatch.setattr(operator_identity, "operator_sender_id", nobody)
    monkeypatch.setattr(schedules, "notify_targets", offered)


async def _stored(pool: Any) -> tuple[str, str]:
    from looper.agents import schedules
    from looper.db import projects

    project = await projects.create_project(name="calc", work_dir="/w", pool=pool)
    agent = await schedules.create_schedule(project["id"], {"name": "daily", "cron": "0 9 * * *",
                                                            "requirement": "Summarize.", "notify_target": PARTNERS},
                                            created_via="manager")
    command = await schedules.create_schedule(project["id"], {"name": "backup", "cron": "0 3 * * *", "kind": "command",
                                                              "command": "make backup"}, created_via="manager")
    return agent["schedule"]["schedule_id"], command["schedule"]["schedule_id"]


def test_a_call_on_a_stored_schedule_reaches_where_the_schedule_does(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import Turn
    from looper.agents.ops import levels as tool_levels
    from looper.agents.ops.service import MANAGER_TOOLS

    tool = {t.name: t for t in MANAGER_TOOLS}["looper_schedules"]
    turn = Turn(channel="web", chat="s-1", message_id="m-1")

    async def level(**arguments: Any) -> ActionLevel:
        return await tool_levels.level_of(tool, {"project": "calc", **arguments}, turn)

    async def test(pool: Any) -> None:
        await _stored(pool)
        assert await level(action="enable", schedule="daily") is ActionLevel.EXTERNAL
        assert await level(action="run", schedule="daily") is ActionLevel.EXTERNAL
        assert await level(action="update", schedule="daily", fields={"cron": "0 10 * * *"}) is ActionLevel.EXTERNAL
        assert await level(action="update", schedule="daily", fields={"notify_target": None}) is ActionLevel.OPERATOR
        assert await level(action="enable", schedule="backup") is ActionLevel.MACHINE
        assert await level(action="update", schedule="backup", fields={"notify_target": PARTNERS}) is \
            ActionLevel.EXTERNAL
        assert await level(action="enable", schedule="nonesuch") is ActionLevel.UNKNOWN
        assert await level(action="disable", schedule="daily") is ActionLevel.INSIDE

    run_db(test, monkeypatch)


def test_an_operation_on_a_stored_schedule_reaches_where_the_schedule_does(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.v1_domains import _DOMAINS

    ops = {op.operation_id: op for module in _DOMAINS for op in module.OPERATIONS}

    async def test(pool: Any) -> None:
        agent, command = await _stored(pool)
        assert await ops["schedule_admin.run"].level_of({"schedule_id": agent}) is ActionLevel.EXTERNAL
        assert await ops["schedule_admin.run"].level_of({"schedule_id": command}) is ActionLevel.MACHINE
        assert await ops["schedule_admin.update"].level_of({"schedule_id": agent, "fields": {"cron": "0 8 * * *"}}) \
            is ActionLevel.EXTERNAL
        assert await ops["schedule_admin.run"].level_of({"schedule_id": "sch-missing"}) is ActionLevel.UNKNOWN

    run_db(test, monkeypatch)
