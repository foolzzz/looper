"""Shared wire vectors, checked against both the schema and real asyncio pipes."""
import asyncio
import json
import os
from pathlib import Path
from typing import get_args, get_origin

import jsonschema
import pytest

from looper.agents.runtimes.runner import protocol as p

ROOT = Path(__file__).resolve().parents[1] / "runners/protocol"
SCHEMA = json.loads((ROOT / "messages.schema.json").read_text())
CASES = json.loads((ROOT / "cases.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def check_schema(case, value):
    errors = list(VALIDATOR.iter_errors(value))
    context = case.get("result_for")
    if context and "result" in value:
        shape = p.Turn if context["method"] == "session/close" and "trace_id" in context["params"] else p.RESULTS[context["method"]]
        names = [item.__name__ for item in get_args(shape)] if get_origin(shape) else [shape.__name__]
        spec = dict(SCHEMA, oneOf=[{"$ref": f"#/$defs/{name}"} for name in names])
        spec.pop("required", None)
        spec.pop("properties", None)
        errors += list(jsonschema.Draft202012Validator(spec).iter_errors(value["result"]))
    assert bool(errors) == (not case.get("schema_valid", "code" not in case)), case["name"]


async def through_pipe(frame):
    read_fd, write_fd = os.pipe()
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb"))

    def send():
        with os.fdopen(write_fd, "wb", buffering=0) as stream:
            try:
                # Split the JSON, any UTF-8 text, and the terminating LF across writes.
                for part in (frame[:1], frame[1:-1], frame[-1:]):
                    remaining = memoryview(part)
                    while remaining:
                        remaining = remaining[stream.write(remaining):]
            except BrokenPipeError:
                pass  # The consumer closes immediately on corrupt stdout.

    sender = asyncio.create_task(asyncio.to_thread(send))
    try:
        return await p.read_frame(reader)
    finally:
        transport.close()
        await sender


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_wire_contract(case):
    def exercise():
        if "versions" in case:
            return p.select_version(case["versions"])
        if "frame_bytes" in case:
            value = {"jsonrpc": "2.0", "id": "l-1", "result": {"text": ""}}
            overhead = len(json.dumps(value, separators=(",", ":")).encode())
            value["result"]["text"] = "x" * (case["frame_bytes"] - overhead)
            check_schema(dict(case, schema_valid=True), value)
            frame = json.dumps(value, separators=(",", ":")).encode() + b"\n"
            if "code" in case:
                with pytest.raises(p.ProtocolError):
                    p.encode_frame(value)
            else:
                assert p.encode_frame(value) == frame
        elif "message" in case:
            value = case["message"]
            check_schema(case, value)
            frame = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        else:
            frame = bytes.fromhex(case["raw_hex"]) if "raw_hex" in case else case["raw"].encode()
        message = asyncio.run(asyncio.wait_for(through_pipe(frame), 5))
        if "message" in case and "code" not in case:
            assert asyncio.run(through_pipe(p.encode_frame(message))) == message
        if "result_for" in case:
            context = case["result_for"]
            params = p.decode_params(context["method"], context["params"])
            result = p.decode_result(context["method"], message.result, params)
            if "handshake" in case:
                p.validate_initialize(result, **case["handshake"])
        if "error_for" in case:
            p.verify_error(message.error, case["error_for"])
        return message

    if "code" in case:
        with pytest.raises(p.ProtocolError) as error:
            exercise()
        assert error.value.code == p.ErrorCode[case["code"]]
    else:
        message = exercise()
        if case["name"] == "additive":
            assert "future" not in message.params
        if case["name"] == "new":
            assert "test-secret" not in repr(message) + repr(message.params) + repr(message.params["provider"])
        if case["name"] == "schema-preserved":
            assert message.params["tools"]["platform"][0]["inputSchema"] == case["message"]["params"]["tools"]["platform"][0]["inputSchema"]


def test_eof_between_frames():
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        with pytest.raises(EOFError):
            await p.read_frame(reader)
    asyncio.run(exercise())


def test_lf_arrives_after_json_and_unicode():
    async def exercise():
        reader = asyncio.StreamReader()
        frame = p.encode_frame(next(c["message"] for c in CASES if c["name"] == "prompt-unicode"))
        pending = asyncio.create_task(p.read_frame(reader))
        for chunk in (frame[:1], frame[1:-1]):
            reader.feed_data(chunk)
            await asyncio.sleep(0)
            assert not pending.done()
        reader.feed_data(b"\n")
        assert (await pending).params["text"] == "hello\u2028world\u2029!"
    asyncio.run(exercise())


@pytest.mark.parametrize("value", [float("nan"), "\ud800", object()])
def test_encoder_rejects_non_json(value):
    with pytest.raises(p.ProtocolError) as error:
        p.encode_frame({"jsonrpc": "2.0", "id": "l-1", "result": {"value": value}})
    assert error.value.code == p.ErrorCode.INVALID_REQUEST


def test_encoder_rejects_ambiguous_response():
    with pytest.raises(p.ProtocolError):
        p.encode_frame(p.Response("l-1", result={}, error={"code": -32603, "message": "error"}))
