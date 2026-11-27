"""Every Manager action row records who started the turn it ran in: the turn's trusted sender and audience class,
never model text; a confirmed call records both who asked and who confirmed."""

import asyncio

import pytest

from looper.agents import card_actions
from looper.agents.ops import Risk, Tool, Turn, TurnOrigin
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.turn_context import SpeakerKind
from looper.db import connection
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

COLUMNS = "tool, status, sender_id, sender_kind, client_id, initiator_sender_id, initiator_kind"


def make_ops():
    async def run(call):
        return {"done": True}

    async def escalate(call):
        return "Delete the synthetic item?"

    return ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1", token="test-token", tools=(
        Tool("t_write", "write", {"type": "object", "properties": {}}, Risk.WRITE, run),
        Tool("t_read", "read", {"type": "object", "properties": {}}, Risk.READ, run),
        Tool("t_delete", "delete", {"type": "object", "properties": {}}, Risk.WRITE, run, escalate=escalate)))


async def rows(pool):
    return [tuple(row) for row in await pool.fetch(f"SELECT {COLUMNS} FROM manager_actions ORDER BY id")]


def test_an_operator_turn_records_the_operator_and_its_sender_id(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        ops.begin_turn(Turn(channel="lark", chat="dm", message_id="m1", sender="ou_op", speaker=SpeakerKind.OPERATOR))
        assert (await ops.call("t_write", {}))["status"] == "ok"
        assert await rows(pool) == [("t_write", "ok", "ou_op", "operator", None, None, None)]

    run_db(check, monkeypatch)


def test_an_allowlisted_non_operator_records_allowed_and_its_sender_id(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        ops.begin_turn(Turn(channel="lark", chat="group", message_id="m1", sender="ou_ally", group=True,
                            speaker=SpeakerKind.ALLOWED))
        assert (await ops.call("t_write", {}))["status"] == "ok"
        # A refusal of the same speaker is attributed too.
        assert (await ops.call("t_delete", {}))["status"] == "error"
        assert await rows(pool) == [("t_write", "ok", "ou_ally", "allowed", None, None, None),
                                    ("t_delete", "error", "ou_ally", "allowed", None, None, None)]

    run_db(check, monkeypatch)


def test_an_api_client_turn_records_client_and_its_client_id(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        ops.begin_turn(Turn(channel="api", chat="c1", message_id="m1", client_id="cl_1", client_name="ci"))
        await ops.call("t_write", {})
        assert await rows(pool) == [("t_write", "ok", None, "client", "cl_1", None, None)]

    run_db(check, monkeypatch)


def test_an_autonomous_turn_records_autonomous_with_a_null_sender(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        ops.begin_turn(Turn(channel="lark", chat="dm", sender="ou_old", origin=TurnOrigin.COMPLETION))
        await ops.call("t_read", {})
        assert await rows(pool) == [("t_read", "ok", None, "autonomous", None, None, None)]

    run_db(check, monkeypatch)


def test_a_turn_without_a_classification_keeps_the_kind_null(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        ops.begin_turn(Turn(channel="lark", chat="dm", message_id="m1", sender="ou_x"))
        await ops.call("t_write", {})
        assert await rows(pool) == [("t_write", "ok", "ou_x", None, None, None, None)]

    run_db(check, monkeypatch)


def test_a_typed_confirmation_records_the_initiator_and_the_confirmer(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        asking = Turn(channel="lark", chat="dm", message_id="m1", sender="ou_op", speaker=SpeakerKind.OPERATOR)
        ops.begin_turn(asking)
        asked = await ops.call("t_delete", {})
        ops.end_turn(asking)
        ops.begin_turn(Turn(channel="lark", chat="dm", message_id="m2", sender="ou_op", speaker=SpeakerKind.OPERATOR))
        assert (await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))["status"] == "ok"
        assert await rows(pool) == [
            ("t_delete", "needs_confirmation", "ou_op", "operator", None, None, None),
            ("t_delete", "ok", "ou_op", "operator", None, "ou_op", "operator")]

    run_db(check, monkeypatch)


def test_a_button_confirmation_records_the_presser_as_the_confirmer(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        from looper.db import system_settings

        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        ops = make_ops()
        monkeypatch.setitem(card_actions._handlers, "confirmation", ops.press)
        asking = Turn(channel="lark_bot", chat="oc_dm", message_id="om_ask", sender="ou_op",
                      channel_id="channel-lark_bot", speaker=SpeakerKind.OPERATOR)
        ops.begin_turn(asking)
        await ops.call("t_delete", {})
        ops.end_turn(asking)
        (card,) = asking.cards
        value = next(b["value"] for b in card["buttons"] if b["value"].endswith(":confirm"))
        answer = await card_actions.press(card_actions.Press("channel-lark_bot", "ou_op", value, chat="oc_dm"))
        await answer.later
        assert await rows(pool) == [
            ("t_delete", "needs_confirmation", "ou_op", "operator", None, None, None),
            ("t_delete", "ok", "ou_op", "operator", None, "ou_op", "operator")]

    run_db(check, monkeypatch)


def test_a_button_after_a_restart_keeps_the_initiator_and_never_records_autonomous(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        from looper.db import system_settings

        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        first = make_ops()
        asking = Turn(channel="lark_bot", chat="oc_dm", message_id="om_ask", sender="ou_op",
                      channel_id="channel-lark_bot", speaker=SpeakerKind.OPERATOR)
        first.begin_turn(asking)
        await first.call("t_delete", {})
        first.end_turn(asking)
        (card,) = asking.cards
        value = next(b["value"] for b in card["buttons"] if b["value"].endswith(":confirm"))
        second = make_ops()  # a restarted service: the asking turn comes back from the card's row
        assert await second.restore_confirmations() == 1
        monkeypatch.setitem(card_actions._handlers, "confirmation", second.press)
        await (await card_actions.press(card_actions.Press("channel-lark_bot", "ou_op", value, chat="oc_dm"))).later
        assert (await rows(pool))[-1] == ("t_delete", "ok", "ou_op", "operator", None, "ou_op", "operator")

    run_db(check, monkeypatch)


def test_refusal_rows_name_the_sender_too(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        ops = make_ops()
        speaker = Turn(channel="lark", chat="group", message_id="m1", sender="ou_ally", group=True,
                       speaker=SpeakerKind.ALLOWED)
        await ops._out_of_scope("t_write", {"a": 1}, speaker, "key")
        client = Turn(channel="api", chat="c1", message_id="m2", client_id="cl_1", client_name="ci")
        await ops._client_refusal("t_write", {"a": 1}, client)
        assert await rows(pool) == [("t_write", "error", "ou_ally", "allowed", None, None, None),
                                    ("t_write", "error", None, "client", "cl_1", None, None)]

    run_db(check, monkeypatch)


def test_a_rest_call_and_its_confirmation_record_the_client_kind(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        from looper.agents.api_v1 import Principal
        from looper.agents.ops.clients import ClientOps

        async def run(call):
            return {"done": True}

        async def escalate(call):
            return "Delete the synthetic item?"

        write = Tool("t_write", "write", {"type": "object", "properties": {}}, Risk.WRITE, run)
        delete = Tool("t_delete", "delete", {"type": "object", "properties": {}}, Risk.WRITE, run, escalate=escalate)
        ops = ClientOps(Platform(bus=None, task_router=None), tools=(write, delete))
        client = Principal("client", client_id="cl_1", client_name="ci")
        assert (await ops.call(client, "s1", "t_write", {}))["status"] == "ok"
        asked = await ops.call(client, "s1", "t_delete", {})
        assert (await ops.confirm(client, "s1", asked["confirmation_id"]))["status"] == "ok"
        assert await rows(pool) == [
            ("t_write", "ok", None, "client", "cl_1", None, None),
            ("t_delete", "needs_confirmation", None, "client", "cl_1", None, None),
            ("t_delete", "ok", None, "client", "cl_1", None, "client")]

    run_db(check, monkeypatch)


@pytest.mark.parametrize("enabled", [False, True], ids=["switch_off", "switch_on"])
def test_a_routed_session_of_the_manager_records_the_speaker_of_each_message(monkeypatch, tmp_path, enabled):
    """Through the real Manager path, in a routed non-main session (which the turn-context step used to skip)."""
    from looper.agents import session_routing
    from tests.lark_cli_stand_in import OPEN_ID, OTHER
    from tests.test_manager_session_routing_db import _Resident
    from tests.test_manager_turn_context_db import _routed_lark, rig_with_ops, seed

    async def check(pool):
        await seed(pool, enabled=enabled)
        write = Tool("t_write", "write", {"type": "object", "properties": {}}, Risk.WRITE,
                     lambda call: {"done": True})
        rig = rig_with_ops(tmp_path, tools=(write,))
        ops = rig.manager.ops
        await rig.start()
        monkeypatch.setattr(session_routing, "_routing_enabled", True)
        monkeypatch.setattr(session_routing, "_max_resident", 2)
        resident = rig.group("oc_synthetic_context", _Resident("sess-group"))
        answers = []

        async def calls(on_event):
            # The turn the routed session runs in, as its tools see it (a routed session lists none of the
            # Manager's own tools, so its row is written for that turn directly).
            await ops._record(rig.manager._running_token.ops_turn, write, {}, Risk.WRITE, "ok")
            answers.append(1)

        resident.on_turn = calls
        try:
            for sender in (OTHER, "ou_syntheticMember", OPEN_ID):
                envelope, _ = _routed_lark(sender)
                await rig.bus.publish(envelope)
                await rig.reply(envelope)
            assert len(answers) == 3
            assert await rows(pool) == [
                ("t_write", "ok", OTHER, "allowed", None, None, None),
                ("t_write", "ok", "ou_syntheticMember", "member", None, None, None),
                ("t_write", "ok", OPEN_ID, "operator", None, None, None)]
        finally:
            await rig.close()

    run_db(check, monkeypatch, timeout=60)


def test_the_migration_is_pending_before_apply_and_leaves_old_rows_alone():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE manager_actions DROP COLUMN sender_id, DROP COLUMN sender_kind, "
                               "DROP COLUMN initiator_sender_id, DROP COLUMN initiator_kind")
            await conn.execute("DELETE FROM schema_migrations WHERE name IN "
                               "('manager_action_sender', 'manager_action_sender_online')")
            await conn.execute("INSERT INTO manager_actions (tool, risk, status) VALUES ('t', 'write', 'ok')")
            ctid = await conn.fetchval("SELECT ctid::text FROM manager_actions")
            assert await connection.schema_change_pending(conn)
            async with conn.transaction():
                await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            # No rewrite: the old row is the same tuple, with NULLs, and the checks are validated.
            assert await conn.fetchval("SELECT ctid::text FROM manager_actions") == ctid
            assert tuple(await conn.fetchrow(
                "SELECT sender_id, sender_kind, initiator_sender_id, initiator_kind FROM manager_actions")) == (None,) * 4
            assert await conn.fetchval(
                "SELECT bool_and(convalidated) FROM pg_constraint WHERE conrelid = 'manager_actions'::regclass "
                "AND conname IN ('manager_actions_sender_kind_check', 'manager_actions_initiator_kind_check')")

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["operator", "client"])
def test_task_detail_shows_sender_ids_only_to_the_operator(monkeypatch, kind):
    from looper.agents import api_v1, projects
    from looper.agents.v1_domains import task_views

    rows_ = [{"tool": "t", "sender_id": "ou_1", "sender_kind": "allowed", "initiator_sender_id": "ou_2",
              "initiator_kind": "operator"}]

    async def detail(task_id):
        return {"manager_actions": [dict(row) for row in rows_]}

    monkeypatch.setattr(projects, "task_detail", detail)
    principal = api_v1.OPERATOR if kind == "operator" else api_v1.Principal("client", client_id="cl_1")
    operation = api_v1.find("POST", "/api/v1/tasks/detail")
    answer = asyncio.run(task_views.run(None, operation, {"task_id": "t1"}, principal, {}))
    (row,) = answer["manager_actions"]
    if kind == "operator":
        assert row == rows_[0]
    else:  # the kinds say who it was in class terms; the ids are chat users' own, nulled so the shape holds
        assert row == {"tool": "t", "sender_id": None, "sender_kind": "allowed", "initiator_sender_id": None,
                       "initiator_kind": "operator"}
