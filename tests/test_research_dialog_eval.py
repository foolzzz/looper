"""The research-order dialogs' checks (``evals/prompt_regression/dialogs.py``) on synthetic call trails: the order of
the successful lookups, stopping once the answer is found, and asking only when nothing was."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from evals.prompt_regression import dialogs

LEADS = dialogs.LEADS_TOOL


def _look(calls: list[str], reply: str, *, failed: tuple[str, ...] = ()) -> Any:
    actions = [{"tool": tool, "status": "error" if tool in failed else "ok", "risk": "read", "arguments": {}}
               for tool in calls]
    return SimpleNamespace(turn_actions=lambda: actions, actions=lambda: actions, reply=reply, fake=False)


@pytest.mark.parametrize("calls, passes", [
    (["looper_tasks", LEADS], True),
    (["looper_projects", "looper_tasks", LEADS], True),
    ([LEADS, "looper_tasks"], False),  # the leads first, then the records: out of order
    ([LEADS], False),
    (["looper_tasks"], False),  # never searched the leads, where the answer is
])
def test_an_answer_in_the_leads_is_looked_up_after_the_records(calls: list[str], passes: bool) -> None:
    problem = dialogs._found_in_leads(_look(calls, "发布前要跑 ZX-42 回归套件（来自飞书消息，未经确认）。"))
    assert (problem is None) is passes, problem


def test_a_records_read_that_failed_does_not_count_as_reading_them_first() -> None:
    assert dialogs._found_in_leads(_look(["looper_projects", LEADS], "ZX-42", failed=("looper_projects",)))


@pytest.mark.parametrize("calls, passes", [
    (["looper_tasks"], True),
    (["looper_tasks", "looper_tasks"], True),
    (["looper_tasks", LEADS], False),  # went on to the leads after the records had the answer
    ([LEADS, "looper_tasks"], False),
])
def test_an_answer_in_the_records_stops_there(calls: list[str], passes: bool) -> None:
    problem = dialogs._found_in_records(_look(calls, "文件内容要写 done。"))
    assert (problem is None) is passes, problem


@pytest.mark.parametrize("reply", [
    "要写 done，请提供文件路径。",
    "文件内容是 done 吗？告诉我最终内容。",
    "任务里写的是 done，请确认一下。",
    "The requirement says done. Could you confirm the final content?",
    "It should contain done, right? Please tell me the exact text.",
    "Is it done?",
])
def test_a_found_answer_is_not_asked_for_again(reply: str) -> None:
    assert "asks the user" in dialogs._found_in_records(_look(["looper_tasks"], reply))


@pytest.mark.parametrize("reply", [
    "要写 done。需要的话告诉我，我再改。",
    "文件内容要写 done。要我帮你开始这个任务吗？",
    "It should contain done. Let me know if you want me to change it.",
])
def test_an_answer_with_an_offer_of_more_help_passes(reply: str) -> None:
    assert dialogs._found_in_records(_look(["looper_tasks"], reply)) is None


def test_going_on_to_the_leads_after_the_answer_was_found_fails_even_when_that_search_failed() -> None:
    problem = dialogs._found_in_records(_look(["looper_tasks", LEADS], "要写 done。", failed=(LEADS,)))
    assert problem and "went on to the leads" in problem


@pytest.mark.parametrize("calls, reply, passes", [
    (["looper_projects", LEADS], "没有查到 Orion 的预算，请告诉我数字。", True),
    ([LEADS], "没有查到，请告诉我。", False),  # asked before reading its records
    (["looper_projects"], "没有查到，请告诉我。", False),  # asked before searching the leads
    ([LEADS, "looper_projects"], "没有查到，请告诉我。", False),
    (["looper_projects", LEADS], "Orion 的预算是 100 万。", False),  # looked, found nothing, did not ask
])
def test_nothing_found_is_asked_after_looking_everywhere_in_order(calls: list[str], reply: str, passes: bool) -> None:
    problem = dialogs._looked_then_asked(_look(calls, reply))
    assert (problem is None) is passes, problem
