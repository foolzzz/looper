"""Whole-message task commands parse, and a sentence that only contains one does not."""

from looper.agents.task_commands import TaskCommand, parse


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
