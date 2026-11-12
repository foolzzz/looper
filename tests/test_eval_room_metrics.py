"""Room inbox usage and deliveries in prompt-regression reports."""

import json

import pytest

from evals.prompt_regression import compare, metrics


def _event(kind, *blocks):
    return {"event": {"type": kind, "message": {"content": list(blocks)}}}


def _inbox(call_id="inbox", name="looper_room_inbox"):
    return {"type": "tool_use", "id": call_id, "name": name}


def _result(content, call_id="inbox", **extra):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, **extra}


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("blocks", [False, True])
def test_inbox_calls_and_empty_results(wrapped, blocks):
    def content(messages):
        payload = {"messages": messages, "more": False, "unread": {}}
        text = json.dumps({"status": "ok", "result": payload} if wrapped else payload)
        return [{"type": "text", "text": text[:12]}, {"type": "text", "text": text[12:]}] if blocks else text

    events = [
        _event("assistant", _inbox("empty", "mcp__looper__looper_room_inbox")),
        _event("user", _result(content([]), "empty")),
        _event("assistant", _inbox("message"), {"type": "tool_use", "name": "looper_room_roster"}),
        _event("user", _result(content([{"id": 4, "body": "Hello"}]), "message")),
    ]
    result = metrics.room_metrics({}, [], {1: events})
    assert result["room_inbox_calls"] == 2
    assert result["room_inbox_empty"] == 1
    assert result["room_tool_calls"] == 3


@pytest.mark.parametrize("content", [
    "tool failed", "[]", "null", '"text"', '{"result": null}', '{"result": []}',
    '{}', '{"messages": null}', '{"messages": {}}', 123,
    [None, "text", {"text": 42}, {"type": "image"}],
])
def test_malformed_inbox_results_are_not_empty(content):
    events = [_event("assistant", _inbox()), _event("user", _result(content))]
    result = metrics.room_metrics({}, [], {1: events})
    assert result["room_inbox_calls"] == 1
    assert result["room_inbox_empty"] == 0


def test_only_successful_results_of_preceding_inbox_calls_in_the_same_record_count():
    empty = '{"messages": []}'
    events = [
        _event("user", _result(empty)),
        _event("assistant", _inbox(), _inbox("read", "looper_room_read")),
        _event("user", _result(empty, is_error=True), _result(empty, "read"), _result(empty, "unknown")),
    ]
    result = metrics.room_metrics({}, [], {1: events, 2: [_event("user", _result(empty))]})
    assert result["room_inbox_calls"] == 1
    assert result["room_inbox_empty"] == 0


def test_malformed_event_blocks_are_skipped():
    events = [None, {"event": None}, {"type": "assistant", "message": "text"},
              {"type": "assistant", "message": {"content": 42}},
              _event("assistant", None, "text", _inbox([])),
              _event("user", None, _result('{"messages": []}', []))]
    result = metrics.room_metrics({}, [], {1: events})
    assert result["room_inbox_calls"] == 1
    assert result["room_inbox_empty"] == 0


def test_room_deliveries_and_reminders_are_counted_separately():
    activity = [{"summary": summary} for summary in
                ("Room messages delivered: #4", "Room reminder sent", "Other activity", None)]
    result = metrics.room_metrics({}, iter(activity), {1: []})
    assert result["room_deliveries"] == 1
    assert result["room_reminders"] == 1
    assert result["room_inbox_calls"] == result["room_inbox_empty"] == 0
    assert all(value == 0 for value in metrics.room_metrics({}, [], {}).values())


def test_comparison_renders_room_metrics_beside_an_older_baseline():
    row = {"id": "room_interface", "outcome": "pass"}
    counted = {"room_deliveries": 1, "room_inbox_calls": 2, "room_inbox_empty": 1}
    base = {"tasks": [row]}
    new = {"tasks": [{**row, **counted}]}
    table = compare.markdown(new, base)
    lines = [line for line in table.splitlines() if line.startswith("|")]
    headers = [cell.strip() for cell in lines[0].split("|")[1:-1]]
    for line in lines[2:]:
        cells = dict(zip(headers, (cell.strip() for cell in line.split("|")[1:-1])))
        assert cells["room deliveries"] == "– → **1.0**"
        assert cells["room inbox calls"] == "– → **2.0**"
        assert cells["empty room inbox calls"] == "– → **1.0**"
