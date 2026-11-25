"""One-time reminders on PostgreSQL (reminders RFC): the default Manager's three tools, only while
``reminders.enabled`` is on and only in the user's own turns; the delivery by code at the time, once, with the
catch-up after downtime; and the upgrade of a database without the table.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings, proactive_delivery as delivery, reminders
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.managers_api import ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from looper.agents.ops import Turn
from looper.agents.ops import reminders as ops_reminders
from looper.agents.ops.service import MANAGER_TOOLS, ManagerOps, Platform, scope_for
from looper.db import connection, proactive_deliveries as deliveries, reminders as store
from tests.db_support import DSN, dropped_tables, run_db, run_with_channel_configs
from tests.test_manager_tools_db import _converse

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

UTC = timezone.utc
OP = "ou_operatorsynthetic"
ZONE = "America/Los_Angeles"
NAMES = list(ops_reminders.TOOL_NAMES)


async def _switch(pool: Any, on: bool = True) -> None:
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('reminders.enabled', $1) "
                       "ON CONFLICT (key) DO UPDATE SET value = $1", "true" if on else "false")


async def _operator(pool: Any, *, enabled: bool = False, channel_id: str = "channel-lark", who: str = OP) -> None:
    """``who`` as the operator of ``channel_id`` (the built-in Lark bot by default)."""
    await pool.execute("UPDATE channel_configs SET enabled = $2, config = $3 WHERE channel_id = $1", channel_id,
                       enabled, json.dumps({"allowed_open_ids": [who], "operator_sender_id": who}))


def _ops(scope: Any = None) -> ManagerOps:
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                     **({"scope": scope} if scope is not None else {}))
    ops._working_at = ops._clock()
    return ops


def _web(message_id: str = "m-web", **fields: Any) -> Turn:
    return Turn(channel="web", chat="chat-1", message_id=message_id, **fields)


def _dm(message_id: str = "m-dm", *, sender: str = OP, group: bool = False) -> Turn:
    return Turn(channel="lark", chat="oc_group" if group else "oc_dm", message_id=message_id, sender=sender,
                group=group, channel_id="channel-lark")


async def _call(ops: ManagerOps, turn: Turn, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A call in ``turn``, carrying its binding as the MCP bridge does."""
    binding = f"b-{turn.message_id}-{id(turn)}"
    ops.begin_turn(turn, binding=binding)
    try:
        return await ops.call(name, arguments, binding=binding)
    finally:
        ops.end_turn(turn)


def _names(listing: list[dict[str, Any]]) -> list[str]:
    return [tool["name"] for tool in listing]


class _Channel:
    """The channel registry's ``post``: records each post and answers with the next of ``outcomes`` (sent after)."""

    def __init__(self, *outcomes: PostOutcome, delay: float = 0.0) -> None:
        self.outcomes, self.delay, self.posts = list(outcomes), delay, []

    async def post(self, channel_id: str, target: Any, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None, proactive: Any = None) -> PostReceipt:
        self.posts.append({"channel_id": channel_id, "kind": target.kind.value, "to": target.id,
                           "group": target.group, "text": text, "key": idempotency_key})
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes.pop(0) if self.outcomes else PostOutcome.SENT
        if outcome is PostOutcome.SENT:
            return PostReceipt(True, external_ids=(f"om_{len(self.posts)}",))
        return PostReceipt(False, error=f"the channel answered {outcome.value}", outcome=outcome)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _stored(text: str, due_at: datetime, *, chat: str = OP, channel_id: str = "channel-lark") -> int:
    row = await store.create(text=text, due_at=due_at, timezone=ZONE, channel_id=channel_id, chat=chat,
                             created_channel="lark", created_message_id="m-0")
    return row["id"]


async def _statuses(pool: Any) -> dict[int, tuple[str, int, str | None]]:
    return {row["id"]: (row["status"], row["attempts"], row["detail"])
            for row in await pool.fetch("SELECT id, status, attempts, detail FROM reminders ORDER BY id")}


