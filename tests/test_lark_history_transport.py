"""CLI history and readiness contracts, without contacting Lark."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from looper.agents.channels import lark_cli
from looper.agents.channels.lark_inbox import history_event
from looper.agents.channels.lark_transport import CliTransport


@pytest.mark.parametrize("exit_code", [0, 1])
def test_search_preflight_preserves_authorization_type_without_api_code(monkeypatch, exit_code):
    async def run():
        payload = json.dumps({"ok": False, "error": {
            "type": "authorization", "subtype": "missing_scope", "message": "access denied",
            "missing_scopes": ["search:message", "im:message.reactions:read"]}}).encode()

        async def communicate():
            return (payload, b"") if not exit_code else (b"", payload)

        async def spawn(*args, **kwargs):
            return SimpleNamespace(returncode=exit_code, communicate=communicate)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(lark_cli.LarkCliError) as caught:
            await lark_cli.run_json(["lark-cli", "im", "+messages-search"])
        assert caught.value.error_type == "authorization"
        assert caught.value.code is None
        assert "search:message" in str(caught.value)
        assert "im:message.reactions:read" in str(caught.value)

    asyncio.run(asyncio.wait_for(run(), 3))


def test_missing_raw_thread_message_is_not_admitted_as_a_manager_message(monkeypatch):
    async def run_json(argv):
        if "+threads-messages-list" in argv:
            return {"messages": [{"message_id": "om_answer", "thread_id": "omt_gate", "msg_type": "text"}],
                    "has_more": False}
        return {"data": {"items": []}}

    monkeypatch.setattr(lark_cli, "run_json", run_json)

    async def run():
        transport = CliTransport()
        with pytest.raises(lark_cli.LarkCliError, match="reply references"):
            await transport.list_thread_messages("omt_gate")

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("mode", ["group", "p2p"])
def test_history_uses_bot_profile_bounded_page_and_flat_event(monkeypatch, mode):
    calls = []
    message = {"message_id": "om_example", "msg_type": "file", "content": '<file key="file_example" name="note.txt"/>',
               "sender": {"id": "ou_example", "sender_type": "user"},
               "mentions": [{"id": "ou_examplebot", "key": "@_user_1", "name": "Helper"}],
               "parent_id": "om_parent", "root_id": "om_root", "create_time": "1700000000000"}

    async def run_json(argv):
        calls.append(argv)
        if "api" in argv:
            return {"data": {"chat_mode": mode}}
        return {"messages": [message], "has_more": True, "page_token": "next"}

    monkeypatch.setattr(lark_cli, "run_json", run_json)

    async def run():
        transport = CliTransport("example-profile")
        page = await transport.list_messages("oc_example", start=1700000000, end=1700000060, page_token="previous")
        assert page["chat_type"] == mode
        event = history_event(page["messages"][0], "oc_example", page["chat_type"])
        assert event == {"type": lark_cli.RECEIVE_EVENT, "chat_id": "oc_example", "chat_type": mode,
                         "message_id": "om_example", "message_type": "file", "sender_id": "ou_example",
                         "sender_type": "user", "mentions": message["mentions"], "content": message["content"],
                         "reply_to": "om_parent", "root_id": "om_root"}
        argv = calls[-1]
        assert argv[:7] == ["lark-cli", "--profile", "example-profile", "im", "+chat-messages-list", "--as", "bot"]
        assert argv[argv.index("--chat-id") + 1] == "oc_example"
        assert argv[argv.index("--order") + 1] == "asc"
        assert argv[argv.index("--page-size") + 1] == "50"
        assert argv[argv.index("--page-token") + 1] == "previous"
        assert datetime.fromisoformat(argv[argv.index("--start") + 1]).timestamp() == 1700000000
        assert datetime.fromisoformat(argv[argv.index("--end") + 1]).timestamp() == 1700000060
        assert "--page-all" not in argv and "--no-reactions" in argv
        if mode == "p2p":
            before = len(calls)
            assert await transport.list_threads("oc_example") == {"threads": [], "has_more": False}
            assert len(calls) == before
        await transport.aclose()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_consumer_waits_for_subscription_ready_marker(monkeypatch):
    async def run():
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        spawned = asyncio.Event()
        process = SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)

        async def spawn(*args, **kwargs):
            spawned.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        transport = CliTransport()
        stream = transport.events()
        pending = asyncio.create_task(anext(stream))
        try:
            await spawned.wait()
            assert not transport.consumer_started.is_set()
            stderr.feed_data(b"[event] ready event_key=im.message.receive_v1\n")
            await asyncio.wait_for(transport.consumer_started.wait(), 1)
            stdout.feed_data(b'{"type":"im.message.receive_v1","message_id":"om_example"}\n')
            assert (await pending)["message_id"] == "om_example"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
        assert not transport.consumer_started.is_set()

    asyncio.run(asyncio.wait_for(run(), 3))
