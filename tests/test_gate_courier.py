"""What each message of a user gate's discussion says in a chat (``gate_courier.message_text``): code's words in
Chinese, the Planner's reply as written, the @-the-bot line in a Lark group only while the gate waits for an
answer, and nothing for the user's own messages."""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.gate_courier import ANSWER_LINE, GROUP_LINE, message_text
from looper.agents.gate_service import GateChat
from looper.agents.task_notice import NotifyChannel, NotifyTarget, TargetKind

DM = GateChat(NotifyChannel.LARK, NotifyTarget(TargetKind.USER, "ou_1"))
GROUP = GateChat(NotifyChannel.LARK, NotifyTarget(TargetKind.CHAT, "oc_1", "ou_1"))
TELEGRAM = GateChat(NotifyChannel.TELEGRAM, NotifyTarget(TargetKind.CHAT, "42"))
AGENDA = [{"id": "q1", "question": "Which database?", "options": ["PostgreSQL", "SQLite"], "status": "open"},
          {"id": "q2", "question": "How long is history kept?", "options": [], "status": "resolved",
           "answer": "90 days"}]


def _row(kind: str, text: str = "", state: str = "discussing", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "text": text, "state": state, "task_id": "task-a", "title": "settings file", "goal_id": "",
            "requirement": "write it", "topic": "storage", "agenda": AGENDA, "findings": "", "summary": None,
            "resolution": "", "plan_change": None, "opened_at": None, "failures": 0, **extra}


@pytest.mark.parametrize(("row", "chat", "expected"), [
    (_row("reply", "Which version?"), DM, "Which version?"),
    (_row("reply", "Which version?"), GROUP, f"Which version?\n{GROUP_LINE}"),
    (_row("reply", "Settled.", state="passed"), GROUP, "Settled."),
    (_row("resumed"), TELEGRAM, "▶️ 任务继续了，这个问题仍在等你：storage"),
    (_row("error", failures=1), DM, "⚠️ 规划者没能处理你的消息：约 1 分钟后会自动重试，你的下一条消息也会一起交给它。"),
    (_row("error", failures=2), DM, "⚠️ 规划者没能处理你的消息：约 5 分钟后会自动重试，你的下一条消息也会一起交给它。"),
    (_row("error", failures=3), GROUP, f"⚠️ 规划者没能处理你的消息：你的下一条消息会把它一起交给规划者。\n{GROUP_LINE}"),
    (_row("late", state="passed"), DM, "你在讨论结束时发的消息已作为补充说明交给规划者。"),
    (_row("cancelled", "No answer needed: s1 found it", state="cancelled", resolution="s1 found it"), GROUP,
     "✅ 不用回答了：s1 found it"),
    (_row("message", "PostgreSQL"), DM, None),
    (_row("end"), DM, None),
])
def test_each_kind_says_what_happened(row: dict[str, Any], chat: GateChat, expected: str | None) -> None:
    assert message_text(row, chat) == expected


def test_the_card_lists_the_open_questions_with_their_options_and_how_to_answer() -> None:
    card = message_text(_row("opened", state="open", findings="the service runs PostgreSQL 15"), GROUP,
                        dashboard_url="http://looper.test")
    assert card == ("⏸ 需要你决定 · settings file\nstorage\n1. Which database?（选项：PostgreSQL / SQLite）\n"
                    f"目前的发现：the service runs PostgreSQL 15\n{ANSWER_LINE}\n{GROUP_LINE}\n"
                    "[http://looper.test/#tasks/task-a](http://looper.test/#tasks/task-a)")
    # Telegram gets the bare link and no @-the-bot line.
    assert message_text(_row("opened", state="open"), TELEGRAM).endswith(f"{ANSWER_LINE}\n/#tasks/task-a")
