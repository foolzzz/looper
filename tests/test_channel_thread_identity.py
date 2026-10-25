"""Channel conversation ids preserve the bot and destination without changing its raw chat id."""

from dataclasses import FrozenInstanceError

import pytest

from looper.agents.message import channel_thread_id, parse_channel_thread_id


@pytest.mark.parametrize("channel_type,chat_id", [
    ("telegram", "12345"), ("telegram", "-10012345"),
    ("lark", "oc_dm"), ("lark", "ou_me"),
    ("lark_bot", "oc_group_1"), ("lark_bot", "ou_me"),
])
@pytest.mark.parametrize("number", [None, 2])
@pytest.mark.parametrize("destination,owner", [
    ("manager", "manager"), ("manager-review", "manager-review"),
    ("manager-review", "manager"), ("manager", "manager-review"),
])
def test_channel_thread_round_trips_the_bot_chat_and_cross_owner_destination(
        channel_type: str, chat_id: str, number: int | None, destination: str, owner: str) -> None:
    channel_id = f"channel-{channel_type}" + (f"-{number}" if number else "")
    prefix = channel_id if number else channel_type
    suffix = f"#{destination}" if destination != owner else ""
    session_id = channel_thread_id(channel_id, chat_id, destination, owner)
    assert session_id == f"{prefix}:{chat_id}{suffix}"
    parsed = parse_channel_thread_id(session_id)
    assert parsed is not None
    assert (parsed.channel_type, parsed.channel_id, parsed.chat_id, parsed.manager_id) == (
        channel_type, channel_id, chat_id, destination if suffix else None)


def test_rebinding_to_the_original_owner_reuses_the_original_id() -> None:
    assert [channel_thread_id("channel-lark-2", "oc_dm", manager, "manager-review")
            for manager in ("manager-review", "manager", "manager-review")] == [
        "channel-lark-2:oc_dm", "channel-lark-2:oc_dm#manager", "channel-lark-2:oc_dm"]


def test_parsed_channel_thread_is_immutable() -> None:
    parsed = parse_channel_thread_id("telegram:42#manager-review")
    assert parsed is not None
    with pytest.raises(FrozenInstanceError):
        parsed.chat_id = "43"  # type: ignore[misc]


@pytest.mark.parametrize("session_id", [
    None, "", "session-123", "web:123", "client:123", "planner-task:123",
    "channel-web:123", "channel-telegram:42", "channel-lark:oc_dm", "unknown:42",
    "channel-telegram-0:42", "channel-lark-01:oc_dm", "channel-lark-1000000000:oc_dm",
    "channel-telegram-2\n:42", "telegram:", "telegram:oc_dm", "telegram:ou_me", "telegram:+42", "telegram:1:2",
    "lark:oc_dm value",
    "telegram:42#", "telegram:42#other", "telegram:42#manager-", "telegram:42#manager-a",
    "telegram:42#manager-Aa", "telegram:42#manager-aa_", "telegram:42#manager-aa\n",
    "telegram:42#manager-" + "a" * 32, "telegram:42#manager-review#manager",
    "lark:oc_dm#arbitrary#manager-review", "lark:oc_dm#arbitrary",
])
def test_malformed_or_non_channel_thread_is_rejected(session_id: str | None) -> None:
    assert parse_channel_thread_id(session_id) is None


@pytest.mark.parametrize("channel_id,chat_id,destination,owner", [
    ("telegram", "42", "manager", "manager"),
    ("channel-web", "42", "manager", "manager"),
    ("channel-telegram-0", "42", "manager", "manager"),
    ("channel-telegram", "42#manager-review", "manager", "manager"),
    ("channel-lark", "oc_dm:other", "manager", "manager"),
    ("channel-telegram", "42", "manager-a", "manager"),
    ("channel-telegram", "42", "manager", "planner-task"),
])
def test_formatter_rejects_invalid_identity_inputs(
        channel_id: str, chat_id: str, destination: str, owner: str) -> None:
    with pytest.raises(ValueError):
        channel_thread_id(channel_id, chat_id, destination, owner)


