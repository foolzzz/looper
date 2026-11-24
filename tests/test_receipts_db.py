"""Action receipts through ``ManagerOps`` and ``ClientOps`` on a disposable PostgreSQL (assistant phase 3 RFC, 6.9):
the line, ``shown_to_user`` and the two ``manager_actions`` columns while the switch is on; the same calls with it off
leave results, blocks and rows as they were; the button note and the status card's ``recent`` section."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from looper.agents import receipts, receipts_settings
from looper.agents.ops import ErrorCode, Turn, TurnOrigin
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import ManagerOps, Platform
from tests.db_support import DSN, run_db
from tests.test_card_actions_db import ASKER, BOT, DELETE, _ask, _exists, _ops, _presser, _sample, _switch, _turn, _value

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

DONE_UPDATE = "✅ 已修改项目「sample」：background"


@pytest.fixture(autouse=True)
def _receipts_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(receipts_settings, "_enabled", False)


def _begin(ops: ManagerOps, turn: Turn, *args: Any, **kwargs: Any) -> str:
    """What ``begin_turn`` tells the turn, as it reads in the input sent (the notes stand where its token was)."""
    return ops.live_note(turn, ops.begin_turn(turn, *args, **kwargs))


def _web(message_id: str = "m1", **extra: Any) -> Turn:
    return Turn(channel="web", chat="chat-1", message_id=message_id, **extra)


async def _call(ops: ManagerOps, turn: Turn, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    ops.begin_turn(turn, "b-" + str(turn.message_id))
    try:
        return await ops.call(name, arguments, binding="b-" + str(turn.message_id))
    finally:
        ops.end_turn(turn)


async def _rows(pool: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in await pool.fetch(
        "SELECT tool, status, error_code, category, receipt, result FROM manager_actions ORDER BY id")]


async def _schedule(pool: Any, project_id: str, name: str = "日报") -> str:
    from looper.db import schedules

    return await schedules.create_schedule(pool, project_id=project_id, profiles={}, fields={
        "name": name, "cron": "0 16 * * *", "timezone": "Asia/Shanghai", "requirement": name})


def test_switch_off_leaves_results_blocks_and_rows_as_they_were(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _schedule(pool, project["id"])
        ops = _ops()
        turn = _web()
        ops.begin_turn(turn)
        changed = await ops.call("looper_projects", {"action": "update", "project": "sample", "background": "B"})
        failed = await ops.call("looper_projects", {"action": "update", "project": "nope", "background": "B"})
        stopped = await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        ops.end_turn(turn)
        assert changed["status"] == "ok" and "shown_to_user" not in json.dumps(changed)
        assert failed == {"status": "error", "error": {"code": "not_found", "message": failed["error"]["message"]}}
        assert stopped["status"] == "ok" and "shown_to_user" not in json.dumps(stopped)
        assert turn.blocks == [] and turn.receipts == []
        assert [(r["tool"], r["status"], r["category"], r["receipt"]) for r in await _rows(pool)] == [
            ("looper_projects", "ok", None, None), ("looper_projects", "error", None, None),
            ("looper_schedules", "ok", None, None)]
        assert ops.begin_turn(_web("m2")) == ""

    run_db(test, monkeypatch, timeout=30)


def test_switch_on_each_write_gets_a_line_shown_to_user_and_both_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _schedule(pool, project["id"])
        receipts_settings.set_enabled(True)
        ops = _ops()
        turn = _web()
        ops.begin_turn(turn)
        changed = await ops.call("looper_projects", {"action": "update", "project": "sample", "background": "B"})
        failed = await ops.call("looper_projects", {"action": "update", "project": "nope", "background": "B"})
        disabled = await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        listed = await ops.call("looper_projects", {"action": "list"})
        ops.end_turn(turn)
        assert changed["result"]["shown_to_user"] == receipts.SHOWN_DONE
        assert failed["status"] == "error" and failed["error"]["code"] == "not_found"
        assert failed["shown_to_user"] == receipts.SHOWN_FAILED
        assert disabled["result"]["shown_to_user"] == receipts.SHOWN_DONE
        assert "shown_to_user" not in json.dumps(listed)
        assert turn.blocks == [DONE_UPDATE, "❌ 没做成：修改项目「nope」（没找到）", "✅ 定时任务「日报」已停用"]
        rows = await _rows(pool)
        assert [(r["tool"], r["status"], r["category"], r["receipt"]) for r in rows] == [
            ("looper_projects", "ok", "update", DONE_UPDATE),
            ("looper_projects", "error", "update", "❌ 没做成：修改项目「nope」（没找到）"),
            ("looper_schedules", "ok", "control", "✅ 定时任务「日报」已停用"),
            ("looper_projects", "ok", None, None)]
        # The trail keeps what the tool answered, not the sentence added for the model.
        assert "shown_to_user" not in (rows[0]["result"] or "")
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE name = '日报'") is False

    run_db(test, monkeypatch, timeout=30)


def test_a_failure_then_a_success_on_the_same_subject_leave_only_the_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        turn = _web()
        ops.begin_turn(turn)
        await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        assert turn.blocks == ["❌ 没做成：停用定时任务「日报」（没找到）"]
        await _schedule(pool, project["id"])
        await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        ops.end_turn(turn)
        assert turn.blocks == ["✅ 定时任务「日报」已停用"]
        assert [r["status"] for r in await _rows(pool)] == ["error", "ok", "ok"]  # every call is still in the trail

    run_db(test, monkeypatch, timeout=30)


def test_no_receipt_for_a_read_a_question_a_background_turn_a_stopped_turn_or_a_refused_speaker(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import system_settings

    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _schedule(pool, project["id"])
        await system_settings.put_settings({"reminders.enabled": "true"}, pool=pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        # a read
        turn = _web("read")
        assert (await _call(ops, turn, "looper_projects", {"action": "list"}))["status"] == "ok"
        assert turn.blocks == []
        # a question: the confirmation text is its own line, and nothing says it was done
        turn = _web("ask")
        asked = await _call(ops, turn, "looper_projects", DELETE)
        assert asked["status"] == "needs_confirmation" and "shown_to_user" in asked and len(turn.blocks) == 1
        assert turn.blocks[0].startswith("需要你确认：") and turn.receipts == []
        # a background turn
        turn = _web("later", origin=TurnOrigin.COMPLETION)
        answer = await _call(ops, turn, "looper_schedules", {"action": "disable", "schedule": "日报"})
        assert answer["error"]["code"] == ErrorCode.NEEDS_USER_TURN.value and "shown_to_user" not in answer
        assert turn.blocks == []
        # a stopped turn
        turn = _web("stopped")
        ops.begin_turn(turn, "b-stopped")
        ops.stop_turn(turn)
        answer = await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"}, binding="b-stopped")
        assert answer["error"]["code"] == ErrorCode.STOPPED.value and "shown_to_user" not in answer
        assert turn.blocks == []
        # an operator-only tool in a group member's turn
        turn = Turn(channel="lark", chat="g1", message_id="g", sender="ou_member", group=True)
        answer = await _call(ops, turn, "looper_reminder_create", {"text": "x", "in_minutes": 5})
        assert answer["error"]["code"] == ErrorCode.FORBIDDEN.value and "shown_to_user" not in answer
        assert turn.blocks == []
        assert {r["category"] for r in await _rows(pool)} == {None}

    run_db(test, monkeypatch, timeout=30)


def test_a_tool_with_its_own_line_keeps_it_and_the_trail_still_gets_the_receipt(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def update(ops: ManagerOps, name: str, message_id: str) -> tuple[list[str], dict[str, Any]]:
        turn = _web(message_id)
        answer = await _call(ops, turn, "looper_schedules",
                             {"action": "update", "schedule": name, "fields": {"cron": "0 9 * * *"}})
        return [block.replace(name, "X") for block in turn.blocks], answer

    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _schedule(pool, project["id"], "日报")
        await _schedule(pool, project["id"], "周报")
        ops = _ops()
        off_blocks, off_answer = await update(ops, "周报", "off")
        receipts_settings.set_enabled(True)
        on_blocks, on_answer = await update(ops, "日报", "on")
        assert off_blocks == on_blocks and len(on_blocks) == 1 and on_blocks[0].startswith("已修改定时任务「X」")
        # The tool's own sentence to the model stays; no second one is added.
        assert on_answer["result"]["shown_to_user"] == off_answer["result"]["shown_to_user"]
        assert on_answer["result"]["shown_to_user"].startswith("Looper adds what changed")
        off_row, on_row = await _rows(pool)
        assert (off_row["category"], off_row["receipt"]) == (None, None)
        assert (on_row["category"], on_row["receipt"]) == ("update", "✅ 已修改定时任务「日报」：cron")

    run_db(test, monkeypatch, timeout=30)


def test_a_typed_confirmation_runs_the_call_and_the_reply_gets_its_line(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        asked, _turn_asked = await _ask(ops)
        turn = _turn("om_yes")
        ops.begin_turn(turn)
        done = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})
        ops.end_turn(turn)
        assert done["status"] == "ok" and done["result"]["shown_to_user"] == receipts.SHOWN_DONE
        assert turn.blocks == ["✅ 已删除项目「sample」"] and not await _exists(pool, project)
        rows = await _rows(pool)
        assert [(r["tool"], r["status"], r["category"]) for r in rows] == [
            ("looper_projects", "needs_confirmation", None), ("looper_projects", "ok", "remove")]

    run_db(test, monkeypatch, timeout=30)


def test_a_press_shows_the_receipt_on_the_card_and_tells_the_next_user_turn_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        asked, turn = await _ask(ops)
        done = await (await press(_value(turn))).later
        assert done.text.endswith("✅ 已执行。\n\n✅ 已删除项目「sample」")
        # A completion turn in between does not take the note; the next user turn in this chat does.
        completion = Turn(channel="lark_bot", chat="oc_dm", channel_id=BOT, message_id="done-1",
                          origin=TurnOrigin.COMPLETION)
        assert ops.begin_turn(completion) == ""
        note = _begin(ops, _turn("om_next"))
        assert note == (f"[Looper] After your last reply, the operator answered confirmation {asked['confirmation_id']} "
                        "with its card: it ran (✅ 已删除项目「sample」).")
        assert ops.begin_turn(_turn("om_after")) == ""  # told once
        assert ops.begin_turn(_turn("om_other", chat="oc_other")) == ""  # and only in its own chat

    run_db(test, monkeypatch, timeout=30)


def test_a_cancelled_or_failed_press_is_told_to_the_next_turn_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        asked, turn = await _ask(ops)
        assert (await press(_value(turn, "cancel"))).toast == "已取消"
        following = _turn("om_1")
        assert _begin(ops, following) == (
            f"[Looper] After your last reply, the operator answered confirmation {asked['confirmation_id']} with its "
            "card: it was cancelled; it did not run.")
        ops.end_turn(following)  # The Manager ends a delivered turn before starting the next one.
        asked, turn = await _ask(ops)
        await pool.execute("DELETE FROM projects WHERE id = $1", project["id"])  # gone before the press
        failed = await (await press(_value(turn))).later
        assert failed.text.endswith("\n\n❌ 没做成：删除项目「sample」（没找到）") and "没有执行：" not in failed.text
        following = _turn("om_2")
        assert _begin(ops, following) == (
            f"[Looper] After your last reply, the operator answered confirmation {asked['confirmation_id']} with its "
            "card: it failed (没找到).")
        ops.end_turn(following)

    run_db(test, monkeypatch, timeout=30)


def test_with_the_switch_off_a_press_stores_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _sample(pool)
        await _switch(pool)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        _, turn = await _ask(ops)
        done = await (await press(_value(turn))).later
        assert done.text.endswith("✅ 已执行。") and ops._button_notes == {}
        assert ops.begin_turn(_turn("om_next")) == ""

    run_db(test, monkeypatch, timeout=30)


def test_notes_are_kept_per_chat_at_most_three_and_only_while_on() -> None:
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="t")
    key = ("lark_bot", BOT, "oc_dm")
    ops._note_button(key, ASKER, "c-0", "it ran")
    assert ops._button_notes == {}  # off
    receipts_settings.set_enabled(True)
    for index in range(5):
        ops._note_button(key, ASKER, f"c-{index}", "it was cancelled; it did not run")
    note = _begin(ops, _turn("om_1"))
    assert [line.split()[9] for line in note.splitlines()] == ["c-2", "c-3", "c-4"]


def test_an_api_client_gets_the_line_in_notes_and_the_columns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.api_v1 import Principal
    from looper.agents.bus import MessageBus
    from looper.db import api_clients

    async def test(pool: Any) -> None:
        await _sample(pool)
        view, _ = await api_clients.create("agent", pool=pool)
        client = Principal("client", view["id"], view["name"])
        ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                 task_router=SimpleNamespace(has_planner=lambda task_id: False)))
        arguments = {"action": "update", "project": "sample", "background": "B"}
        plain = await ops.call(client, "s1", "looper_projects", arguments)
        assert plain["status"] == "ok" and "notes" not in plain and "shown_to_user" not in json.dumps(plain)
        receipts_settings.set_enabled(True)
        answer = await ops.call(client, "s1", "looper_projects", arguments)
        assert answer["status"] == "ok" and answer["notes"] == [DONE_UPDATE]
        assert "shown_to_user" not in json.dumps(answer)  # a client has no reply to avoid repeating
        failed = await ops.call(client, "s1", "looper_projects", {**arguments, "project": "nope"})
        assert failed["error"]["code"] == "not_found"
        rows = await _rows(pool)
        assert [(r["category"], r["receipt"]) for r in rows] == [
            (None, None), ("update", DONE_UPDATE), ("update", "❌ 没做成：修改项目「nope」（没找到）")]

    run_db(test, monkeypatch, timeout=30)


def test_the_status_card_has_a_recent_section_only_while_receipts_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import chat_settings, status_card

    class Fleet:
        def agents(self) -> list[Any]:
            return [SimpleNamespace(name="Manager", agent_id="manager", activity=lambda: [])]

    async def card() -> dict[str, Any]:
        return await status_card.snapshot(fleet=Fleet(), task_router=SimpleNamespace(running_planners=lambda: []),
                                          worker_pool=None, now=datetime.now(timezone.utc))

    def clock(moment: datetime) -> str:
        return moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")

    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_timezone", "Asia/Shanghai")
        project = await _sample(pool)
        await _schedule(pool, project["id"])
        ops = _ops()
        # Written with the switch on; read with it off (no section) and on (the section).
        receipts_settings.set_enabled(True)
        turn = _web()
        ops.begin_turn(turn)
        await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        for index in range(6):
            await ops.call("looper_projects", {"action": "update", "project": "sample", "background": f"B{index}"})
        ops.end_turn(turn)
        await pool.execute("UPDATE manager_actions SET created_at = now() - interval '2 minutes' WHERE id = 1")
        await pool.execute("UPDATE manager_actions SET created_at = now() - interval '30 hours' WHERE id = 2")
        await pool.execute("UPDATE manager_actions SET created_at = now() - interval '1 minute' WHERE id > 2")
        receipts_settings.set_enabled(False)
        assert "recent" not in [s["name"] for s in (await card())["sections"]]
        receipts_settings.set_enabled(True)
        result = await card()
        (recent,) = [s for s in result["sections"] if s["name"] == "recent"]
        assert recent["state"] == "ok" and len(recent["items"]) == 5  # at most 5, newest first
        assert {item["receipt"] for item in recent["items"]} == {DONE_UPDATE}
        stamp = clock(datetime.fromisoformat(recent["items"][0]["at"]))
        assert result["text"].splitlines()[-1].startswith(f"最近：{DONE_UPDATE}（{stamp}）；")
        # Older than 24 hours is out: id 2 is 30 hours old, so only the disable (id 1) is left.
        await pool.execute("DELETE FROM manager_actions WHERE id > 2")
        (recent,) = [s for s in (await card())["sections"] if s["name"] == "recent"]
        assert [item["receipt"] for item in recent["items"]] == ["✅ 定时任务「日报」已停用"]
        stamp = clock(datetime.fromisoformat(recent["items"][0]["at"]))
        assert (await card())["text"].splitlines()[-1] == f"最近：✅ 定时任务「日报」已停用（{stamp}）"
        await pool.execute("DELETE FROM manager_actions")
        assert (await card())["text"].splitlines()[-1] == "最近：没有"

    run_db(test, monkeypatch, timeout=30)


def test_a_failed_read_of_the_recent_receipts_is_an_error_section_never_an_empty_one(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import status_card
    from looper.db import manager_actions

    async def broken(**_: Any) -> Any:
        raise RuntimeError("receipts store is down")

    async def test(pool: Any) -> None:
        receipts_settings.set_enabled(True)
        monkeypatch.setattr(manager_actions, "recent_receipts", broken)
        card = await status_card.snapshot(fleet=None, task_router=None, worker_pool=None,
                                          now=datetime(2026, 11, 22, 7, 5, tzinfo=timezone.utc))
        (recent,) = [s for s in card["sections"] if s["name"] == "recent"]
        assert recent["state"] == "error" and "receipts store is down" in recent["error"] and "items" not in recent
        assert "最近：没能读取（RuntimeError: receipts store is down）" in card["text"]

    run_db(test, monkeypatch, timeout=30)


def test_the_metric_counts_writes_by_category_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import manager_actions

    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _schedule(pool, project["id"])
        ops = _ops()
        turn = _web()
        ops.begin_turn(turn)
        await ops.call("looper_projects", {"action": "update", "project": "sample", "background": "A"})  # off: none
        receipts_settings.set_enabled(True)
        await ops.call("looper_projects", {"action": "update", "project": "sample", "background": "B"})
        await ops.call("looper_projects", {"action": "update", "project": "nope", "background": "B"})
        await ops.call("looper_schedules", {"action": "disable", "schedule": "日报"})
        await ops.call("looper_projects", {"action": "list"})
        ops.end_turn(turn)
        await pool.execute("UPDATE manager_actions SET created_at = now() - interval '10 days' WHERE id = 2")
        assert await manager_actions.count_by_category(7, pool=pool) == {
            "update": {"error": 1}, "control": {"ok": 1}}
        assert await manager_actions.count_by_category(30, pool=pool) == {
            "update": {"ok": 1, "error": 1}, "control": {"ok": 1}}

    run_db(test, monkeypatch, timeout=30)


def test_notes_queued_while_on_are_never_told_once_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        _, turn = await _ask(ops)
        await (await press(_value(turn))).later
        assert ops._button_notes
        receipts_settings.set_enabled(False)
        assert ops.begin_turn(_turn("om_next")) == "" and ops._button_notes == {}
        receipts_settings.set_enabled(True)
        assert ops.begin_turn(_turn("om_later")) == ""  # dropped, not parked

    run_db(test, monkeypatch, timeout=30)


def test_notes_go_only_to_the_person_who_pressed_and_not_across_a_session_reset(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.turn_context import SpeakerKind

    async def test(pool: Any) -> None:
        await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        _, turn = await _ask(ops)
        await (await press(_value(turn))).later
        stranger = _turn("om_x", sender="ou_stranger")
        assert ops.begin_turn(stranger) == ""
        member = _turn("om_m")
        member.speaker = SpeakerKind.MEMBER
        assert ops.begin_turn(member) == ""
        assert _begin(ops, _turn("om_mine")).startswith("[Looper] After your last reply")  # still there for them
        await _sample(pool)
        _, turn = await _ask(ops)
        await (await press(_value(turn))).later
        ops.forget_confirmations()  # the session was reset
        assert ops._button_notes == {} and ops.begin_turn(_turn("om_new")) == ""

    run_db(test, monkeypatch, timeout=30)


def test_a_failed_press_shows_the_generated_failed_receipt_on_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        _, turn = await _ask(ops)
        await pool.execute("DELETE FROM projects WHERE id = $1", project["id"])
        failed = await (await press(_value(turn))).later
        assert failed.text.endswith("\n\n❌ 没做成：删除项目「sample」（没找到）") and "没有执行：" not in failed.text
        (row,) = [r for r in await _rows(pool) if r["status"] == "error"]
        assert (row["category"], row["receipt"]) == ("remove", "❌ 没做成：删除项目「sample」（没找到）")

    run_db(test, monkeypatch, timeout=30)


def _client_ops(tmp_path: Path, **extra: Any) -> ClientOps:
    from looper.agents.bus import MessageBus

    return ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                              task_router=SimpleNamespace(has_planner=lambda task_id: False)), **extra)


async def _client(pool: Any) -> Any:
    from looper.agents.api_v1 import Principal
    from looper.db import api_clients

    view, _ = await api_clients.create("agent", pool=pool)
    return Principal("client", view["id"], view["name"])


def test_every_client_error_exit_has_the_failed_receipt_notes_and_both_columns(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    line = "❌ 没做成：删除定时任务「周报」（没找到）"

    async def test(pool: Any) -> None:
        project = await _sample(pool)
        client = await _client(pool)
        now = [0.0]
        ops = _client_ops(tmp_path, clock=lambda: now[0])
        gone = {"action": "delete", "schedule": "周报"}
        off = await ops.call(client, "s1", "looper_schedules", gone)  # off: as before, no notes
        assert off["status"] == "error" and "notes" not in off
        receipts_settings.set_enabled(True)
        # escalate refuses (a schedule that does not exist)
        direct = await ops.call(client, "s1", "looper_schedules", gone)
        assert direct["error"]["code"] == "not_found" and direct["notes"] == [line]
        # a run that fails after the confirmation was asked
        asked = await ops.call(client, "s2", "looper_projects", DELETE)
        assert asked["status"] == "needs_confirmation"
        await pool.execute("DELETE FROM projects WHERE id = $1", project["id"])
        confirmed = await ops.confirm(client, "s2", asked["confirmation_id"])
        assert confirmed["error"]["code"] == "not_found"
        assert confirmed["notes"] == ["❌ 没做成：删除项目「sample」（没找到）"]
        # a confirmation that lapsed
        await _sample(pool)
        asked = await ops.call(client, "s3", "looper_projects", DELETE)
        now[0] = 10_000
        lapsed = await ops.confirm(client, "s3", asked["confirmation_id"])
        assert lapsed["error"]["code"] == "expired"
        assert lapsed["notes"] == ["❌ 没做成：删除项目「sample」（确认已过期）"]
        rows = [(r["status"], r["error_code"], r["category"], r["receipt"]) for r in await _rows(pool)]
        assert rows == [("error", "not_found", None, None), ("error", "not_found", "remove", line),
                        ("needs_confirmation", None, None, None),
                        ("error", "not_found", "remove", "❌ 没做成：删除项目「sample」（没找到）"),
                        ("needs_confirmation", None, None, None),
                        ("error", "expired", "remove", "❌ 没做成：删除项目「sample」（确认已过期）")]

    run_db(test, monkeypatch, timeout=30)


def _fake_tools() -> list[tuple[Any, Any]]:
    """One stand-in tool per registered tool, whose run succeeds or fails as its arguments say."""
    from looper.agents.ops import OpError, Risk, Tool
    from looper.agents.ops.categories import TOOL_ACTIONS, ActionKind

    async def run(call: Any) -> Any:
        if call.arguments.get("fail"):
            raise OpError(ErrorCode.NOT_FOUND, "no such thing")
        return {"done": True}

    found = []
    for name, entry in TOOL_ACTIONS.items():
        kinds = {None: entry} if isinstance(entry, ActionKind) else dict(entry)
        subjects = {kind.subject for kind in kinds.values() if kind.subject}
        schema = {"type": "object", "properties": {"action": {"type": "string"}, "fail": {"type": "boolean"},
                                                   **{key: {"type": "string"} for key in subjects}}}
        found.append((Tool(name=name, description="stand-in", schema=schema, risk=Risk.WRITE, run=run), kinds))
    return found


def _matrix_ops(tmp_path: Path, client: bool, monkeypatch: pytest.MonkeyPatch) -> Any:
    tools = [tool for tool, _ in _fake_tools()]
    if client:
        return _client_ops(tmp_path, tools=tools)
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="t", tools=tools)

    # The Manager swaps a tool in or out of its scope by that tool's own switch (the ledger, reminders, any added
    # later), which would put the real tool back in place of its stand-in. The matrix is about receipts, not about
    # switches (the real-tool test below runs the switched tools), so its scope stays the stand-ins.
    async def stand_ins() -> Any:
        return ops._scope

    ops.current_scope = stand_ins

    # Memory proposals commit after their audit row exists; replace that phase along with the tool's run.
    from looper.agents.ops import memory_propose

    async def committed(call: Any, answer: Any, action_id: Any) -> Any:
        return answer

    monkeypatch.setattr(memory_propose, "commit", committed)
    return ops


@pytest.mark.parametrize("client", [False, True])
def test_off_every_write_tool_success_and_failure_are_as_before(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                client: bool) -> None:
    async def test(pool: Any) -> None:
        ops = _matrix_ops(tmp_path, client, monkeypatch)
        principal = await _client(pool) if client else None
        turn = _web()
        if not client:
            ops.begin_turn(turn)
        count = 0
        for tool, kinds in _fake_tools():
            for action, kind in kinds.items():
                for fail in (False, True):
                    arguments = {**({"action": action} if action else {}), **({kind.subject: "demo"} if kind.subject else {}),
                                 **({"fail": True} if fail else {})}
                    answer = await (ops.call(principal, "s1", tool.name, arguments) if client
                                    else ops.call(tool.name, arguments))
                    count += 1
                    if fail:
                        assert answer == {"status": "error", "error": {"code": "not_found", "message": "no such thing"}}
                    else:
                        assert answer == {"status": "ok", "result": {"done": True}}
        assert turn.blocks == [] and count == len(await _rows(pool)) and count > 40
        assert {(r["category"], r["receipt"]) for r in await _rows(pool)} == {(None, None)}

    run_db(test, monkeypatch, timeout=60)


@pytest.mark.parametrize("client", [False, True])
def test_on_every_write_tool_gets_its_receipt_and_category(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                           client: bool) -> None:
    async def test(pool: Any) -> None:
        receipts_settings.set_enabled(True)
        ops = _matrix_ops(tmp_path, client, monkeypatch)
        principal = await _client(pool) if client else None
        for tool, kinds in _fake_tools():
            for action, kind in kinds.items():
                for fail in (False, True):
                    turn = _web("m")
                    arguments = {**({"action": action} if action else {}), **({kind.subject: "demo"} if kind.subject else {}),
                                 **({"fail": True} if fail else {})}
                    if client:
                        answer = await ops.call(principal, "s1", tool.name, arguments)
                        lines = answer.get("notes")
                    else:
                        answer = await _call(ops, turn, tool.name, arguments)
                        lines = turn.blocks
                    assert lines and len(lines) == 1 and lines[0].startswith("❌ 没做成：" if fail else "✅ "), (tool.name, action)
                    if not client:
                        assert answer.get("shown_to_user" if fail else "result") is not None
        rows = await _rows(pool)
        assert all(r["category"] and r["receipt"] for r in rows) and len(rows) > 40

    run_db(test, monkeypatch, timeout=60)


def test_on_and_off_with_the_real_tools_a_real_escalate_own_blocks_and_the_four_switched_tools(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The stand-in matrix cannot show what the real tools do; these run the real implementations, off then on."""
    import re

    from tests.db_support import run_with_channel_configs
    from tests.test_reminders_db import _dm, _operator, _switch as reminders_switch
    from looper.agents.ops import reminders as ops_reminders

    async def scenario(ops: ManagerOps, pool: Any, tag: str, n: int) -> list[tuple[str, dict[str, Any], list[str]]]:
        from tests.test_reminders_db import _call as call

        steps = [
            ("looper_schedules", {"action": "delete", "schedule": f"gone-{tag}"}),  # a real escalate refuses it
            ("looper_projects", {"action": "update", "project": f"p-{tag}", "background": "B"}),
            ("looper_projects", {"action": "update", "project": f"none-{tag}", "background": "B"}),
            ("looper_schedules", {"action": "update", "schedule": f"s-{tag}", "fields": {"cron": "0 9 * * *"}}),
            ("looper_ledger_add", {"kind": "pending", "title": f"T-{tag}"}),
            ("looper_ledger_update", {"action": "note", "item": f"L-{n}", "note": "seen"}),
            ("looper_ledger_update", {"action": "note", "item": "L-9999", "note": "seen"}),
            (ops_reminders.CREATE, {"text": f"r-{tag}", "in_minutes": 30}),
            (ops_reminders.CANCEL, {"id": n}),
            (ops_reminders.CANCEL, {"id": 9999}),
        ]
        done = []
        for index, (name, arguments) in enumerate(steps):
            turn = _dm(f"m-{tag}-{index}")
            done.append((name, await call(ops, turn, name, arguments), turn.blocks))
        return done

    async def test(pool: Any) -> None:
        project = await pool.fetchrow("INSERT INTO projects (id, name, background, work_dir) VALUES ('p1', 'p-off', 'A', '/srv/p') RETURNING id")
        await pool.execute("INSERT INTO projects (id, name, background, work_dir) VALUES ('p2', 'p-on', 'A', '/srv/p')")
        await _schedule(pool, project["id"], "s-off")
        await _schedule(pool, project["id"], "s-on")
        await _operator(pool, enabled=True)
        await reminders_switch(pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', 'true') "
                           "ON CONFLICT (key) DO UPDATE SET value = 'true'")
        ops = _ops()
        off = await scenario(ops, pool, "off", 1)
        off_rows = await _rows(pool)
        assert {(r["category"], r["receipt"]) for r in off_rows} == {(None, None)}
        receipts_settings.set_enabled(True)
        on = await scenario(ops, pool, "on", 2)
        norm = lambda text: re.sub(r"\d+", "N", re.sub(r"sched-[0-9a-f]+", "sched-X", text).replace("-off", "-X").replace("-on", "-X"))
        own = {"looper_schedules": 3, "looper_ledger_add": 4, "looper_ledger_update": 5, "looper_reminder_create": 7,
               "looper_reminder_cancel": 8}
        for index, ((name, off_answer, off_blocks), (_, on_answer, on_blocks)) in enumerate(zip(off, on)):
            if index in (3, 4, 5, 7, 8):  # tools with their own line: byte-identical blocks, their own shown_to_user
                assert [norm(b) for b in on_blocks] == [norm(b) for b in off_blocks] and len(off_blocks) == 1, (index, off_answer, on_answer)
                assert on_answer["status"] == off_answer["status"] == "ok", (index, on_answer)
                assert norm(json.dumps(on_answer, ensure_ascii=False, sort_keys=True)) == norm(
                    json.dumps(off_answer, ensure_ascii=False, sort_keys=True)), index
            elif index in (0, 2, 6, 9):  # failures: nothing before, a failed-write line now
                assert off_answer["status"] == "error" and off_blocks == [] and "shown_to_user" not in off_answer
                assert on_answer["status"] == "error" and on_answer["shown_to_user"] == receipts.SHOWN_FAILED
                assert len(on_blocks) == 1 and on_blocks[0].startswith("❌ 没做成："), (index, on_blocks)
            else:  # the plain success
                assert off_blocks == [] and len(on_blocks) == 1 and on_blocks[0].startswith("✅ ")
                assert on_answer["result"]["shown_to_user"] == receipts.SHOWN_DONE
        on_rows = (await _rows(pool))[len(off_rows):]
        writes = [r for r in on_rows if r["status"] != "needs_confirmation"]
        assert len(writes) == 10 and all(r["category"] and r["receipt"] for r in writes)

    run_with_channel_configs(test, monkeypatch)


