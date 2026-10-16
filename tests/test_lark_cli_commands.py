"""The Lark (lark-cli) channel's lark-cli calls, as lark-cli 1.0.97 takes them: a stand-in ``lark-cli`` on PATH
answers from a small world of profiles, users and events (and anything else as lark-cli does, with a JSON error on
stderr and a non-zero exit), so nothing reaches a real chat, user or account."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from lark_cli_stand_in import BOT_OPEN_ID, OPEN_ID, OTHER
from looper.agents.bus import MessageBus
from looper.agents.channels import lark_cli, lark_settings
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_cli import (
    LarkCliError, bot_info_command, consume_command, reaction_command, reply_command, search_command, send_command,
    whoami_command,
)
from looper.agents.message import MessageEnvelope, MessageType, channel_address


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _adapter(tmp_path: Path, profile: str = "", **kwargs: Any) -> LarkChannelAdapter:
    return LarkChannelAdapter(profile=profile, runtime_root=tmp_path, bot_open_id=BOT_OPEN_ID,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})), **kwargs)


@pytest.mark.parametrize("profile", ["", "work"])
def test_every_command_carries_the_profile_when_there_is_one(profile: str) -> None:
    head = ["lark-cli", *(["--profile", profile] if profile else [])]
    assert send_command(chat_id="oc_1", markdown="**hi**", profile=profile) == [
        *head, "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown", "**hi**"]
    assert send_command(user_id=OPEN_ID, text="hi", profile=profile)[len(head) + 4:] == [
        "--user-id", OPEN_ID, "--text", "hi"]
    assert reply_command("om_1", markdown="**hi**", profile=profile) == [
        *head, "im", "+messages-reply", "--as", "bot", "--message-id", "om_1", "--markdown", "**hi**"]
    assert consume_command(profile) == [*head, "event", "consume", "im.message.receive_v1", "--as", "bot"]
    assert whoami_command(profile) == [*head, "whoami"]
    assert bot_info_command(profile) == [*head, "api", "GET", "/open-apis/bot/v3/info", "--as", "bot"]
    assert search_command(query="ada", profile=profile) == [
        *head, "contact", "+search-user", "--as", "user", "--query", "ada", "--page-size", "10"]
    assert search_command(open_ids=[OPEN_ID, OTHER], profile=profile)[-2:] == ["--user-ids", f"{OPEN_ID},{OTHER}"]


@pytest.mark.parametrize("bad", [{}, {"chat_id": "oc_1", "user_id": OPEN_ID, "text": "x"},
                                 {"chat_id": "oc_1"}, {"chat_id": "oc_1", "text": "x", "markdown": "x"}])
def test_a_send_names_one_target_and_one_body(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        send_command(**bad)


@pytest.mark.parametrize("raw", ["work; rm -rf /", "-x", " " * 3 + "a b"])
def test_a_profile_is_a_name(raw: str) -> None:
    with pytest.raises(ValueError):
        lark_cli.profile_name(raw)
    assert lark_cli.profile_name("  cli_a945b6dd.prod@x ") == "cli_a945b6dd.prod@x"


def test_replies_go_as_markdown_to_their_chat_and_messages_as_text_to_a_user(tmp_path: Path,
                                                                              lark_cli_stand_in: Path) -> None:
    adapter = _adapter(tmp_path, profile="other")
    adapter._correlation_chat["c1"] = "oc_dm"
    reply = MessageEnvelope.create(source=channel_address("lark"), target=channel_address("lark"),
                                   message_type=MessageType.AGENT_REPLY, payload={"text": "**2** tasks run"},
                                   correlation_id="c1")
    assert asyncio.run(adapter.deliver(reply)).success
    assert asyncio.run(adapter.notify(OPEN_ID, "告警")).success
    assert _calls(lark_cli_stand_in) == [
        ["--profile", "other", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_dm", "--markdown",
         "**2** tasks run"],
        ["--profile", "other", "im", "+messages-send", "--as", "bot", "--user-id", OPEN_ID, "--text", "告警"],
    ]


def test_a_failed_send_says_why(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    adapter = _adapter(tmp_path)
    adapter._correlation_chat["c1"] = "oc_gone"
    reply = MessageEnvelope.create(source=channel_address("lark"), target=channel_address("lark"),
                                   message_type=MessageType.AGENT_REPLY, payload={"text": "x"}, correlation_id="c1")
    receipt = asyncio.run(adapter.deliver(reply))
    assert (receipt.success, receipt.error) == (False, "lark-cli exited 2: Bot/User can NOT be out of the chat")
    notified = asyncio.run(adapter.notify("ou_gone", "x"))
    assert (notified.success, notified.error) == (False, "lark-cli exited 2: Bot/User can NOT be out of the chat")


def test_direct_messages_and_group_mentions_of_the_bot_reach_the_manager(tmp_path: Path,
                                                                         lark_cli_stand_in: Path) -> None:
    rejected: list[tuple[str, str | None]] = []

    async def on_rejected(sender_id: str, chat_id: str | None) -> None:
        rejected.append((sender_id, chat_id))

    async def main() -> Any:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = _adapter(tmp_path, on_rejected=on_rejected)
        adapter._bus = bus  # what start() sets
        adapter._running = True
        await asyncio.wait_for(adapter._run_event_consumer(), timeout=10)  # the stand-in prints its events, exits
        queue = bus.get_queue("manager")
        messages = [queue.get_nowait() for _ in range(queue.qsize())]
        await asyncio.wait_for(asyncio.gather(*adapter._reactions._pending), timeout=10)
        await bus.shutdown()
        return messages, dict(adapter._correlation_chat), dict(adapter._group_message)

    messages, conversations, replies_to = asyncio.run(main())
    direct, group = messages
    assert direct.payload["text"] == "status please"
    assert direct.payload["channel_context"] == {"chat_id": "oc_dm", "chat_type": "p2p", "sender_id": OPEN_ID,
                                                 "message_id": "om_9"}
    # The @-mention is taken out; the Manager is told who wrote, by the name the contact search finds.
    assert group.payload["text"] == "what runs now?"
    assert group.payload["channel_context"] == {"chat_id": "oc_group", "chat_type": "group", "sender_id": OPEN_ID,
                                                "message_id": "om_11", "sender_name": "Ada Lovelace"}
    assert conversations == {direct.correlation_id: "oc_dm", group.correlation_id: "oc_group"}
    assert replies_to == {group.correlation_id: "om_11"}
    # The unlisted sender's @-mention: who and in which chat, never what; the other group messages: nothing.
    assert rejected == [(OTHER, "oc_group")]
    calls = _calls(lark_cli_stand_in)
    assert calls[0] == consume_command()[1:]
    assert sorted(calls[1:]) == sorted([reaction_command("om_9")[1:], reaction_command("om_11")[1:],
                                        ["contact", "+search-user", "--as", "user", "--user-ids", OPEN_ID]])


def _group_envelope(kind: MessageType, payload: dict[str, Any]) -> MessageEnvelope:
    return MessageEnvelope.create(source=channel_address("lark"), target=channel_address("lark"), message_type=kind,
                                  payload=payload, correlation_id="c1")


def test_in_a_group_the_status_and_the_answer_reply_to_the_message_and_progress_goes_to_the_chat(
    tmp_path: Path, lark_cli_stand_in: Path,
) -> None:
    async def main() -> list[bool]:
        adapter = _adapter(tmp_path)
        adapter._correlation_chat["c1"] = "oc_group"
        adapter._group_message["c1"] = "om_11"
        phase = {"text": "thinking", "phase": True, "activity": "thinking"}
        return [(await adapter.deliver(_group_envelope(kind, payload))).success for kind, payload in (
            (MessageType.STATUS_UPDATE, phase),
            (MessageType.AGENT_REPLY, {"text": "任务已创建：**calc**"}),
            (MessageType.STATUS_UPDATE, {"text": "进度更新: 0/1 完成", "task_id": "t1", "status": "executing"}),
        )]

    assert asyncio.run(main()) == [True, True, True]
    calls = _calls(lark_cli_stand_in)
    status, answer, closing, progress = calls
    assert status[:-1] == reply_command("om_11", text="")[1:-1] and status[-1].startswith("⏳ 处理中")
    assert answer == reply_command("om_11", markdown="任务已创建：**calc**")[1:]
    # The status message the reply sent is edited in place.
    assert closing[:6] == ["im", "+messages-edit", "--as", "bot", "--message-id", "om_r0001"]
    assert progress == send_command(chat_id="oc_group", markdown="进度更新: 0/1 完成")[1:]


def test_a_consumer_that_stops_is_started_again_after_a_pause(tmp_path: Path, lark_cli_stand_in: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    sleep = asyncio.sleep

    async def main() -> None:
        adapter = _adapter(tmp_path)
        adapter._bus = MessageBus(runtime_root=tmp_path)
        adapter._running = True

        async def pause(seconds: float) -> None:
            pauses.append(seconds)
            if len(pauses) == 2:
                adapter._running = False
            await sleep(0)

        monkeypatch.setattr(asyncio, "sleep", pause)
        await asyncio.wait_for(adapter._event_collector_loop(), timeout=10)
        await adapter.stop()  # with the reactions to the messages it got still running
        await adapter._bus.shutdown()

    asyncio.run(main())
    consumers = [c for c in _calls(lark_cli_stand_in) if c[:2] == ["event", "consume"]]
    assert pauses == [10, 10] and len(consumers) == 2  # not a tight loop of lark-cli processes


# --- The bot, and why the channel would not start ---


@pytest.mark.parametrize(("config", "problem", "bot"), [
    ({"profile": ""}, None, ("HelperBot", "cli_main", "cli_main")),
    ({"profile": "other"}, None, ("OtherBot", "cli_other", "other")),
    ({"profile": "out"}, "lark-cli cannot act as the bot: lark-cli is not signed in for this profile", None),
    ({"profile": "gone"}, 'lark-cli cannot act as the bot: lark-cli exited 3: profile "gone" not found', None),
    ({"profile": "other", "allowlist_app_id": "cli_main"},
     "the sender allowlist was made for app cli_main; profile other is app cli_other; open_ids differ per app",
     ("OtherBot", "cli_other", "other")),
])
def test_the_start_check(lark_cli_stand_in: Path, config: dict[str, str], problem: str | None,
                         bot: tuple[str, str, str] | None) -> None:
    found_problem, identity = asyncio.run(lark_cli.check(config, fresh=True))
    assert (found_problem or "").startswith(problem or "") and (found_problem is None) == (problem is None)
    assert (None if identity is None else (identity.bot_name, identity.app_id, identity.profile)) == bot


def test_the_identity_is_kept_briefly(lark_cli_stand_in: Path) -> None:
    asyncio.run(lark_cli.identity())
    asyncio.run(lark_cli.identity())
    assert len(_calls(lark_cli_stand_in)) == 2  # whoami and the bot's info, once
    asyncio.run(lark_cli.identity(fresh=True))
    assert len(_calls(lark_cli_stand_in)) == 4


# --- People ---


def test_search_and_names(lark_cli_stand_in: Path) -> None:
    found = asyncio.run(lark_cli.search_people("ada"))
    assert found == [{"open_id": OPEN_ID, "name": "Ada Lovelace", "email": "ada@example.test", "department": "R&D"}]
    assert asyncio.run(lark_cli.names([OPEN_ID, OTHER])) == {OPEN_ID: "Ada Lovelace", OTHER: "Alan Turing"}
    calls = len(_calls(lark_cli_stand_in))
    assert asyncio.run(lark_cli.names([OPEN_ID])) == {OPEN_ID: "Ada Lovelace"}  # remembered
    assert len(_calls(lark_cli_stand_in)) == calls


def test_one_unknown_id_does_not_hide_the_other_names(lark_cli_stand_in: Path) -> None:
    # lark-cli fails the whole batch for one id it cannot resolve; each is then looked up alone.
    assert asyncio.run(lark_cli.names([OPEN_ID, "ou_unknown0001"])) == {OPEN_ID: "Ada Lovelace"}


def test_people_search_refuses_an_empty_or_long_query() -> None:
    for query in ("  ", "x" * 51):
        with pytest.raises(ValueError):
            asyncio.run(lark_settings.people(query))


# --- The allowlist's app ---


@pytest.mark.parametrize(("before", "after", "bound"), [
    # A new allowlist is bound to the saved profile's app.
    ({}, {"allowed_open_ids": [OPEN_ID]}, "cli_main"),
    ({}, {"profile": "other", "allowed_open_ids": [OPEN_ID]}, "cli_other"),
    # An unchanged allowlist keeps its app, also under a profile of another app (which then does not start).
    ({"allowed_open_ids": [OPEN_ID], "allowlist_app_id": "cli_main"},
     {"profile": "other", "allowed_open_ids": [OPEN_ID], "allowlist_app_id": "cli_forged"}, "cli_main"),
    # A changed one takes the profile's app; an empty one has none; lark-cli unable to tell leaves it unknown.
    ({"allowed_open_ids": [OPEN_ID], "allowlist_app_id": "cli_main"},
     {"profile": "other", "allowed_open_ids": [OTHER]}, "cli_other"),
    ({"allowed_open_ids": [OPEN_ID], "allowlist_app_id": "cli_main"}, {"allowed_open_ids": []}, None),
    ({}, {"profile": "out", "allowed_open_ids": [OPEN_ID]}, None),
])
def test_the_allowlist_is_bound_to_an_app(lark_cli_stand_in: Path, before: dict[str, Any], after: dict[str, Any],
                                          bound: str | None) -> None:
    stored = asyncio.run(lark_settings.bound_config(before, {**after, "app_id_env": "LARK_APP_ID"}))
    assert stored.get("allowlist_app_id") == bound
    assert "app_id_env" not in stored and "profile" in stored


def test_without_lark_cli_the_check_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    lark_cli.forget()
    problem, identity = asyncio.run(lark_cli.check({}, fresh=True))
    assert identity is None and problem.startswith("lark-cli cannot act as the bot: lark-cli could not start")
    with pytest.raises(LarkCliError):
        asyncio.run(lark_cli.search_people("ada"))
    lark_cli.forget()


def test_a_cancelled_call_leaves_no_lark_cli_behind(tmp_path: Path) -> None:
    from fake_claude import fake_claude

    pid_file = tmp_path / "pid"
    slow = tmp_path / "lark-cli"
    fake_claude(slow, f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")

    async def main() -> None:
        call = asyncio.ensure_future(lark_cli.run([str(slow)]))
        while not pid_file.exists() or not pid_file.read_text():
            await asyncio.sleep(0.02)
        call.cancel()  # what the channel's stop() does to a reaction call still running
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(asyncio.wait_for(main(), timeout=20))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