@pytest.mark.parametrize("channel_type", ["lark", "lark_bot"])
@pytest.mark.parametrize("chat_id", ["42", "oc_", "oc_dm/other", "not-a-chat"])
def test_lark_chat_ids_need_no_platform_prefix(channel_type: str, chat_id: str) -> None:
    session_id = channel_thread_id(f"channel-{channel_type}", chat_id, "manager", "manager")
    parsed = parse_channel_thread_id(session_id)
    assert parsed is not None
    assert parsed.chat_id == chat_id


@pytest.mark.parametrize("channel_type", ["lark", "lark_bot"])
@pytest.mark.parametrize("chat_id", [
    "", "oc_dm#manager-review", "oc_dm:other", "oc_dm value", "\toc_dm", "oc_dm\n", "oc_dm\r", "oc_dm\u00a0value",
])
def test_lark_chat_ids_reject_empty_ids_separators_and_whitespace(channel_type: str, chat_id: str) -> None:
    with pytest.raises(ValueError, match=f"not a {channel_type} chat id"):
        channel_thread_id(f"channel-{channel_type}", chat_id, "manager-review", "manager")
    assert parse_channel_thread_id(f"{channel_type}:{chat_id}#manager-review") is None


@pytest.mark.parametrize("raw", ["0", "00123", "-00123"])
def test_numeric_chat_ids_keep_their_historical_spelling(raw: str) -> None:
    session_id = channel_thread_id("channel-telegram", raw, "manager", "manager")
    assert session_id == f"telegram:{raw}"
    assert parse_channel_thread_id(session_id).chat_id == raw  # type: ignore[union-attr]


def _message(*, pinned=None, channel="telegram", channel_id="channel-telegram-2", chat_id="42",
             target="manager-review", session_id=None, owner=None):
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address

    metadata = {"channel_thread_id": pinned} if pinned is not None else {}
    if owner is not None:
        metadata["channel_manager_id"] = owner
    return MessageEnvelope.create(
        source=channel_address(channel, channel_id), target=manager_address(target),
        message_type=MessageType.USER_MESSAGE,
        payload={"text": "hello", "channel_context": {"chat_id": chat_id, "session_id": session_id}},
        metadata=metadata)


@pytest.mark.parametrize("destination", ["manager", "manager-review"])
def test_formatted_pin_accepts_the_bot_owner_and_a_bound_manager(destination):
    from looper.agents.tiers.manager import _session_key

    pinned = channel_thread_id("channel-telegram-2", "42", destination, "manager")
    assert _session_key(_message(pinned=pinned, target=destination, owner="manager")) == pinned


@pytest.mark.parametrize("destination,target", [
    ("manager", "manager-review"), ("manager-review", "manager"),
])
def test_formatted_pin_rejects_an_owner_or_bound_manager_destination_mismatch(destination, target):
    from looper.agents.tiers.manager import _session_key

    pinned = channel_thread_id("channel-telegram-2", "42", destination, "manager")
    with pytest.raises(ValueError, match="does not match"):
        _session_key(_message(pinned=pinned, target=target, owner="manager"))


def test_pinned_identity_survives_envelope_round_trip_without_changing_channel_context():
    from looper.agents.message import MessageEnvelope
    from looper.agents.tiers.manager import _session_key, channel_line

    original = _message()
    pinned = _message(pinned="channel-telegram-2:42#manager-review")
    restored = MessageEnvelope.from_dict(pinned.to_dict())
    assert _session_key(restored) == "channel-telegram-2:42#manager-review"
    assert channel_line(restored) == channel_line(original)
    assert restored.payload == original.payload


@pytest.mark.parametrize("pinned", [
    "channel-telegram-2:43#manager-review", "channel-telegram-3:42#manager-review",
    "channel-lark-2:oc_dm#manager-review", "channel-telegram-2:42#manager",
    "channel-telegram-2:42#", "channel-telegram-2:42", 42,
])
def test_a_pin_must_match_the_source_chat_and_destination(pinned):
    from looper.agents.tiers.manager import _session_key

    with pytest.raises(ValueError, match="does not match"):
        _session_key(_message(pinned=pinned))


def test_channel_metadata_cannot_override_a_web_session():
    from looper.agents.tiers.manager import _session_key

    assert _session_key(_message(channel="web", channel_id="channel-web", session_id="client-session",
                                 pinned="telegram:42#manager-review")) == "client-session"
