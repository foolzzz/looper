"""Whole-message task commands parse, and a sentence that only contains one does not."""

import pytest

from looper.agents.task_commands import (
    DIRECT, NOT_YET_REPLIES, ORCHESTRATED, STARTING, TaskCommand, delivery_route, fixed_command_reply,
    history_for_model, parse, start_reply,
)
from looper.db.tasks import STATUS_GROUPS, TaskGroup
from looper.agents.tiers.task_manager import resume_message


def test_parse_commands():
    for command in TaskCommand:
        word = command.value
        assert parse(word) is command
        assert parse(f"  {word.upper()}  ") is command
        assert parse(f"\n{word[:1] + word[1:].upper()}\t") is command
    assert parse(" /stop ") is TaskCommand.STOP
    assert parse("/DONE") is TaskCommand.DONE
    for text in ("/stop now", "please /done", "/stopped", "/done please", "stop", "", "/stop/done", " / stop"):
        assert parse(text) is None


def test_every_task_command_has_a_reply():
    """Each command is answered from live state or from its own mapped sentence. A gap raises."""
    live = {TaskCommand.STOP, TaskCommand.DONE, TaskCommand.START}
    assert live.isdisjoint(NOT_YET_REPLIES)
    assert set(NOT_YET_REPLIES) | live == set(TaskCommand)
    assert NOT_YET_REPLIES == {
        TaskCommand.BACKGROUND: "这个命令要等「交给后台」上线后才能用",
        TaskCommand.PAUSE: "此控制将在后续步骤提供",
        TaskCommand.TAKEOVER: "此控制将在后续步骤提供",
    }
    for command, text in NOT_YET_REPLIES.items():
        assert fixed_command_reply(command) == text
    for command in live:
        with pytest.raises(RuntimeError, match="no reply"):
            fixed_command_reply(command)


def test_start_reply_follows_each_status_group():
    """One sentence per dashboard group. A status the groups do not know is the neutral fallback."""
    expected = {
        TaskGroup.READY: "请用上方的「开始」按钮启动任务",
        TaskGroup.DRAFT: "请用上方的「开始」按钮启动任务",
        TaskGroup.IN_PROGRESS: "任务已经在进行中",
        TaskGroup.NEEDS_YOU: "任务已经在进行中",
        TaskGroup.ATTENTION: "请用「任务详情」里的「继续后台任务」按钮继续",
        TaskGroup.CANCELLED: "请用「任务详情」里的「继续后台任务」按钮继续",
        TaskGroup.COMPLETED: "任务已经完成",
    }
    seen = set()
    for status, group in STATUS_GROUPS.items():
        assert start_reply(status) == expected[group]
        seen.add(group)
    assert seen == set(expected)
    for status in ("not-a-status", None, "", 3):
        assert start_reply(status) == "请用任务控制里的按钮操作"


def test_delivery_route_by_status():
    assert delivery_route("pending") is STARTING
    assert delivery_route("assigned") is STARTING
    for status in ("executing", "running", "waiting_user", "planning", "verifying", "stopping", "waiting",
                   "awaiting_budget"):
        assert delivery_route(status) is ORCHESTRATED
    for status in ("draft", "init", "ready", "failed", "stopped", "completed", "cancelled", None, "not-a-status"):
        assert delivery_route(status) is DIRECT


def test_history_for_model_drops_each_command_and_its_code_reply():
    history = []
    for command in TaskCommand:
        history.append({"role": "user", "content": f"\n  {command.value.upper()}  "})
        history.append({"role": "tool", "content": "{}"})
        history.append({"role": "event", "content": "command"})
        history.append({"role": "assistant", "content": f"code reply {command.value}"})
    history.append({"role": "user", "content": "please continue"})
    history.append("/stop")
    history.append({"role": "assistant", "content": "已停止这一轮"})
    history.append({"role": "user", "content": "after"})
    kept = history_for_model(history)
    assert kept == [
        {"role": "user", "content": "please continue"},
        {"role": "user", "content": "after"},
    ]
    resumed = resume_message("please continue", history)
    assert "please continue" in resumed
    assert "after" in resumed
    for command in TaskCommand:
        assert command.value not in resumed
        assert f"code reply {command.value}" not in resumed
    assert "已停止这一轮" not in resumed


def test_history_for_model_uses_typed_entry_kind_not_the_text():
    """A stored kind decides model history. Text that only looks like a command stays when it is a message."""
    history = [
        {"role": "user", "content": "/done", "entry_kind": "message"},
        {"role": "assistant", "content": "the model said done", "entry_kind": "message"},
        {"role": "user", "content": "not a command word", "entry_kind": "command"},
        {"role": "user", "content": "between"},
        {"role": "assistant", "content": "stored as the code reply", "entry_kind": "command_reply"},
        {"role": "user", "content": "after"},
    ]
    kept = history_for_model(history)
    assert [item["content"] for item in kept] == ["/done", "the model said done", "between", "after"]
    resumed = resume_message("next", history)
    assert "/done" in resumed
    assert "the model said done" in resumed
    assert "between" in resumed
    assert "after" in resumed
    assert "not a command word" not in resumed
    assert "stored as the code reply" not in resumed
    assert "entry_kind" not in resumed
