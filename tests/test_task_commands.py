"""Whole-message task commands parse, and a sentence that only contains one does not."""

from looper.agents.task_commands import (
    DIRECT, ORCHESTRATED, STARTING, TaskCommand, delivery_route, history_for_model, parse,
)
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
