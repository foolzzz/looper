"""Document the stand-in platform's lack of an offline event replay contract."""

import asyncio
import json

from lark_platform_fake import FakeLarkPlatform
from looper.agents.channels.lark_api import LarkApi
from looper.agents.channels.lark_stream import Frame, stream_events


def test_disconnected_platform_event_is_not_replayed_on_reconnect():
    async def run():
        async with FakeLarkPlatform() as platform:
            api = LarkApi(platform.app_id, "test-secret", base_url=platform.base_url)
            first = stream_events(api)
            second = None
            received = None
            try:
                received = asyncio.create_task(anext(first))
                await platform.push_message(text="before restart", message_id="om_before")
                assert (await asyncio.wait_for(received, 2))["content"] == "before restart"
                await first.aclose()
                async with asyncio.timeout(2):
                    async with platform._change:
                        await platform._change.wait_for(lambda: not platform.clients)
                offline = {"schema": "2.0", "header": {"event_id": "ev_gap",
                           "event_type": "im.message.receive_v1"}, "event": {
                               "sender": {"sender_id": {"open_id": "ou_sender"}, "sender_type": "user"},
                               "message": {"message_id": "om_gap", "chat_id": "oc_chat", "chat_type": "p2p",
                                           "message_type": "text", "content": json.dumps({"text": "during gap"})}}}
                await platform.push_frame(Frame(method=1, service=7, headers=[("type", "event"),
                    ("message_id", "om_gap"), ("sum", "1"), ("seq", "0")], payload=json.dumps(offline).encode()),
                    wait_for_connection=False)
                second = stream_events(api)
                received = asyncio.create_task(anext(second))
                await platform.wait_connections(2)
                await platform.push_message(text="after restart", message_id="om_after")
                assert (await asyncio.wait_for(received, 2))["content"] == "after restart"
            finally:
                if received is not None and not received.done():
                    received.cancel()
                    await asyncio.gather(received, return_exceptions=True)
                await first.aclose()
                if second is not None:
                    await second.aclose()
                await api.aclose()

    asyncio.run(asyncio.wait_for(run(), 10))
