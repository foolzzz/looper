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
        note = ops.begin_turn(_turn("om_next"))
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
        assert ops.begin_turn(_turn("om_1")) == (
            f"[Looper] After your last reply, the operator answered confirmation {asked['confirmation_id']} with its "
            "card: it was cancelled; it did not run.")
        asked, turn = await _ask(ops)
        await pool.execute("DELETE FROM projects WHERE id = $1", project["id"])  # gone before the press
        failed = await (await press(_value(turn))).later
        assert "❌ 没有执行：" in failed.text
        assert ops.begin_turn(_turn("om_2")) == (
            f"[Looper] After your last reply, the operator answered confirmation {asked['confirmation_id']} with its "
            "card: it failed (没找到).")

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
    ops._note_button(key, "c-0", "it ran")
    assert ops._button_notes == {}  # off
    receipts_settings.set_enabled(True)
    for index in range(5):
        ops._note_button(key, f"c-{index}", "it was cancelled; it did not run")
    note = ops.begin_turn(_turn("om_1"))
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
