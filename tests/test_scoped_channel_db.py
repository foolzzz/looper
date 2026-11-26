"""A scoped message through the native CLI transport and the real Manager bus."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID, OPEN_ID
from looper.agents import chat_scopes
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from tests.db_support import DSN, run_db, until
from tests.test_manager_session_routing_db import GROUP_A, LARK, _Resident, _lark
from tests.test_scenario_session_routing_db import _scenario, _to

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_native_cli_group_message_runs_in_a_scoped_session(monkeypatch, tmp_path: Path,
                                                         lark_cli_stand_in: Path) -> None:
    world_file = tmp_path / "world.json"
    world = json.loads(world_file.read_text())
    world["events"] = [{"type": "im.message.receive_v1", "chat_id": GROUP_A, "chat_type": "group",
                        "message_id": "om_synthetic_scoped", "message_type": "text", "sender_id": OPEN_ID,
                        "sender_type": "user", "content": "@HelperBot synthetic scoped question",
                        "mentions": [BOT_MENTION]}]
    world_file.write_text(json.dumps(world))

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]

    async def body(pool) -> None:
        rig = await _scenario(pool, tmp_path, "scoped")
        message = _to(rig, _lark("synthetic scoped question", GROUP_A, OPEN_ID, group=True))
        session = await rig.manager._router.target(message)
        principals = []

        async def observe(on_event) -> None:
            principals.append(rig.manager._session_now().running_token.ops_turn.principal)

        resident = _Resident("synthetic-scoped")
        resident.on_turn, resident.reply_text = observe, "synthetic scoped reply"
        session.claude = resident
        adapter = LarkChannelAdapter(allowlist=SenderAllowlist(LARK.agent_id, frozenset({OPEN_ID})),
                                     runtime_root=tmp_path, bot_open_id=BOT_OPEN_ID,
                                     manager_id=rig.manager.agent_id)
        await rig.start()
        adapter._bus, adapter._running = rig.bus, True
        adapter._outbound_task = asyncio.create_task(adapter._outbound_loop())
        try:
            await asyncio.wait_for(adapter._run_event_consumer(), 10)
            await until(lambda: any("+messages-reply" in call and "synthetic scoped reply" in call
                                   for call in calls()), timeout=20)
            row = await pool.fetchrow("SELECT * FROM manager_sessions WHERE manager_id = $1 AND session_key = $2",
                                      rig.manager.agent_id, session.key)
            assert row["scope_id"] == chat_scopes.CHAT_ONLY and row["audience"] == "shared"
            assert session.key.endswith("~" + row["scope_fingerprint"])
            assert len(resident.turns) == 1 and "synthetic scoped question" in resident.turns[0]
            assert not rig.main.turns
            assert principals == [chat_scopes.Principal(chat_scopes.ScopeAudience.SHARED,
                                  chat_scopes.PrincipalKind.PERSON, chat_scopes.CHAT_ONLY, 1)]
            recorded = calls()
            assert ["event", "consume", "im.message.receive_v1", "--as", "bot"] in recorded
            reply = next(call for call in recorded if "+messages-reply" in call and "synthetic scoped reply" in call)
            assert reply[reply.index("--message-id") + 1] == "om_synthetic_scoped"
        finally:
            await adapter.stop()
            await rig.close()

    run_db(body, monkeypatch, timeout=45)
