"""Lark's binary event protocol against a local platform, without a real account."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from lark_platform_fake import FakeLarkPlatform
from looper.agents.channels import lark_stream
from looper.agents.channels.lark_api import LarkApi, LarkApiError
from looper.agents.channels.lark_stream import Frame, SeenEvents, stream_events


@pytest.mark.parametrize("frame", [
    Frame(),
    Frame(seq_id=150, log_id=2**63, service=42, method=1,
          headers=[("type", "event"), ("message_id", "m1"), ("type", "duplicate")],
          payload_encoding="json", payload_type="application/json", payload=b"", log_id_new="log-new"),
    Frame(seq_id=2**64 - 1, service=-1, method=1, payload=b"\x00\xff" * 100_000),
    Frame(headers=[("type", "事件"), ("trace_id", "追踪")], payload="你好".encode()),
])
def test_frame_round_trip(frame: Frame) -> None:
    assert Frame.decode(frame.encode()) == frame
    assert frame.header("missing") is None
    if frame.headers:
        assert frame.header(frame.headers[0][0]) == frame.headers[0][1]


def test_frame_matches_protobuf_wire_fields() -> None:
    encoded = (b"\x08\x96\x01\x10\x02\x18\x03\x20\x01"
               b"\x2a\x0d\x0a\x04type\x12\x05event"
               b"\x32\x04json\x3a\x04text\x42\x02{}\x4a\x03log")
    assert Frame.decode(encoded) == Frame(
        seq_id=150, log_id=2, service=3, method=1, headers=[("type", "event")],
        payload_encoding="json", payload_type="text", payload=b"{}", log_id_new="log")


@pytest.mark.parametrize("unknown", [
    b"\x50\x96\x01", b"\x59abcdefgh", b"\x62\x03abc", b"\x6dabcd",
    b"\x53\x58\x01\x54",
])
def test_frame_skips_unknown_wire_fields(unknown: bytes) -> None:
    frame = Frame(method=1, payload=b"event")
    assert Frame.decode(unknown + frame.encode() + unknown) == frame


@pytest.mark.parametrize("garbage", [
    b"\x00", b"\x08", b"\x08\x80", b"\x08" + b"\x80" * 11,
    b"\x42\x04abc", b"\x59abc", b"\x6dabc", b"\x56", b"\x2a\x04\x0a\x04ab",
    b"", b"\x08\x00", b"\x0a\x01x", b"\x28\x00", b"\x30\x00",
    b"\x2a\x02\x08\x00", b"\x2a\x03\x0a\x01k", b"\x32\x01\xff",
    b"\x08" + b"\xff" * 9 + b"\x02", b"\x53", b"\x53\x5c",
])
def test_frame_rejects_garbage_and_truncated_fields(garbage: bytes) -> None:
    with pytest.raises(ValueError):
        Frame.decode(garbage)


def test_seen_events_remembers_both_ids_even_for_a_duplicate() -> None:
    seen = SeenEvents()
    assert seen.first("event-1", "message-1")
    assert not seen.first("event-1", "message-2")
    assert not seen.first("event-2", "message-2")
    assert not seen.first("event-2", "message-3")
    assert seen.first("event-3", "message-4")


def test_seen_events_is_bounded_and_recent_duplicates_refresh_lru() -> None:
    seen = SeenEvents()
    for number in range(1000):
        assert seen.first(f"id-{number}")
    assert not seen.first("id-0")
    assert seen.first("id-1000")
    assert not seen.first("id-0")
    assert seen.first("id-1")


@contextlib.asynccontextmanager
async def _stream(fake: FakeLarkPlatform, **kwargs: Any) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
    api = LarkApi(fake.app_id, "test-secret", base_url=fake.base_url)
    events = stream_events(api, **kwargs)
    try:
        yield events
    finally:
        await events.aclose()
        await api.aclose()


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def _event(*, event_id: str = "evt-1", message_id: str = "om_1", message_type: str = "text",
           content: str = '{"text":"hello"}') -> dict[str, Any]:
    return {"schema": "2.0", "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "ou_sender"}, "sender_type": "user"},
                      "message": {"message_id": message_id, "chat_id": "oc_chat", "chat_type": "p2p",
                                  "message_type": message_type, "content": content}}}


def _event_frame(event: dict[str, Any], *, message_id: str = "wire-1", seq: int = 0,
                 total: int = 1, payload: bytes | None = None) -> Frame:
    return Frame(seq_id=17, log_id=23, service=42, method=1,
                 headers=[("type", "event"), ("message_id", message_id), ("trace_id", "trace-1"),
                          ("sum", str(total)), ("seq", str(seq))],
                 payload=json.dumps(event).encode() if payload is None else payload)


def test_message_is_flattened_and_acked_while_consumer_is_paused() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            await fake.push_message(text="@_user_1 hi @_user_1", sender="ou_sender", chat_id="oc_group",
                                    chat_type="group", message_id="om_1", event_id="evt-1",
                                    mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Bot"}],
                                    parent_id="om_parent", root_id="om_root")
            event = await asyncio.wait_for(received, 2)
            await fake.wait_acks(1)
            assert event == {"type": "im.message.receive_v1", "event_id": "evt-1", "chat_id": "oc_group",
                             "chat_type": "group", "message_id": "om_1", "message_type": "text",
                             "sender_id": "ou_sender", "sender_type": "user",
                             "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": "Bot"}],
                             "content": "@Bot hi @Bot", "reply_to": "om_parent", "root_id": "om_root"}
            assert fake.acks[0][1] == {"code": 200}
            assert int(fake.ack_frames[0].header("biz_rt")) >= 0
            await fake.push_message(text="while consumer is paused", message_id="om_2", event_id="evt-2")
            await fake.wait_acks(2)
            assert (await anext(events))["content"] == "while consumer is paused"
    asyncio.run(main())


@pytest.mark.parametrize(("message_type", "content", "expected"), [
    ("text", "not JSON", ""), ("text", "[]", ""), ("text", "{}", ""),
    ("image", '{"image_key":"img_1"}', '{"image_key":"img_1"}'),
])
def test_content_fallback_and_absent_thread_fields(message_type: str, content: str, expected: str) -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            await fake.push_frame(_event_frame(_event(message_type=message_type, content=content)))
            event = await asyncio.wait_for(received, 2)
            await fake.wait_acks(1)
            assert event["content"] == expected
            assert "reply_to" not in event and "root_id" not in event
    asyncio.run(main())


def test_cards_other_events_and_duplicate_events_are_acked_without_yielding() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            first = asyncio.create_task(anext(events))
            await fake.push_message(text="first", event_id="evt-first", message_id="om_first")
            assert (await asyncio.wait_for(first, 2))["content"] == "first"
            next_event = asyncio.create_task(anext(events))
            await fake.push_frame(Frame(method=1, headers=[("type", "card"), ("message_id", "card-1")],
                                        payload=b"not an event"))
            await fake.push_frame(_event_frame({"header": {"event_type": "contact.user.updated_v3"}, "event": {}},
                                               message_id="other-1"))
            await fake.push_message(text="duplicate event", event_id="evt-first", message_id="om_changed")
            await fake.push_message(text="duplicate message", event_id="evt-changed", message_id="om_first")
            await fake.wait_acks(5)
            assert not next_event.done()
            await fake.push_message(text="next", event_id="evt-next", message_id="om_next")
            assert (await asyncio.wait_for(next_event, 2))["content"] == "next"
            await fake.wait_acks(6)
            assert all(payload == {"code": 200} for _, payload in fake.acks)
    asyncio.run(main())


def test_split_payload_acks_each_part_before_reassembly() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            payload = json.dumps(_event(content=json.dumps({"text": "你好" * 20})), ensure_ascii=False).encode()
            pieces = [payload[:19], payload[19:127], payload[127:]]
            for ack_count, index in enumerate((2, 0), start=1):
                await fake.push_frame(_event_frame({}, message_id="split-1", seq=index, total=3,
                                                   payload=pieces[index]))
                await fake.wait_acks(ack_count)
                assert not received.done()
            await fake.push_frame(_event_frame({}, message_id="split-1", seq=1, total=3, payload=pieces[1]))
            assert (await asyncio.wait_for(received, 2))["content"] == "你好" * 20
            await fake.wait_acks(3)
            assert [message_id for message_id, _ in fake.acks] == ["split-1"] * 3
            for frame in fake.ack_frames:
                assert (frame.seq_id, frame.log_id, frame.service, frame.method) == (17, 23, 42, 1)
                assert frame.header("trace_id") == "trace-1"
                assert int(frame.header("biz_rt")) >= 0
    asyncio.run(main())


def test_fake_split_message_reassembles_three_frames() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            await fake.push_message(text="split message", split=3)
            assert (await asyncio.wait_for(received, 2))["content"] == "split message"
            await fake.wait_acks(3)
    asyncio.run(main())


def test_client_sends_protocol_pings_and_applies_pong_config() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            fake.ping_interval = 0.04
            async with _stream(fake) as events:
                received = asyncio.create_task(anext(events))
                await _wait_until(lambda: fake.pings >= 1)
                await fake.push_frame(Frame(method=0, headers=[("type", "pong")],
                                            payload=json.dumps({"PingInterval": 0.4}).encode()))
                await fake.push_message(text="config processed")
                await asyncio.wait_for(received, 2)
                await fake.wait_acks(1)
                count = fake.pings
                received = asyncio.create_task(anext(events))
                try:
                    await asyncio.sleep(0.15)
                    assert fake.pings <= count + 1
                    await _wait_until(lambda: fake.pings > count + 1)
                finally:
                    received.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await received
    asyncio.run(main())


def test_ping_override_beats_server_interval() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake, ping_interval=0.05) as events:
            received = asyncio.create_task(anext(events))
            try:
                await _wait_until(lambda: fake.pings >= 2)
                assert not received.done()
                assert all((frame.seq_id, frame.log_id, frame.service, frame.method) == (0, 0, 7, 0)
                           and frame.header("type") == "ping" for frame in fake.ping_frames)
            finally:
                received.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await received
    asyncio.run(main())


def test_connection_drop_and_reconnect_keep_seen_ids() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            seen = SeenEvents()
            async with _stream(fake, seen=seen) as events:
                received = asyncio.create_task(anext(events))
                await fake.push_message(text="first", event_id="evt-first", message_id="om_first")
                assert (await asyncio.wait_for(received, 2))["content"] == "first"
                received = asyncio.create_task(anext(events))
                await fake.drop_connections()
                try:
                    await asyncio.wait_for(received, 2)
                except (ConnectionError, LarkApiError, StopAsyncIteration):
                    pass
                else:
                    pytest.fail("a dropped connection yielded a message")
            await _wait_until(lambda: not fake.clients)
            async with _stream(fake, seen=seen) as events:
                received = asyncio.create_task(anext(events))
                await fake.push_message(text="redelivery", event_id="evt-first", message_id="om_first")
                await fake.wait_acks(2)
                assert not received.done()
                await fake.push_message(text="second", event_id="evt-second", message_id="om_second")
                assert (await asyncio.wait_for(received, 2))["content"] == "second"
                assert fake.connections == 2
    asyncio.run(main())


def test_normal_server_close_finishes_iterator() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            await _wait_until(lambda: fake.connections == 1)
            await asyncio.gather(*(client.close(code=1000) for client in tuple(fake.clients)))
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(received, 2)
    asyncio.run(main())


def test_half_open_connection_without_frames_times_out() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            fake.respond_pings = False
            async with _stream(fake, ping_interval=0.04) as events:
                with pytest.raises(ConnectionError):
                    await asyncio.wait_for(anext(events), 1)
                assert fake.pings >= 1
    asyncio.run(main())


def test_endpoint_refusal_keeps_platform_code() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            fake.refuse_endpoint(10003, "invalid param")
            with pytest.raises(LarkApiError, match="invalid param") as caught:
                await anext(events)
            assert caught.value.code == 10003
            assert fake.connections == 0
    asyncio.run(main())


def test_cancellation_closes_socket_and_stops_ping_task() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake, ping_interval=0.04) as events:
            received = asyncio.create_task(anext(events))
            await _wait_until(lambda: fake.pings >= 1)
            received.cancel()
            with pytest.raises(asyncio.CancelledError):
                await received
            await _wait_until(lambda: not fake.clients)
            count = fake.pings
            await asyncio.sleep(0.12)
            assert fake.pings == count
    asyncio.run(main())


def test_stream_logs_never_contain_payload_secret_or_ticket(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="looper.agents.channels.lark_stream")
    caplog.set_level(logging.DEBUG, logger="websockets.client")

    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            await fake.push_message(text="private-message-sentinel")
            await asyncio.wait_for(received, 2)
            await fake.wait_acks(1)
            assert "test-secret" not in caplog.text
            assert fake.ws_url not in caplog.text
            assert "ticket=" not in caplog.text
            assert "private-message-sentinel" not in caplog.text
    asyncio.run(main())


def test_pending_fragment_groups_are_bounded() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            payload = json.dumps(_event()).encode()
            midpoint = len(payload) // 2
            for number in range(33):
                await fake.push_frame(_event_frame({}, message_id=f"partial-{number}", total=2,
                                                   payload=payload[:midpoint]))
            await fake.wait_acks(33)
            await fake.push_frame(_event_frame({}, message_id="partial-0", seq=1, total=2,
                                               payload=payload[midpoint:]))
            await fake.wait_acks(34)
            assert not received.done()
            await fake.push_message(text="fresh complete event")
            assert (await asyncio.wait_for(received, 2))["content"] == "fresh complete event"
    asyncio.run(main())


def test_malformed_event_payload_is_acked_and_does_not_block_later_messages() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            for index, payload in enumerate((b"not JSON", b"[]", b"\xff")):
                await fake.push_frame(_event_frame({}, message_id=f"malformed-{index}", payload=payload))
            await fake.wait_acks(3)
            assert not received.done()
            await fake.push_message(text="valid after malformed")
            assert (await asyncio.wait_for(received, 2))["content"] == "valid after malformed"
    asyncio.run(main())


def test_expired_fragment_group_cannot_reassemble(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(lark_stream, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            payload = json.dumps(_event()).encode()
            midpoint = len(payload) // 2
            await fake.push_frame(_event_frame({}, message_id="expired", total=2, payload=payload[:midpoint]))
            await fake.wait_acks(1)
            now[0] += 5.1
            await fake.push_frame(_event_frame({}, message_id="expired", seq=1, total=2,
                                               payload=payload[midpoint:]))
            await fake.wait_acks(2)
            assert not received.done()
            await fake.push_message(text="fresh after expired group")
            assert (await asyncio.wait_for(received, 2))["content"] == "fresh after expired group"
    asyncio.run(main())


def test_unknown_and_broken_frames_do_not_block_later_messages() -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake, _stream(fake) as events:
            received = asyncio.create_task(anext(events))
            await fake.wait_connections()
            for raw in ("text frame", b"\x08\x80", Frame(method=9).encode(),
                        Frame(headers=[("type", "unknown")]).encode(),
                        Frame(headers=[("type", "pong")], payload=b"invalid JSON").encode()):
                await asyncio.gather(*(client.send(raw) for client in tuple(fake.clients)))
            await fake.push_message(text="after ignored frames")
            assert (await asyncio.wait_for(received, 2))["content"] == "after ignored frames"
            await fake.wait_acks(1)
            assert len(fake.acks) == 1
    asyncio.run(main())