def test_turning_the_switch_off_and_on_again_does_not_backfill_old_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        _, turn = await _ask(ops)
        await (await press(_value(turn))).later
        receipts_settings.set_enabled(False)  # off and on again, no turn in between
        receipts_settings.set_enabled(True)
        assert ops.begin_turn(_turn("om_next")) == ""

    run_db(test, monkeypatch, timeout=30)


def test_a_reset_named_session_does_not_read_the_notes_left_in_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _sample(pool)
        await _switch(pool)
        receipts_settings.set_enabled(True)
        ops = _ops()
        press = _presser(monkeypatch, ops)

        async def ask_and_press() -> None:
            turn = _turn("om_ask")
            ops.begin_turn(turn, "b-ask", session_key="group:g1")
            asked = await ops.call("looper_projects", DELETE, binding="b-ask", session_key="group:g1")
            ops.end_turn(turn)
            assert asked["status"] == "needs_confirmation", asked
            await (await press(_value(turn))).later

        await ask_and_press()
        assert ops.begin_turn(_turn("om_main"), session_key="main") == ""  # another session never reads it
        assert _begin(ops, _turn("om_g1"), session_key="group:g1").startswith("[Looper] After your last reply")
        await _sample(pool)
        await ask_and_press()
        ops.forget_session("group:g1")  # what the Manager does when that session is reset
        assert ops.begin_turn(_turn("om_g1b"), session_key="group:g1") == ""
        assert ops._button_notes == {}

    run_db(test, monkeypatch, timeout=30)
