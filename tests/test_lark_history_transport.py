"""CLI history and readiness contracts, without contacting Lark."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from looper.agents.channels import lark_cli
from looper.agents.channels.lark_inbox import history_event
from looper.agents.channels.lark_transport import CliTransport


@pytest.mark.parametrize("exit_code", [0, 1])
def test_history_preserves_authorization_type_without_api_code(monkeypatch, exit_code):
    async def run():
        payload = json.dumps({"ok": False, "error": {
            "type": "authorization", "subtype": "missing_scope", "message": "access denied",
            "missing_scopes": ["im:message:readonly"]}}).encode()

        async def communicate():
            return (payload, b"") if not exit_code else (b"", payload)

        async def spawn(*args, **kwargs):
            return SimpleNamespace(returncode=exit_code, communicate=communicate)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(lark_cli.LarkCliError) as caught:
            await lark_cli.run_json(lark_cli.history_command("oc_example", start=1, end=2))
        assert caught.value.error_type == "authorization"
        assert caught.value.code is None
        assert "im:message:readonly" in str(caught.value)

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("mode", ["group", "p2p"])
def test_history_uses_bot_profile_bounded_raw_page_and_flat_event(monkeypatch, mode):
    calls = []
    message = {"message_id": "om_example", "msg_type": "file",
               "body": {"content": json.dumps({"file_key": "file_example", "file_name": "note.txt"})},
               "sender": {"id": "ou_example", "sender_type": "user"},
               "mentions": [{"id": "ou_examplebot", "key": "@_user_1", "name": "Helper"}],
               "parent_id": "om_parent", "root_id": "om_root", "thread_id": "omt_gate",
               "create_time": "1700000000000"}

    async def run_json(argv):
        calls.append(argv)
        if argv[argv.index("GET") + 1].startswith("/open-apis/im/v1/chats/"):
            return {"data": {"chat_mode": mode}}
        assert argv[argv.index("GET") + 1] == "/open-apis/im/v1/messages"
        return {"data": {"items": [message], "has_more": True, "page_token": "next"}}

    monkeypatch.setattr(lark_cli, "run_json", run_json)

    async def run():
        transport = CliTransport("example-profile")
        page = await transport.list_messages("oc_example", start=1700000000.2, end=1700000060.2,
                                             page_token="previous")
        assert page["chat_type"] == mode
        assert page["has_more"] and page["page_token"] == "next"
        assert page["messages"][0]["thread_id"] == "omt_gate"
        event = history_event(page["messages"][0], "oc_example", page["chat_type"])
        assert event == {"type": lark_cli.RECEIVE_EVENT, "chat_id": "oc_example", "chat_type": mode,
                         "message_id": "om_example", "message_type": "file", "sender_id": "ou_example",
                         "sender_type": "user", "mentions": message["mentions"],
                         "content": '<file key="file_example" name="note.txt"/>',
                         "reply_to": "om_parent", "root_id": "om_root", "thread_id": "omt_gate"}
        argv = calls[-1]
        assert argv[:8] == ["lark-cli", "--profile", "example-profile", "api", "GET",
                           "/open-apis/im/v1/messages", "--as", "bot"]
        assert json.loads(argv[argv.index("--params") + 1]) == {
            "container_id_type": "chat", "container_id": "oc_example", "sort_type": "ByCreateTimeAsc",
            "start_time": "1700000000", "end_time": "1700000061", "page_size": 50, "page_token": "previous"}
        assert len(calls) == 2  # No search or per-message reference lookup.
        if mode == "p2p":
            before = len(calls)
            assert await transport.list_threads("oc_example") == {"roots": [], "has_more": False}
            assert len(calls) == before
        await transport.aclose()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_thread_history_preserves_raw_references_without_search_permissions(monkeypatch):
    calls = []

    async def run_json(argv):
        calls.append(argv)
        assert "+messages-search" not in argv
        assert argv[1:6] == ["api", "GET", "/open-apis/im/v1/messages", "--as", "bot"]
        assert json.loads(argv[argv.index("--params") + 1]) == {
            "container_id_type": "thread", "container_id": "omt_gate", "sort_type": "ByCreateTimeDesc",
            "page_size": 50, "page_token": "next"}
        return {"data": {"items": [{"message_id": "om_answer", "msg_type": "text",
            "body": {"content": '{"text":"@_user_1 agreed"}'},
            "sender": {"id": "ou_example", "sender_type": "user"},
            "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": "Helper"}],
            "parent_id": "om_question", "root_id": "om_gate", "thread_id": "omt_gate"}], "has_more": False}}

    monkeypatch.setattr(lark_cli, "run_json", run_json)

    async def run():
        page = await CliTransport().list_thread_messages("omt_gate", page_token="next")
        event = history_event(page["messages"][0], "oc_chat", page["chat_type"])
        assert event["content"] == "@Helper agreed"
        assert event["reply_to"] == "om_question" and event["root_id"] == "om_gate"
        assert len(calls) == 1

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("kind, body, rendered", [
    ("image", {"image_key": "img_example"}, "[Image: img_example]"),
    ("post", {"title": "Title", "content": [[{"tag": "text", "text": "Body"}]]}, "Title\nBody"),
])
def test_raw_history_rich_content_uses_the_live_renderer(kind, body, rendered):
    message = {"message_id": "om_example", "msg_type": kind, "body": {"content": json.dumps(body)}}
    assert CliTransport._history_message(message)["content"] == rendered


@pytest.mark.parametrize("bad_content", ["{", "[]", '{"text":123}'])
def test_malformed_history_text_does_not_block_valid_messages(monkeypatch, bad_content):
    async def run_json(argv):
        return {"data": {"items": [
            {"message_id": "om_bad", "msg_type": "text", "body": {"content": bad_content}},
            {"message_id": "om_good", "msg_type": "text", "body": {"content": '{"text":"valid answer"}'}},
        ], "has_more": False}}

    monkeypatch.setattr(lark_cli, "run_json", run_json)

    async def run():
        page = await CliTransport().list_thread_messages("omt_gate")
        assert [(message["message_id"], message["content"]) for message in page["messages"]] == [
            ("om_bad", ""), ("om_good", "valid answer")]

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
