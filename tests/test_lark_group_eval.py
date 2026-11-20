"""The scoring of ``evals/lark_group.py``'s completion conversations, on synthetic posts and rows."""

from __future__ import annotations

from evals import lark_group as lg

TASK = "task-ab12cd34ef56"
LINK = f"[https://x.test/#tasks/{TASK}](https://x.test/#tasks/{TASK})"
WORDS = "做好了：hello.py 已创建。\n要我再加个测试吗？"
EVENT = {"state": "delivered", "reason": None, "ended": True, "seconds_to_sent": 12.0, "turn_cost_usd": 0.05}


def post(kind: str, text: str) -> dict[str, str]:
    return {"kind": kind, "to": "user:ou_x", "text": text}


def direct(text: str = f"{WORDS}\n\n{LINK}", *, posts=None, event=EVENT, calls=(), status="completed"):
    posts = posts if posts is not None else [post("answer", "任务已创建"), post("answer", text)]
    return lg.score_completion(posts, TASK, status, event, list(calls))


def test_a_manager_message_with_a_link_passes():
    result = direct(calls=[{"tool": "looper_tasks", "risk": "read"}])
    assert result["passed"] and result["turn_calls"] == ["looper_tasks"]
    assert result["event_state"] == "delivered" and result["seconds_to_sent"] == 12.0


def test_the_code_notice_is_not_the_managers_message():
    result = direct(posts=[post("answer", "ok"), post("notice", f"✅ 任务完成 · x\n{LINK}")], event=None)
    assert not result["manager_last"] and not result["delivered"] and not result["passed"]


def test_a_notice_anywhere_fails_manager_last():
    assert not direct(posts=[post("notice", "✅ 任务完成 · x"), post("answer", f"{WORDS}\n\n{LINK}")])["manager_last"]


def test_the_link_must_name_the_task():
    assert not direct(f"{WORDS}\n\n[l](https://x.test/#tasks/task-other)")["link"]


def test_first_line_must_be_a_sentence():
    for first in ("# 总结", "- 完成", "* 完成", "| a | b |", "1. 完成"):
        assert not direct(f"{first}\n要我继续吗？\n\n{LINK}")["first_line"], first
    assert not direct(LINK)["first_line"]  # nothing before the link
    assert direct(f"2024 年的数据已更新。\n\n{LINK}")["first_line"]


def test_at_most_eight_non_empty_lines():
    nine = "\n".join(f"第{i}行" for i in range(9))
    eight = "\n\n".join(f"第{i}行" for i in range(8))
    assert not direct(f"{nine}\n\n{LINK}")["lines_ok"]
    assert direct(f"{eight}\n\n{LINK}")["lines_ok"]


def test_a_write_call_fails_no_writes():
    result = direct(calls=[{"tool": "looper_task_control", "risk": "write", "error_code": "needs_user_turn"}])
    assert not result["no_writes"] and not result["passed"]


def test_a_failed_task_does_not_pass():
    assert not direct(status="failed")["passed"]


def test_fallback_event_is_not_delivered():
    assert not direct(event={**EVENT, "state": "fallback", "reason": "turn_failed"})["delivered"]


def test_manager_words_stop_at_the_first_link_line():
    assert lg.manager_words(f"{WORDS}\n\n{LINK}\n[b](https://x.test/#tasks/t2)") == WORDS
    assert lg.manager_words("no link") == "no link"


def group(posts, event):
    return lg.score_completion_group(posts, "/w", "completed", event)


def test_group_keeps_the_notice_and_skips_the_event():
    posts = [post("answer", lg.GROUP_START_QUESTION), post("notice", "✅ 任务完成 · x")]
    assert group(posts, {"state": "skipped", "reason": "group_chat", "ended": True})["passed"]
    assert not group(posts, {"state": "delivered", "reason": None, "ended": True})["event_skipped"]
    assert not group(posts, None)["passed"]


def test_aliases_and_chats():
    assert lg.ALIASES["completion"].split(",") == list(lg.COMPLETION)
    assert set(lg.COMPLETION) <= set(lg.CHATS) and "completion-group" in lg.GROUPS
