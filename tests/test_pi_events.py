"""Pi JSON translation, using synthetic, public-safe messages only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.runtimes.pi import PiRuntime, PiStream, edited_paths
from looper.agents.run_usage import Usage, compaction, request_context, run_usage
from looper.agents.runtimes.pricing import event_usage

PI = PiRuntime()
FIXTURES = Path(__file__).parent / "fixtures" / "pi_json"


def _wire(*records: Any, newline: bytes = b"\n") -> bytes:
    return newline.join(json.dumps(record, ensure_ascii=False).encode() for record in records) + newline


def _assistant(text: str = "Done.", stop: str = "stop", **fields: Any) -> dict[str, Any]:
    return {"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}],
        "model": "test-model", "stopReason": stop, **fields}}


SETTLED = {"type": "agent_settled", "aborted": False}


async def _read(data: bytes, *, chunk_size: int = 4093) -> list[dict[str, Any]]:
    # Exercise real chunk boundaries, including split UTF-8 sequences and large records.
    reader = asyncio.StreamReader(limit=64)

    async def feed() -> None:
        for start in range(0, len(data), chunk_size):
            reader.feed_data(data[start:start + chunk_size])
            await asyncio.sleep(0)
        reader.feed_eof()

    feeder = asyncio.create_task(feed())
    try:
        return [event async for event in PI.parse(reader, model="configured-model")]
    finally:
        await feeder


def _parse(data: bytes, **kwargs: Any) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        async with asyncio.timeout(5):
            return await _read(data, **kwargs)

    return asyncio.run(run())


def _fixture(name: str) -> list[dict[str, Any]]:
    return _parse((FIXTURES / f"{name}.jsonl").read_bytes())


def _results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "result"]


def _blocks(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [block for event in events if event["type"] in ("assistant", "user")
            for block in event["message"]["content"] if block["type"] == kind]


def test_tool_round_trip_uses_final_messages_and_the_same_call_id() -> None:
    events = _fixture("tools")
    assert events[0] == {"type": "system", "subtype": "init", "session_id": "test-session",
                         "runtime": "pi", "model": "configured-model", "cwd": "workspace"}
    assert all(event.get("session_id") == "test-session" for event in events)
    assert _blocks(events, "text") == [
        {"type": "text", "text": "Checking the file."}, {"type": "text", "text": "Done."}]
    assert _blocks(events, "thinking") == [{"type": "thinking", "thinking": "Read before editing."}]
    assert _blocks(events, "tool_use") == [
        {"type": "tool_use", "id": "call-read", "name": "read", "input": {"path": "sample.txt"}}]
    assert _blocks(events, "tool_result") == [
        {"type": "tool_result", "tool_use_id": "call-read", "content": "first line\nsecond line",
         "is_error": False}]
    assistants = [event for event in events if event["type"] == "assistant"]
    assert len(assistants) == 2
    assert all(event["message"]["model"] == "test-model" for event in assistants)
    assert all(event["parent_tool_use_id"] is None for event in assistants)
    result, = _results(events)
    assert result["subtype"] == "success" and result["is_error"] is False
    assert result["result"] == "Done."


def test_agent_end_and_retry_do_not_finish_the_run() -> None:
    stream = PiStream()
    records = [json.loads(line) for line in (FIXTURES / "retry.jsonl").read_bytes().split(b"\n") if line]
    events = []
    for record in records[:-1]:
        events.extend(stream.translate(record))
        assert _results(events) == []
    result, = stream.translate(records[-1])
    assert result["subtype"] == "success" and result["result"] == "Recovered."
    assert stream.translate(records[-1]) == []


@pytest.mark.parametrize("content", [[], [{"type": "text", "text": ""}],
                                      [{"type": "thinking", "thinking": "No text answer."}]])
def test_empty_final_assistant_does_not_reuse_progress(content: list[dict[str, Any]]) -> None:
    events = _parse(_wire(_assistant("Progress."), _assistant(content=content), SETTLED))
    result, = _results(events)
    assert result["is_error"] is False and result["result"] == ""
    assert [event for event in events if event["type"] == "assistant"][-1]["message"]["content"] == content


def test_final_answer_is_only_the_last_assistant_text() -> None:
    events = _parse(_wire(_assistant("First progress."), _assistant("Second progress."),
                          _assistant(content=[{"type": "text", "text": "Final one."},
                                              {"type": "text", "text": "Final two."}]), SETTLED))
    result, = _results(events)
    assert result["result"] == "Final one.\nFinal two."
    assert len([event for event in events if event["type"] == "assistant"]) == 3


@pytest.mark.parametrize("stop", ["error", "aborted", "length", "deferred", "pending", "toolUse", "unknown", None])
def test_unfinished_or_failed_assistant_cannot_be_success(stop: str | None) -> None:
    events = _parse(_wire(_assistant("Incomplete.", stop=stop), SETTLED))
    result, = _results(events)
    assert result["is_error"] is True and result["subtype"] == "error_during_execution"
    assert result["result"] == "" and result["errors"]


def test_failed_and_interrupted_fixtures_report_failure() -> None:
    failed, = _results(_fixture("failed"))
    assert failed["is_error"] is True and failed["result"] == ""
    assert "Synthetic provider failure." in failed["errors"]
    interrupted, = _results(_fixture("interrupted"))
    assert interrupted["is_error"] is True and interrupted["result"] == ""


@pytest.mark.parametrize("stop", ["error", "aborted", "length", "deferred"])
def test_a_later_success_recovers_from_an_earlier_failure(stop: str) -> None:
    events = _parse(_wire(_assistant("", stop, errorMessage="Synthetic failure."),
                          {"type": "agent_end", "willRetry": True},
                          _assistant("Recovered."), SETTLED))
    result, = _results(events)
    assert result["is_error"] is False and result["result"] == "Recovered."
    assert "errors" not in result


def test_settled_without_an_assistant_is_not_success() -> None:
    result, = _results(_parse(_wire(SETTLED)))
    assert result["is_error"] is True and result["errors"]


def test_settled_is_terminal_and_emits_exactly_one_result() -> None:
    events = _parse(_wire(_assistant("Final."), SETTLED, SETTLED, _assistant("Late."),
                          {"type": "agent_settled", "aborted": True}))
    result, = _results(events)
    assert result["result"] == "Final." and result["is_error"] is False
    assert _blocks(events, "text") == [{"type": "text", "text": "Final."}]


@pytest.mark.parametrize("tail", [b"", b'{"type":"agent_settled"', b'\xff\xfe', b'null\n', b'[]\n'])
def test_eof_without_settled_never_invents_a_result(tail: bytes) -> None:
    events = _parse(_wire(_assistant(), {"type": "agent_end", "willRetry": False}) + tail)
    assert _blocks(events, "text") == [{"type": "text", "text": "Done."}]
    assert _results(events) == []


@pytest.mark.parametrize("broken", [
    b'{"type":\n', b'\xff\n', b'null\n', b'[]\n', b'{}\n', b'{"type":[]}\n', b'{"type":{}}\n',
    b'{"type":"message_end","message":null}\n',
    _wire(_assistant(content=None)), _wire(_assistant(content="invalid assistant content")),
])
def test_invalid_record_before_settled_cannot_hide_a_lost_message(broken: bytes) -> None:
    events = _parse(_wire(_assistant("Progress.")) + broken + _wire(SETTLED))
    result, = _results(events)
    assert result["is_error"] is True and result["result"] == ""
    assert any(event.get("subtype") == "pi_event" for event in events)


@pytest.mark.parametrize("aborted", [None, 0, "false", [], {}])
def test_a_malformed_settled_abort_flag_cannot_be_success(aborted: Any) -> None:
    result, = _results(_parse(_wire(_assistant(), {"type": "agent_settled", "aborted": aborted})))
    assert result["is_error"] is True and result["result"] == ""


def test_reader_failure_propagates_without_a_result() -> None:
    async def run() -> list[dict[str, Any]]:
        reader = asyncio.StreamReader()
        reader.feed_data(_wire(_assistant()))
        output = []
        with pytest.raises(ConnectionError, match="Synthetic pipe failure"):
            async for event in PI.parse(reader):
                output.append(event)
                reader.set_exception(ConnectionError("Synthetic pipe failure"))
        return output

    assert _results(asyncio.run(asyncio.wait_for(run(), 5))) == []


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_unicode_separators_crlf_and_large_lines_do_not_split_records(newline: bytes) -> None:
    text = "你好🙂\u2028middle\u2029end\n" + "界" * 100_000
    data = _wire(_assistant(text), SETTLED, newline=newline)
    assert b"\xe2\x80\xa8" in data and b"\xe2\x80\xa9" in data
    events = _parse(data)
    assert _blocks(events, "text") == [{"type": "text", "text": text}]
    result, = _results(events)
    assert result["result"] == text and result["is_error"] is False


def test_unicode_can_cross_every_byte_boundary_and_final_record_can_lack_lf() -> None:
    text = "字🙂\u2028\u2029"
    events = _parse(_wire(_assistant(text), SETTLED).removesuffix(b"\n"), chunk_size=1)
    result, = _results(events)
    assert result["result"] == text


def test_tool_failure_preserves_the_call_id_and_error_flag() -> None:
    events = _parse(_wire({"type": "message_end", "message": {
        "role": "toolResult", "toolCallId": "call-failed", "toolName": "read", "isError": True,
        "content": [{"type": "text", "text": "Synthetic missing file."}]}}))
    block, = _blocks(events, "tool_result")
    assert block == {"type": "tool_result", "tool_use_id": "call-failed",
                     "content": "Synthetic missing file.", "is_error": True}
    assert _results(events) == []


def test_unknown_events_preserve_structure_and_numeric_diagnostics_but_mask_opaque_strings() -> None:
    raw = {"type": "future_event", "attempt": 2, "success": False,
           "payload": {"apiKey": "synthetic-sensitive-value", "path": "/synthetic/private/file",
                       "details": ["opaque diagnostic", {"authorization": "Bearer synthetic-value"}]}}
    stream = PiStream(session_id="test-session")
    event, = stream.translate(raw)
    assert (event["type"], event["subtype"], event["session_id"]) == ("system", "pi_event", "test-session")
    diagnostic = event["event"]
    assert (diagnostic["type"], diagnostic["attempt"], diagnostic["success"]) == ("future_event", 2, False)
    assert diagnostic["payload"] == {"apiKey": "***", "path": "***",
                                      "details": ["***", {"authorization": "***"}]}
    assert raw["payload"]["apiKey"] == "synthetic-sensitive-value"


def test_unknown_message_roles_are_kept_as_redacted_diagnostics() -> None:
    event, = PiStream().translate({"type": "message_end", "message": {
        "role": "custom", "content": "opaque extension content"}})
    assert event["subtype"] == "pi_event"
    assert "opaque extension content" not in json.dumps(event)


def test_message_model_falls_back_to_the_configured_model() -> None:
    events = _parse(_wire(_assistant(model=None), SETTLED))
    assert events[0]["message"]["model"] == "configured-model"


def test_usage_counts_final_requests_nested_work_and_compaction_even_when_cancelled() -> None:
    events = _fixture("compaction")
    usage = [event_usage(event) for event in events if event["type"] == "usage"]
    assert usage == [
        ("test-model", Usage(input_tokens=100, output_tokens=10, cache_read_tokens=40, cache_creation_tokens=8)),
        ("configured-model", Usage(input_tokens=20, output_tokens=3, cache_read_tokens=5, cache_creation_tokens=2)),
        ("configured-model", Usage(input_tokens=30, output_tokens=4, cache_read_tokens=10, cache_creation_tokens=1)),
        ("test-model", Usage(input_tokens=50, output_tokens=6, cache_read_tokens=20, cache_creation_tokens=0)),
        ("test-model", Usage(input_tokens=7, output_tokens=2, cache_read_tokens=3, cache_creation_tokens=0)),
    ]
    result, = _results(events)
    assert result["is_error"] is True
    assert result["modelUsage"] == {
        "test-model": {"inputTokens": 157, "outputTokens": 18, "cacheReadInputTokens": 63,
                       "cacheCreationInputTokens": 8},
        "configured-model": {"inputTokens": 50, "outputTokens": 7, "cacheReadInputTokens": 15,
                             "cacheCreationInputTokens": 3},
    }
    total = Usage()
    for _, tokens in usage:
        total = total.plus(tokens)
    assert run_usage(result) == total == Usage(input_tokens=207, output_tokens=25,
                                               cache_read_tokens=78, cache_creation_tokens=11)
    assert [compaction(event) for event in events if compaction(event) is not None] == [
        {"trigger": "auto", "pre_tokens": 150, "post_tokens": 32}]
    diagnostics = [event["event"] for event in events if event.get("subtype") == "pi_event"
                   and event["event"]["type"] == "compaction_end"]
    assert [event["aborted"] for event in diagnostics] == [False, False, True]
    assert diagnostics[0]["willRetry"] is True
    assert "summary" not in json.dumps([event for event in events if event.get("subtype") == "compact_boundary"])


@pytest.mark.parametrize("value", [None, -1, True, False, "12", 1.5, [], {}, float("inf"), float("nan")])
def test_invalid_usage_counts_cannot_become_negative_or_inflate_output(value: Any) -> None:
    events = _parse(_wire(_assistant(usage={"input": value, "output": value, "cacheRead": value,
                                           "cacheWrite": value, "reasoning": 100, "totalTokens": 100}), SETTLED))
    usage, = [event for event in events if event["type"] == "usage"]
    assert event_usage(usage)[1] == Usage(input_tokens=0, output_tokens=0,
                                         cache_read_tokens=0, cache_creation_tokens=0)
    assert run_usage(_results(events)[0]) == event_usage(usage)[1]


def test_missing_usage_is_not_invented_and_partial_usage_keeps_reported_counts() -> None:
    events = _parse(_wire(_assistant(), _assistant(usage=None),
                          _assistant(usage={"input": 2, "output": 0}), SETTLED))
    usage, = [event for event in events if event["type"] == "usage"]
    assert event_usage(usage)[1] == Usage(input_tokens=2, output_tokens=0,
                                         cache_read_tokens=0, cache_creation_tokens=0)
    assert run_usage(_results(events)[0]) == event_usage(usage)[1]
    assistants = [event for event in events if event["type"] == "assistant"]
    assert request_context(assistants[0]) is None
    assert request_context(assistants[-1]) == 2


@pytest.mark.parametrize("reason", ["manual", "threshold", "overflow"])
def test_successful_compaction_maps_trigger_and_context_without_assistant_output(reason: str) -> None:
    events = _parse(_wire(_assistant("Final."), {"type": "compaction_end", "reason": reason,
                          "result": {"tokensBefore": 100, "estimatedTokensAfter": 20},
                          "aborted": False, "willRetry": False}, SETTLED))
    boundary, = [event for event in events if event.get("subtype") == "compact_boundary"]
    assert compaction(boundary) == {"trigger": "manual" if reason == "manual" else "auto",
                                    "pre_tokens": 100, "post_tokens": 20}
    assert _results(events)[0]["result"] == "Final."
    assert not [event for event in events if event["type"] == "usage"]


@pytest.mark.parametrize("fields", [
    {"aborted": True, "result": {"usage": {"input": 100}}},
    {"aborted": False, "errorMessage": "Synthetic failure."},
    {"aborted": False, "result": None, "willRetry": True},
    {"result": {}}, {"aborted": "false", "result": {}},
])
def test_failed_aborted_or_malformed_compaction_only_keeps_diagnostics(fields: dict[str, Any]) -> None:
    event, = PiStream().translate({"type": "compaction_end", "reason": "overflow", **fields})
    assert event["subtype"] == "pi_event"


def test_omp_compaction_is_not_a_pi_boundary_or_usage_source() -> None:
    event, = PiStream().translate({"type": "auto_compaction_end", "aborted": False,
                                  "result": {"tokensBefore": 100, "usage": {"input": 99}}})
    assert event["subtype"] == "pi_event"


def test_process_usage_does_not_import_session_totals_or_leak_to_the_next_process() -> None:
    data = _wire({"type": "session", "id": "resumed-session", "usage": {"input": 9999}},
                 _assistant(usage={"input": 2, "output": 1}),
                 {"type": "agent_end", "messages": [_assistant(usage={"input": 9999})["message"]]}, SETTLED)
    for _ in range(2):
        events = _parse(data)
        assert run_usage(_results(events)[0]).input_tokens == 2
        assert len([event for event in events if event["type"] == "usage"]) == 1


@pytest.mark.parametrize(("name", "arguments", "summary", "paths"), [
    ("read", {"path": "sample.txt"}, "Reading: sample.txt", []),
    ("write", {"path": "sample.txt", "content": "new"}, "Writing: sample.txt", ["sample.txt"]),
    ("edit", {"path": "sample.txt", "oldText": "old", "newText": "new"}, "Editing: sample.txt", ["sample.txt"]),
    ("edit", {"input": "[other.py#TAG]\n+change"}, "Editing: ?", []),
    ("ast_edit", {"paths": ["other.py"]}, "Tool: ast_edit", []),
    ("bash", {"command": "echo synthetic"}, "Running: echo synthetic", []),
    ("grep", {"pattern": "example"}, "grep: example", []),
    ("find", {"pattern": "*.py"}, "find: *.py", []),
    ("ls", {"path": "src"}, "Listing: src", []),
    ("mcp__looper__test", {}, "Tool: mcp__looper__test", []),
    ("edit", {"path": None}, "Editing: ?", []),
    ("write", {"path": ["bad"]}, "Writing: ?", []),
])
def test_tool_summary_and_edited_paths_use_pi_arguments(
    name: str, arguments: dict[str, Any], summary: str, paths: list[str],
) -> None:
    assert PI.tool_summary(name, arguments) == summary
    assert edited_paths(name, arguments) == paths


@pytest.mark.parametrize("name", ["write", "edit"])
@pytest.mark.parametrize("path", ["", "agent://message", None, 3, True])
def test_non_file_targets_are_not_edited_paths(name: str, path: Any) -> None:
    assert edited_paths(name, {"path": path}) == []