def _prepared(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every turn the Manager prepares to run (its system prompt and its process signature)."""
    from looper.agents.tiers.manager import ManagerAgent

    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
    return prepared


def test_off_the_manager_is_launched_as_before_and_on_only_its_tool_listing_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        prepared = _prepared(monkeypatch)
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'reminders.%'") == 0
        off = await _converse(tmp_path / "off", [[("looper_overview", {})]], ["hi"])
        await _switch(pool)
        on = await _converse(tmp_path / "on", [[("looper_overview", {})]], ["hi"])
        before, after = prepared

        # The listing: the catalog as it is; on, the three reminder tools are the ones added.
        assert off.tools == [tool.name for tool in MANAGER_TOOLS]
        assert on.tools == [*off.tools, *NAMES]
        # The system prompt and the command line are the same either way: the tools explain themselves.
        assert after.system_prompt.text == before.system_prompt.text
        assert after.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT"
        def same(command: Any, root: str) -> list[str]:  # each Manager ran in its own directory
            # and its MCP entry names its own port.
            return [re.sub(r"\.local/mcp/[0-9a-f]+\.json$", "<mcp>", str(part).replace(str(tmp_path / root), "<root>"))
                    for part in command]

        assert same(after.signature[0], "on") == same(before.signature[0], "off")
        assert "reminder" not in " ".join(before.signature[0]) + before.system_prompt.text
        # The process signature gains only the listing's hash (so a group turn's listing restarts the process).
        workflows = any(tool.name == "looper_task_create" and "workflow" in tool.schema.get("properties", {})
                        for tool in MANAGER_TOOLS)
        assert len(before.signature) == 6 + workflows
        assert len(after.signature) == 7 and after.signature[1] == before.signature[1]

    run_db(test, monkeypatch, timeout=60)


def test_only_the_default_manager_has_them_only_while_on_and_only_in_the_users_own_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool)
        ops = _ops()
        assert not set(NAMES) & set(_names(await ops.current_listing(_web())))
        await _switch(pool)
        assert _names(await ops.current_listing(_web()))[-3:] == NAMES
        assert _names(await ops.current_listing(_dm()))[-3:] == NAMES
        for turn in (_dm(group=True), _dm(sender="ou_someoneelse"), _web(client_id="client-1")):
            assert not set(NAMES) & set(_names(await ops.current_listing(turn)))
            for name, arguments in ((ops_reminders.CREATE, {"text": "看 PR", "in_minutes": 5}),
                                    (ops_reminders.LIST, {}), (ops_reminders.CANCEL, {"id": 1})):
                answer = await _call(ops, turn, name, arguments)
                assert answer["status"] == "error" and answer["error"] == {
                    "code": "forbidden",
                    "message": f"{name} runs only in the user's own direct chat or on the dashboard"}
        scenario = ManagerDefinition(
            id="manager-desk", name="Desk", description="", system_prompt="p", profile_id=None,
            tools=ManagerTools.ALL, task_scope=TaskScope.ALL, task_policy=TaskPolicy.FREE, enabled=True,
            builtin=False, revision=-1)
        desk = _ops(scope_for(scenario))
        assert not set(NAMES) & set(_names(await desk.current_listing(_web())))
        assert (await _call(desk, _web(), ops_reminders.LIST, {}))["error"]["code"] == "forbidden"
        await _switch(pool, on=False)
        answer = await _call(ops, _web(), ops_reminders.CREATE, {"text": "看 PR", "in_minutes": 5})
        assert answer["error"]["message"] == f"this Manager no longer has {ops_reminders.CREATE}"
        assert await pool.fetchval("SELECT count(*) FROM reminders") == 0

    run_with_channel_configs(test, monkeypatch)


def test_a_reminder_set_in_the_users_direct_chat_is_read_back_and_goes_back_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool)
        await _switch(pool)
        turn = _dm()
        answer = await _call(_ops(), turn, ops_reminders.CREATE,
                             {"text": "  看 PR  ", "at": "2099-01-02 15:00", "timezone": ZONE})
        assert answer["status"] == "error" and "at most a year ahead" in answer["error"]["message"]
        due = datetime.now(UTC).astimezone(reminders.cron.zone(ZONE)) + timedelta(days=2)
        at = f"{due:%Y-%m-%d} 15:00"
        answer = await _call(_ops(), turn, ops_reminders.CREATE, {"text": "  看 PR  ", "at": at, "timezone": ZONE})
        assert answer["status"] == "ok", answer
        stored = answer["result"]["reminder"]
        assert stored["text"] == "看 PR" and stored["due"] == at and stored["timezone"] == ZONE
        assert stored["status"] == "pending" and "do not repeat it" in answer["result"]["shown_to_user"]
        row = await pool.fetchrow("SELECT * FROM reminders")
        assert (row["channel_id"], row["chat"], row["created_channel"], row["created_message_id"]) == (
            "channel-lark", OP, "lark", "m-dm")
        assert turn.blocks == [f"已记下提醒 #{row['id']}：{reminders.when(row['due_at'], ZONE)}（{ZONE}），"
                               "到时发到这个私聊：看 PR"]

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize(("now", "at", "due", "note"), [
    (datetime(2026, 11, 1, 8, 45, tzinfo=UTC), "01:30", datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
     "11-01 01:30 在 America/Los_Angeles 有两次（夏令时结束），记的是第二次（PST）"),
    (datetime(2027, 3, 14, 9, 0, tzinfo=UTC), "02:30", datetime(2027, 3, 14, 10, 0, tzinfo=UTC),
     "03-14 02:30 在 America/Los_Angeles 不存在（夏令时开始），改为 03-14 03:00"),
])
def test_a_clock_change_that_chose_or_moved_the_time_is_told_in_the_confirmation(
    monkeypatch: pytest.MonkeyPatch, now: datetime, at: str, due: datetime, note: str,
) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool)
        await _switch(pool)
        monkeypatch.setattr(ops_reminders, "_now", lambda: now)
        turn = _dm()
        answer = await _call(_ops(), turn, ops_reminders.CREATE, {"text": "看 PR", "at": at, "timezone": ZONE})
        assert answer["status"] == "ok", answer
        assert answer["result"]["clock_change"] == note
        assert await pool.fetchval("SELECT due_at FROM reminders") == due
        assert turn.blocks[0].endswith(f"到时发到这个私聊：看 PR\n注意：{note}。")

    run_with_channel_configs(test, monkeypatch)


def test_a_reminder_set_on_the_dashboard_goes_to_the_users_direct_chat_with_a_bot_that_names_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _switch(pool)
        answer = await _call(_ops(), _web(), ops_reminders.CREATE, {"text": "看 PR", "in_minutes": 30})
        assert answer["error"]["code"] == "conflict" and "Settings → Channels" in answer["error"]["message"]
        await _operator(pool, enabled=False)  # a bot that is off is not one to send through
        assert (await _call(_ops(), _web(), ops_reminders.CREATE, {"text": "看 PR", "in_minutes": 30})
                )["error"]["code"] == "conflict"
        await _operator(pool, enabled=True)
        turn = _web()
        answer = await _call(_ops(), turn, ops_reminders.CREATE, {"text": "看 PR", "in_minutes": 30})
        assert answer["status"] == "ok", answer
        row = await pool.fetchrow("SELECT channel_id, chat, created_channel FROM reminders")
        assert tuple(row) == ("channel-lark", OP, "web")
        assert turn.blocks[0].endswith("到时发到你的 Lark 私聊：看 PR")

    run_with_channel_configs(test, monkeypatch)


def test_the_user_lists_their_reminders_and_cancels_a_pending_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool)
        await _switch(pool)
        now = datetime.now(UTC)
        later = await _stored("later", now + timedelta(hours=5))
        sooner = await _stored("sooner", now + timedelta(hours=1))
        sent = await _stored("sent", now + timedelta(hours=2))
        await pool.execute("UPDATE reminders SET status = 'delivered', settled_at = now() WHERE id = $1", sent)
        listed = (await _call(_ops(), _dm(), ops_reminders.LIST, {}))["result"]
        assert [r["id"] for r in listed["reminders"]] == [sooner, later] and listed["more"] is False
        assert listed["now"].endswith(reminders.default_timezone())
        everything = (await _call(_ops(), _dm(), ops_reminders.LIST, {"status": "all", "limit": 2}))["result"]
        assert [r["id"] for r in everything["reminders"]] == [later, sent] and everything["more"] is True

        turn = _dm()
        answer = await _call(_ops(), turn, ops_reminders.CANCEL, {"id": sooner})
        assert answer["status"] == "ok" and answer["result"]["reminder"]["status"] == "cancelled"
        assert turn.blocks[0].startswith(f"已取消提醒 #{sooner}（原定 ") and turn.blocks[0].endswith("：sooner")
        for reminder_id, code in ((sooner, "conflict"), (sent, "conflict"), (999, "not_found")):
            answer = await _call(_ops(), _dm(), ops_reminders.CANCEL, {"id": reminder_id})
            assert answer["error"]["code"] == code, answer
        assert (await _statuses(pool))[sent][0] == "delivered"

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("state", ["uncertain", "failed", "missed"])
def test_the_user_cancels_a_terminal_reminder_without_resending_it(monkeypatch, state):
    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime.now(UTC)
        reminder_id = await _stored("Check the application", now - timedelta(hours=2))
        await store.settle(pool, reminder_id, state, now=now, from_status="pending")
        turn = _dm()
        answer = await _call(_ops(), turn, ops_reminders.CANCEL, {"id": reminder_id})
        assert answer["status"] == "ok", answer
        assert answer["result"]["reminder"]["status"] == "cancelled"
        assert f"已取消提醒 #{reminder_id}" in turn.blocks[0]
        channel = _Channel()
        service = reminders.ReminderService(channel, clock=_Clock(now))
        await service.run_once()
        await service.run_once()
        assert channel.posts == []
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    run_with_channel_configs(test, monkeypatch)


def test_a_due_reminder_is_sent_once_by_code_to_the_users_direct_chat_and_survives_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        due = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        reminder_id = await _stored("看 PR", due)
        channel, clock = _Channel(), _Clock(due - timedelta(seconds=5))
        await reminders.ReminderService(channel, clock=clock).run_once()
        assert channel.posts == []  # not yet due
        clock.now = due + timedelta(seconds=4)
        # A new service: a restart between setting it and its time loses nothing.
        await reminders.ReminderService(channel, clock=clock).run_once()
        await reminders.ReminderService(channel, clock=clock).run_once()
        (post,) = channel.posts
        row = await store.get(reminder_id)
        assert post == {"channel_id": "channel-lark", "kind": "user", "to": OP, "group": False,
                        "text": reminders.message_text(row, now=clock.now), "key": f"looper-reminder-{reminder_id}"}
        assert post["text"].endswith("让我提醒你：看 PR") and "补发" not in post["text"]
        assert (row["status"], row["attempts"], row["external_id"]) == ("delivered", 1, "om_1")
        assert row["settled_at"] is not None

    run_with_channel_configs(test, monkeypatch)


def test_after_downtime_only_the_latest_reminder_missed_within_36_hours_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
        oldest = await _stored("oldest", now - timedelta(hours=40))
        earlier = await _stored("earlier", now - timedelta(hours=6))
        latest = await _stored("latest", now - timedelta(hours=2))
        ahead = await _stored("ahead", now + timedelta(hours=1))
        channel = _Channel(PostOutcome.REJECTED)  # the channel is not up yet at the first tick after the restart
        service = reminders.ReminderService(channel, clock=_Clock(now))
        await service.run_once()
        await service.run_once()
        assert [post["text"].split("：", 1)[1] for post in channel.posts] == [
            "latest\n（原定 11-22 02:00，Looper 没能按时发出，现在补发。）"] * 2
        assert await _statuses(pool) == {
            oldest: ("missed", 0, reminders.MISSED_OLD), earlier: ("missed", 0, reminders.MISSED_LATER),
            latest: ("delivered", 2, None), ahead: ("pending", 0, None)}

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("how", ["a restart", "a 61-minute pause of a running service"])
def test_after_a_gap_a_chat_gets_one_message_for_what_it_missed(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """The reviews' case: reminders 60, 9 and 1 minutes overdue in one direct chat when the ticks come back, whether
    the server restarted or a running one slept or stalled for 61 minutes."""

    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
        ids = [await _stored(text, now - timedelta(minutes=minutes))
               for text, minutes in (("sixty", 60), ("nine", 9), ("one", 1))]
        channel, clock = _Channel(), _Clock(now - timedelta(minutes=61))
        service = reminders.ReminderService(channel, clock=clock)
        if how != "a restart":
            await service.run_once()  # the last tick before the pause: nothing was due
        clock.now = now
        await service.run_once()
        assert [post["text"].split("：", 1)[1] for post in channel.posts] == ["one"]  # 1 minute late: no late line
        assert await _statuses(pool) == {ids[0]: ("missed", 0, reminders.MISSED_LATER),
                                         ids[1]: ("missed", 0, reminders.MISSED_LATER),
                                         ids[2]: ("delivered", 1, None)}

    run_with_channel_configs(test, monkeypatch)


def test_while_running_reminders_due_together_are_all_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        both = [await _stored(text, now) for text in ("first", "second")]
        channel, clock = _Channel(), _Clock(now - timedelta(seconds=10))
        service = reminders.ReminderService(channel, clock=clock)
        await service.run_once()  # a tick before they are due
        clock.now = now + timedelta(seconds=1)
        await service.run_once()
        assert sorted(post["text"].split("：", 1)[1] for post in channel.posts) == ["first", "second"]
        assert [status for status, _, _ in (await _statuses(pool)).values()] == ["delivered"] * len(both)
        # Switched off meanwhile: of what came due more than the grace ago, only the newest.
        later = [await _stored(text, now + timedelta(minutes=minutes)) for text, minutes in (("a", 5), ("b", 6))]
        await _switch(pool, on=False)
        clock.now = now + timedelta(minutes=20)
        await service.run_once()
        await _switch(pool)
        await service.run_once()
        statuses = await _statuses(pool)
        assert (statuses[later[0]][0], statuses[later[1]][0]) == ("missed", "delivered")

    run_with_channel_configs(test, monkeypatch)


def test_two_servers_taking_turns_in_an_upgrade_send_everything_just_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old server ticked; two reminders came due; the new one takes the lock first. Neither is missed."""

    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        start = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        both = [await _stored(text, start + timedelta(seconds=seconds)) for text, seconds in (("first", 5), ("second", 6))]
        channel = _Channel()
        old, new = (reminders.ReminderService(channel, clock=_Clock(start)),
                    reminders.ReminderService(channel, clock=_Clock(start + timedelta(seconds=10))))
        await old.run_once()  # nothing due yet
        await new.run_once()  # its first tick
        old._clock = lambda: start + timedelta(seconds=20)
        await old.run_once()
        assert sorted(post["text"].split("：", 1)[1] for post in channel.posts) == ["first", "second"]
        assert [status for status, _, _ in (await _statuses(pool)).values()] == ["delivered"] * len(both)

    run_with_channel_configs(test, monkeypatch)


def test_a_send_cut_off_by_a_stop_is_never_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        reminder_id = await _stored("看 PR", now - timedelta(seconds=30))
        # A server claimed it and stopped before the channel's answer was recorded.
        await pool.execute("UPDATE reminders SET status = 'sending', attempts = 1, post_started_at = now() WHERE id = $1", reminder_id)
        channel = _Channel()
        await reminders.ReminderService(channel, clock=_Clock(now)).run_once()
        assert channel.posts == []
        assert (await _statuses(pool))[reminder_id] == (
            "uncertain", 1, "the sender stopped before recording the answer")

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("layer,point", [(False, "claim"), (True, "claim"), (True, "decision"),
                                        (False, "post"), (True, "post"), (True, "receipt")])
def test_restart_recovers_only_reminders_that_never_started_post(monkeypatch, layer, point):
    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat_settings.set_delivery_enabled(layer)
        now = datetime(2026, 11, 20, 23, tzinfo=UTC)
        reminder_id = await _stored("crash recovery", now)
        key = f"looper-reminder-{reminder_id}"
        channel = _Channel()
        owner, method = {"claim": (store, "claim"), "decision": (delivery, "decide"),
                         "post": (store, "settle"), "receipt": (deliveries, "record_receipt")}[point]
        original = getattr(owner, method)

        async def crash(*args, **kwargs):
            if point in {"claim", "decision"}:
                await original(*args, **kwargs)
            raise asyncio.CancelledError

        with monkeypatch.context() as patch:
            patch.setattr(owner, method, crash)
            with pytest.raises(asyncio.CancelledError):
                await reminders.ReminderService(channel, clock=_Clock(now)).run_once()
        before = await store.get(reminder_id)
        assert before["status"] == ("delivered" if point == "receipt" else "sending")
        assert len(channel.posts) == (1 if point in {"post", "receipt"} else 0)
        decision_before = await deliveries.get(pool, key)
        service = reminders.ReminderService(channel, clock=_Clock(now + timedelta(seconds=1)))
        await service.run_once()
        await service.run_once()
        row = await store.get(reminder_id)
        assert row["status"] == ("uncertain" if point == "post" else "delivered")
        assert row["post_started_at"] is not None
        assert row["attempts"] == (2 if point in {"claim", "decision"} else 1)
        assert len(channel.posts) == 1
        assert channel.posts[0]["key"] == key
        decision = await deliveries.get(pool, key)
        if layer:
            assert decision["outcome"] == ("unknown" if point == "post" else "sent")
            assert decision["sent_at"] is not None
            if decision_before:
                assert decision["id"] == decision_before["id"]
                assert decision["decided_at"] == decision_before["decided_at"]
            assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 1
        else:
            assert decision is None

    run_with_channel_configs(test, monkeypatch)


def test_restart_recovers_many_claims_and_finishes_all_started_decisions(monkeypatch):
    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat_settings.set_delivery_enabled(True)
        now = datetime(2026, 11, 20, 23, tzinfo=UTC)
        ids = [await _stored(f"batch {n}", now) for n in range(120)]
        for n, reminder_id in enumerate(ids):
            await store.claim(pool, reminder_id, now)
            await delivery.decide(
                delivery.Delivery(delivery.DeliveryKind.USER_REMINDER, delivery.Urgency.USER_SET,
                                  delivery.ChatKind.DIRECT),
                dedupe_key=f"looper-reminder-{reminder_id}", channel_id="channel-lark", chat=OP,
                source_ref=str(reminder_id), last_user_message_at=None, now=now)
            if n % 2:
                await pool.execute("UPDATE reminders SET post_started_at=$2 WHERE id=$1", reminder_id, now)
                # A refused previous attempt must not hide an interrupted later attempt.
                await deliveries.record_receipt(f"looper-reminder-{reminder_id}", "rejected")
        channel = _Channel()
        service = reminders.ReminderService(channel, clock=_Clock(now))
        await service.run_once()
        await service.run_once()
        assert len(channel.posts) == 60
        assert {post["key"] for post in channel.posts} == {f"looper-reminder-{rid}" for rid in ids[::2]}
        statuses = await _statuses(pool)
        assert all(statuses[rid][:2] == ("delivered", 2) for rid in ids[::2])
        assert all(statuses[rid][:2] == ("uncertain", 1) for rid in ids[1::2])
        outcomes = await pool.fetch("SELECT outcome, count(*) AS n FROM proactive_deliveries GROUP BY outcome")
        assert {row["outcome"]: row["n"] for row in outcomes} == {"sent": 60, "unknown": 60}

    run_with_channel_configs(test, monkeypatch)


def test_recovery_rolls_back_reminder_state_when_decision_settlement_fails(monkeypatch):
    import asyncpg

    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat_settings.set_delivery_enabled(True)
        now = datetime(2026, 11, 20, 23, tzinfo=UTC)
        reminder_id = await _stored("atomic recovery", now)
        channel = _Channel()

        async def crash(*args, **kwargs):
            raise asyncio.CancelledError

        with monkeypatch.context() as patch:
            patch.setattr(store, "settle", crash)
            with pytest.raises(asyncio.CancelledError):
                await reminders.ReminderService(channel, clock=_Clock(now)).run_once()
        await pool.execute("CREATE FUNCTION refuse_recovery() RETURNS trigger LANGUAGE plpgsql AS $$ "
                           "BEGIN RAISE EXCEPTION 'synthetic receipt failure'; END $$; "
                           "CREATE TRIGGER refuse_recovery BEFORE UPDATE ON proactive_deliveries "
                           "FOR EACH ROW EXECUTE FUNCTION refuse_recovery()")
        service = reminders.ReminderService(channel, clock=_Clock(now))
        with pytest.raises(asyncpg.RaiseError, match="synthetic receipt failure"):
            await service.run_once()
        assert (await store.get(reminder_id))["status"] == "sending"
        assert (await deliveries.get(pool, f"looper-reminder-{reminder_id}"))["outcome"] is None
        await pool.execute("DROP TRIGGER refuse_recovery ON proactive_deliveries")
        await service.run_once()
        assert (await store.get(reminder_id))["status"] == "uncertain"
        assert (await deliveries.get(pool, f"looper-reminder-{reminder_id}"))["outcome"] == "unknown"
        assert len(channel.posts) == 1

    run_with_channel_configs(test, monkeypatch)


def test_upgrade_preserves_unknown_legacy_sends_and_reapplies_idempotently(monkeypatch):
    async def test(pool):
        now = datetime.now(UTC)
        pending = await _stored("pending", now)
        sending = await _stored("legacy send", now)
        await store.claim(pool, sending, now)
        await pool.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS post_started_at")
        await pool.execute("DELETE FROM schema_migrations WHERE name='reminder_post_started'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        row = await store.get(sending)
        assert row["post_started_at"] == row["updated_at"]
        assert (await store.get(pending))["post_started_at"] is None
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='reminder_post_started'") == 1
        assert (await store.get(sending))["post_started_at"] == row["post_started_at"]

    run_db(test, monkeypatch)


def test_a_stop_lets_the_send_under_way_finish_and_starts_no_other(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        first, second = await _stored("first", now), await _stored("second", now)
        channel, clock = _Channel(delay=1.0), _Clock(now - timedelta(seconds=1))
        service = reminders.ReminderService(channel, clock=clock, interval=0.05)
        await service.start()
        await asyncio.sleep(0.3)  # a tick before they are due: the service is running, not recovering
        clock.now = now
        for _ in range(200):
            if channel.posts:
                break
            await asyncio.sleep(0.05)
        await service.stop()  # the server stops while the first send is under way
        assert [post["text"].rsplit("：", 1)[1] for post in channel.posts] == ["first"]
        statuses = await _statuses(pool)
        assert (statuses[first][0], statuses[second][0]) == ("delivered", "pending")

    run_with_channel_configs(test, monkeypatch)


def test_a_stop_takes_at_most_its_grace_even_when_the_send_will_not_end(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stuck(_Channel):
        """A send that does not end, and whose cancellation cleanup does not end either, until ``release`` is set."""

        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def post(self, *args: Any, **kwargs: Any) -> PostReceipt:
            self.posts.append(args)
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()  # its clean-up does not end on its own either
                raise
            raise AssertionError

    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        reminder_id = await _stored("看 PR", now)
        monkeypatch.setattr(reminders, "STOP_GRACE_SECONDS", 0.4)
        channel = Stuck()
        service = reminders.ReminderService(channel, clock=_Clock(now), interval=0.05)
        await service.start()
        sending = service._task
        assert sending is not None
        await asyncio.wait_for(channel.started.wait(), 5)  # stop only once a send is actually under way
        loop = asyncio.get_running_loop()
        began = loop.time()
        try:
            await service.stop()
            assert loop.time() - began < 1.0
            assert not sending.done()  # the send did not end; stop returned within the grace anyway
            # The send never answered: it stays claimed, and the next start settles it uncertain, never sending it again.
            assert (await _statuses(pool))[reminder_id][0] == "sending"
        finally:
            # Let the stuck send unwind so the schema pool can close. This is after the assertions.
            channel.release.set()
            if not sending.done():
                try:
                    await sending
                except asyncio.CancelledError:
                    pass

    run_with_channel_configs(test, monkeypatch)


def test_a_refused_send_is_tried_three_times_and_a_lost_answer_never_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        refused = await _stored("refused", now)
        channel = _Channel(*[PostOutcome.REJECTED] * 5)
        service = reminders.ReminderService(channel, clock=_Clock(now))
        for _ in range(5):
            await service.run_once()
        assert len(channel.posts) == 3
        assert (await _statuses(pool))[refused] == ("failed", 3, "the channel answered rejected")
        lost = await _stored("lost", now)
        channel = _Channel(PostOutcome.UNKNOWN)
        service = reminders.ReminderService(channel, clock=_Clock(now))
        await service.run_once()
        await service.run_once()
        assert len(channel.posts) == 1
        assert (await _statuses(pool))[lost] == ("uncertain", 1, "the channel answered unknown")

    run_with_channel_configs(test, monkeypatch)


def test_a_reminder_is_never_sent_to_someone_no_longer_the_bots_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        reminder_id = await _stored("看 PR", now)
        await _operator(pool, enabled=True, who="ou_newoperator")
        channel = _Channel()
        await reminders.ReminderService(channel, clock=_Clock(now)).run_once()
        assert channel.posts == []
        status, attempts, detail = (await _statuses(pool))[reminder_id]
        assert (status, attempts) == ("failed", 0) and "no longer names this user" in detail

    run_with_channel_configs(test, monkeypatch)


def test_nothing_is_sent_while_reminders_are_off_or_the_server_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        await _stored("看 PR", now)
        channel = _Channel()
        await reminders.ReminderService(channel, clock=_Clock(now)).run_once()  # no switch row: off
        await _switch(pool, on=False)
        await reminders.ReminderService(channel, clock=_Clock(now)).run_once()
        await _switch(pool)
        await reminders.ReminderService(channel, clock=_Clock(now), drain=SimpleNamespace(draining=True)).run_once()
        assert channel.posts == []
        await reminders.ReminderService(channel, clock=_Clock(now), drain=SimpleNamespace(draining=False)).run_once()
        assert len(channel.posts) == 1

    run_with_channel_configs(test, monkeypatch)


def test_two_servers_ticking_together_send_a_reminder_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        await _stored("看 PR", now)
        channel = _Channel(delay=0.3)
        await asyncio.gather(*(reminders.ReminderService(channel, clock=_Clock(now)).run_once() for _ in range(2)))
        assert len(channel.posts) == 1

    run_db(test, monkeypatch, setup=_seed_channels, max_size=4)


async def _seed_channels(pool: Any) -> None:
    from looper.agents.serve import seed_channel_configs

    await seed_channel_configs(pool)


def test_remind_me_at_three_reaches_the_users_chat_at_three(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The acceptance path: the user asks on the dashboard, the Manager sets it, Looper sends it at the time."""

    async def test(pool: Any) -> None:
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat = await _converse(tmp_path, [[(ops_reminders.CREATE, {"text": "看 PR", "at": "15:00",
                                                                   "timezone": ZONE})]], ["三点提醒我看 PR"],
                               bound=True)
        (reply,) = chat.replies
        row = await pool.fetchrow("SELECT * FROM reminders")
        assert row is not None, reply
        assert f"已记下提醒 #{row['id']}：" in reply and "15:00（America/Los_Angeles），到时发到你的 Lark 私聊：看 PR" in reply
        assert f"{row['due_at'].astimezone(reminders.cron.zone(ZONE)):%H:%M}" == "15:00"
        channel = _Channel()
        await reminders.ReminderService(channel, clock=_Clock(row["due_at"] + timedelta(seconds=3))).run_once()
        (post,) = channel.posts
        assert post["to"] == OP and post["text"].endswith("让我提醒你：看 PR")

    run_with_channel_configs(test, monkeypatch)


def test_a_database_without_the_table_gets_it_on_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        async with dropped_tables(pool, "reminders"):
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'reminders'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'reminders'") == 1
        await _stored("看 PR", datetime.now(UTC) + timedelta(hours=1))
        await connection.ensure_schema(pool)  # and a second upgrade changes nothing
        assert await pool.fetchval("SELECT count(*) FROM reminders") == 1

    run_with_channel_configs(test, monkeypatch)


def test_the_table_refuses_a_settled_reminder_without_its_time_and_an_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    async def test(pool: Any) -> None:
        reminder_id = await _stored("看 PR", datetime.now(UTC) + timedelta(hours=1))
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE reminders SET status = 'delivered' WHERE id = $1", reminder_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE reminders SET status = 'snoozed', settled_at = now() WHERE id = $1",
                               reminder_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE reminders SET channel_id = 'channel-web' WHERE id = $1", reminder_id)

    run_db(test, monkeypatch)


@pytest.mark.parametrize("hour,reason", [(23, "quiet_hours"), (13, "daily_cap")])
def test_user_reminder_delivery_is_urgent_at_night_and_over_cap(monkeypatch, hour, reason):
    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat_settings.set_delivery_enabled(True)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        noon = datetime(2026, 11, 20, 12, tzinfo=UTC)
        ordinary = delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                                     delivery.ChatKind.DIRECT)
        for n in range(5):
            assert (await delivery.decide(ordinary, dedupe_key=f"quota-{n}", channel_id="channel-lark",
                                          chat=OP, last_user_message_at=None, now=noon)).counted
        now = noon.replace(hour=hour)
        control = await delivery.decide(ordinary, dedupe_key="ordinary-control", channel_id="channel-lark",
                                        chat=OP, last_user_message_at=None, now=now)
        assert (control.outcome.value, control.reason.value) == ("digest", reason)
        reminder_id = await _stored("urgent reminder", now)
        channel = _Channel()
        await reminders.ReminderService(channel, clock=_Clock(now)).run_once()
        row = await deliveries.get(pool, f"looper-reminder-{reminder_id}")
        assert row is not None
        assert (row["source"], row["source_ref"], row["urgency"], row["channel_id"], row["chat"], row["chat_kind"]) == (
            "user_reminder", str(reminder_id), "user_set", "channel-lark", OP, "direct")
        assert (row["decision"], row["reason"], row["counted"], row["digest_date"]) == ("send", "urgent", False, None)
        assert row["decided_at"] == now
        assert (row["outcome"], row["external_id"]) == ("sent", "om_1") and row["sent_at"] is not None
        assert (await _statuses(pool))[reminder_id] == ("delivered", 1, None)
        assert len(channel.posts) == 1
        assert await deliveries.counted_on(pool, "channel-lark", OP, now.date()) == 5

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("mode,channel_id,chat", [
    ("default", "channel-lark", OP), ("false", "channel-telegram", "123450001"),
    ("restored", "channel-lark-2", OP),
])
def test_delivery_off_freezes_reminder_post_bytes_and_records(monkeypatch, mode, channel_id, chat):
    async def test(pool):
        await _operator(pool, enabled=True, channel_id=channel_id, who=chat)
        # The extra bot is not part of the built-in seed.
        if channel_id == "channel-lark-2":
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) "
                               "VALUES ($1, 'lark', true, $2)", channel_id,
                               json.dumps({"operator_sender_id": chat, "allowed_open_ids": [chat]}))
        await _switch(pool)
        if mode == "restored":
            chat_settings.set_delivery_enabled(True)
        if mode != "default":
            chat_settings.set_delivery_enabled(False)
        calls = []

        async def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("delivery must not run while disabled")

        monkeypatch.setattr(delivery, "decide", forbidden)
        monkeypatch.setattr(deliveries, "record_receipt", forbidden)
        now = datetime(2026, 11, 20, 23, tzinfo=UTC)
        reminder_id = await _stored("exact reminder\n第二行", now, chat=chat, channel_id=channel_id)
        await pool.execute("UPDATE reminders SET created_at=$2, timezone='UTC' WHERE id=$1",
                           reminder_id, now - timedelta(minutes=10))
        posts = []

        class Channel(_Channel):
            async def post(self, *args, **kwargs):
                posts.append((args, kwargs))
                return await super().post(*args, **kwargs)

        await reminders.ReminderService(Channel(), clock=_Clock(now)).run_once()
        assert posts == [((channel_id, reminders.target(chat),
                           "⏰ 你在 11-20 22:50 让我提醒你：exact reminder\n第二行"),
                          {"idempotency_key": f"looper-reminder-{reminder_id}",
                           "proactive": reminders.turn_metrics.ProactiveRef(
                               reminders.turn_metrics.ProactiveSource.USER_REMINDER, str(reminder_id),
                               dedupe_key=f"looper-reminder-{reminder_id}")})]
        assert (await _statuses(pool))[reminder_id] == ("delivered", 1, None)
        assert await pool.fetchval("SELECT external_id FROM reminders WHERE id=$1", reminder_id) == "om_1"
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        assert calls == []

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("outcomes,status,attempts,outcome", [
    ([PostOutcome.REJECTED, PostOutcome.SENT], "delivered", 2, "sent"),
    ([PostOutcome.REJECTED] * 3, "failed", 3, "rejected"),
    ([PostOutcome.UNKNOWN], "uncertain", 1, "unknown"),
])
def test_reminder_delivery_retries_reuse_decision_and_record_actual_outcome(monkeypatch, outcomes, status, attempts, outcome):
    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat_settings.set_delivery_enabled(True)
        now = datetime(2026, 11, 20, 23, tzinfo=UTC)
        reminder_id = await _stored("retry reminder", now)
        key = f"looper-reminder-{reminder_id}"
        channel = _Channel(*outcomes)
        first = None
        for n in range(4):
            # A new service instance models a restart between ticks.
            await reminders.ReminderService(channel, clock=_Clock(now + timedelta(seconds=n))).run_once()
            row = await deliveries.get(pool, key)
            assert row is not None
            first = first or row["decided_at"]
            assert row["decided_at"] == first
        assert len(channel.posts) == attempts and {post["key"] for post in channel.posts} == {key}
        assert (await _statuses(pool))[reminder_id][:2] == (status, attempts)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 1
        assert row["outcome"] == outcome and row["sent_at"] is not None
        assert row["external_id"] == (f"om_{attempts}" if outcome == "sent" else None)

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("failure", ["decision", "receipt"])
def test_reminder_delivery_record_failure_does_not_lose_or_repeat_urgent_send(monkeypatch, caplog, failure):
    async def test(pool):
        await _operator(pool, enabled=True)
        await _switch(pool)
        chat_settings.set_delivery_enabled(True)

        async def broken(*args, **kwargs):
            raise RuntimeError("synthetic recording failure")

        monkeypatch.setattr(delivery if failure == "decision" else deliveries,
                            "decide" if failure == "decision" else "record_receipt", broken)
        now = datetime(2026, 11, 20, 23, tzinfo=UTC)
        reminder_id = await _stored("never suppress urgent reminder", now)
        channel = _Channel()
        service = reminders.ReminderService(channel, clock=_Clock(now))
        await service.run_once()
        await service.run_once()
        assert (await _statuses(pool))[reminder_id] == ("delivered", 1, None)
        assert len(channel.posts) == 1
        assert "synthetic recording failure" in caplog.text
        row = await deliveries.get(pool, f"looper-reminder-{reminder_id}")
        assert row is None if failure == "decision" else row["outcome"] == "sent"

    run_with_channel_configs(test, monkeypatch)
