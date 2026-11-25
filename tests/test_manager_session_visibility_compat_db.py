"""Freeze each unchanged main-session input and tool against the pre-allowlist implementation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents import lanes_settings, operator_identity, persona, session_routing
from looper.agents.ops import service
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import GoalSnapshot, ManagerAgent
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_manager_session_allowlist_db import MAIN_CHAT, SENTINELS, _operator, _seed
from tests.test_manager_session_routing_db import _Rig, _lark, _published

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
FIXTURE = Path(__file__).parent / "fixtures" / "manager_session_visibility_compat.json"


def _tool_digest(tool: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("routing,kind", [(True, "operator"), (False, "operator"),
                                          (False, "group"), (False, "other_dm")])
def test_main_and_routing_off_keep_each_input_and_tool_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, routing: bool, kind: str,
) -> None:
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][f"{str(routing).lower()}:{kind}"]

    async def test(pool: Any) -> None:
        await _seed(pool, tmp_path)
        monkeypatch.setattr(lanes_settings, "_enabled", True)
        monkeypatch.setattr(session_routing, "_routing_enabled", routing)
        monkeypatch.setattr(operator_identity, "operator_sender_id", _operator)
        monkeypatch.setattr(service, "operator_sender_id", _operator)
        monkeypatch.setattr(persona, "_current", persona.Persona(name="Synthetic Manager", tone="tone", notes="notes"))
        rig = _Rig(tmp_path)
        rig.manager._goal_snapshots["synthetic-goal"] = GoalSnapshot("synthetic-goal", title=SENTINELS["goal"])
        ops = ManagerOps(Platform(bus=None, task_router=None, manager=rig.manager),
                         url="http://127.0.0.1:9/", token="synthetic")
        ops._working_at = ops._clock()
        rig.manager.ops = ops
        captures: list[dict[str, Any]] = []
        prepare = ManagerAgent._prepare_turn

        async def capture(self: Any, *args: Any, **kwargs: Any) -> Any:
            prepared = await prepare(self, *args, **kwargs)
            if prepared.signature is not None:
                captures.append({"prompt": prepared.system_prompt.text, "layers": prepared.system_prompt.layers,
                                 "sections": await self._sections(prepared.envelope), "turn": prepared.turn_text,
                                 "tools": await self.ops.current_listing(prepared.turn),
                                 "state": list(prepared.state) if prepared.state is not None else None})
            return prepared

        monkeypatch.setattr(ManagerAgent, "_prepare_turn", capture)
        await rig.start()
        try:
            sender = OPEN_ID if kind == "operator" else OTHER
            question = _lark("synthetic comparison message", MAIN_CHAT, sender, group=kind == "group")
            await rig.reply(await _published(rig, question))
            assert len(captures) == len(rig.main.turns) == 1
            actual = captures[0]
            actual["actual_input"] = rig.main.turns[0]
        finally:
            await rig.close()
        assert actual["prompt"] == baseline["prompt"]
        assert actual["layers"].keys() == baseline["layers"].keys()
        for layer, value in actual["layers"].items():
            assert value == baseline["layers"][layer], layer
        for field in ("sections", "state"):
            assert len(actual[field]) == len(baseline[field]), field
            for index, value in enumerate(actual[field]):
                assert value == baseline[field][index], (field, index)
        for field in ("turn", "actual_input"):
            assert actual[field] == baseline[field], field
        assert [tool["name"] for tool in actual["tools"]] == baseline["tool_order"]
        for tool in actual["tools"]:
            assert _tool_digest(tool) == baseline["tools"][tool["name"]], tool["name"]

    run_db(test, monkeypatch, timeout=90)
