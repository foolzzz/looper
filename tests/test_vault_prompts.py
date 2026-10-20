"""A disabled knowledge vault leaves the Planner's existing tool guide byte-identical."""

from types import SimpleNamespace

import pytest

from looper.agents.ops.planner import PlannerOps, ROLES_GUIDE, TOOLS_GUIDE
from looper.agents.room import PLANNER_ROOM_GUIDE


@pytest.mark.parametrize("roles", [False, True])
@pytest.mark.parametrize("room", [False, True])
@pytest.mark.parametrize("has_room", [False, True])
def test_vault_off_keeps_the_planner_tool_guide_byte_identical(roles: bool, room: bool, has_room: bool) -> None:
    ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
    if has_room:
        ops.room = SimpleNamespace()
    expected = TOOLS_GUIDE
    if roles:
        expected += "\n" + ROLES_GUIDE
    if room and has_room:
        expected += "\n" + PLANNER_ROOM_GUIDE
    assert ops.guide_for(roles=roles, room=room) == expected
    assert ops.guide_for(roles=roles, room=room, vault="") == expected
