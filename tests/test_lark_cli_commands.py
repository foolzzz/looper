"""The Lark (lark-cli) channel's lark-cli calls, as lark-cli 1.0.97 takes them: a stand-in ``lark-cli`` on PATH
answers from a small world of profiles, users and events (and anything else as lark-cli does, with a JSON error on
stderr and a non-zero exit), so nothing reaches a real chat, user or account."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID, OTHER
from looper.agents.bus import MessageBus
from looper.agents.channels import lark_cli, lark_settings
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_cli import (
    LarkCliError, bot_info_command, consume_command, search_command, send_command, whoami_command,
)
from looper.agents.message import MessageEnvelope, MessageType, channel_address


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _adapter(tmp_path: Path, profile: str = "") -> LarkChannelAdapter:
    return LarkChannelAdapter(profile=profile, runtime_root=tmp_path,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))


@pytest.mark.parametrize("profile", ["", "work"])
def test_every_command_carries_the_profile_when_there_is_one(profile: str) -> None:
    head = ["lark-cli", *(["--profile", profile] if profile else [])]
    assert send_command(chat_id="oc_1", markdown="**hi**", profile=profile) == [
        *head, "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--markdown", "**hi**"]
    assert send_command(user_id=OPEN_ID, text="hi", profile=profile)[len(head) + 4:] == [
        "--user-id", OPEN_ID, "--text", "hi"]
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


def test_direct_messages_reach_the_manager_and_group_ones_do_not(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    async def main() -> Any:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = _adapter(tmp_path)
        adapter._bus = bus  # what start() sets
        adapter._running = True
        await asyncio.wait_for(adapter._run_event_consumer(), timeout=10)  # the stand-in prints two events, exits
        queue = bus.get_queue("manager")
        messages = [queue.get_nowait() for _ in range(queue.qsize())]
        await bus.shutdown()
        return messages, dict(adapter._correlation_chat)

    messages, conversations = asyncio.run(main())
    (message,) = messages
    assert message.payload["text"] == "status please"
    assert message.payload["channel_context"] == {"chat_id": "oc_dm", "sender_id": OPEN_ID, "message_id": "om_9"}
    assert conversations == {message.correlation_id: "oc_dm"}
    assert _calls(lark_cli_stand_in) == [consume_command()[1:]]


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
        await adapter._bus.shutdown()

    asyncio.run(main())
    assert pauses == [10, 10] and len(_calls(lark_cli_stand_in)) == 2  # not a tight loop of lark-cli processes


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
