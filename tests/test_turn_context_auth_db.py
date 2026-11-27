"""Authorization follows the calling speaker, independently of the chat's content audience."""

import json

import pytest

from looper.agents.ops import Risk, Tool, Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.turn_context import SpeakerKind
from looper.db import connection
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def make_ops(ran):
    async def run(call):
        ran.append(call.turn.message_id)
        return {"done": True}

    async def escalate(call):
        return "Delete the synthetic item?"

    return ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1",
                      tools=(Tool("t_delete", "delete", {"type": "object", "properties": {}}, Risk.WRITE,
                                  run, escalate=escalate),))


@pytest.mark.parametrize("speaker", list(SpeakerKind))
def test_escalation_checks_the_speaker_before_registering_any_confirmation(monkeypatch, speaker):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', 'true')")
        ran = []
        ops = make_ops(ran)
        turn = Turn(channel="lark", chat="group", message_id="m1", group=True, speaker=speaker)
        ops.begin_turn(turn)
        answer = await ops.call("t_delete", {})
        action = await pool.fetchrow("SELECT status, error_code, result FROM manager_actions")
        if speaker in (SpeakerKind.ALLOWED, SpeakerKind.MEMBER):
            assert answer["error"]["code"] == "forbidden"
            assert action["error_code"] == "forbidden"
            assert json.loads(action["result"]) == {"reason": "operator_only"}
            assert not ops.asks_in("m1") and not turn.blocks
            assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 0
        else:
            assert answer["status"] == "needs_confirmation"
            assert action["status"] == "needs_confirmation"
            assert ops.asks_in("m1") and turn.blocks
            assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 1
        assert ran == []

    run_db(check, monkeypatch)


@pytest.mark.parametrize("speaker", [SpeakerKind.MEMBER, SpeakerKind.OPERATOR, SpeakerKind.CLIENT])
def test_confirmation_checks_the_current_speaker_and_drops_a_refused_pending(monkeypatch, speaker):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ran = []
        ops = make_ops(ran)
        asking = Turn(channel="lark", chat="group", message_id="m1", speaker=SpeakerKind.OPERATOR)
        ops.begin_turn(asking)
        asked = await ops.call("t_delete", {})
        ops.end_turn(asking)
        confirming = Turn(channel="lark", chat="group", message_id="m2", speaker=speaker)
        assert ops.begin_turn(confirming)
        answer = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})
        if speaker is SpeakerKind.MEMBER:
            assert answer["error"]["code"] == "forbidden"
            assert ran == []
            action = await pool.fetchrow("SELECT error_code, result FROM manager_actions ORDER BY id DESC LIMIT 1")
            assert action["error_code"] == "forbidden"
            assert json.loads(action["result"]) == {"reason": "operator_only"}
        else:
            assert answer == {"status": "ok", "result": {"done": True}}
            assert ran == ["m2"]
        assert not ops.asks_in("m1")

    run_db(check, monkeypatch)


def test_missing_operator_id_explains_why_authorization_is_refused(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops([])
        ops.begin_turn(Turn(channel="lark", chat="direct", message_id="m", speaker=SpeakerKind.ALLOWED,
                            operator_id_missing=True))
        answer = await ops.call("t_delete", {})
        assert answer["error"]["code"] == "forbidden"
        assert answer["error"]["message"].endswith(" (this bot has no operator id set)")

    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel,client_id,allowed", [
    ("lark", None, False), ("telegram", None, False), ("lark_bot", None, False),
    ("web", None, True), ("web", "client-synthetic", True)])
def test_unknown_speaker_is_refused_on_a_bot_channel_only(monkeypatch, channel, client_id, allowed):
    """A path that forgot to classify the speaker fails closed on a bot channel; the dashboard and API clients
    keep their own rules."""
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ran = []
        ops = make_ops(ran)
        asking = Turn(channel=channel, chat="group", message_id="m1", client_id=client_id)
        assert asking.speaker is None
        ops.begin_turn(asking)
        asked = await ops.call("t_delete", {})
        if not allowed:
            assert asked["error"]["code"] == "forbidden" and not ops._pending
            return
        assert asked["status"] == "needs_confirmation"
        ops.end_turn(asking)
        confirming = Turn(channel=channel, chat="group", message_id="m2", client_id=client_id)
        assert ops.begin_turn(confirming)
        assert await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}) == {
            "status": "ok", "result": {"done": True}}
        assert ran == ["m2"]

    run_db(check, monkeypatch)
