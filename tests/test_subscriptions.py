"""Subscriptions' pure parts: the fields of a request, the text the processor reads, what is made of its reply and what
the operator is shown. The runs on PostgreSQL are in ``test_subscriptions_db.py``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from looper.agents import cron, subscriptions

UTC = timezone.utc
CREATE = {"source_id": "s", "chat_kinds": ["group", "topic"], "sender_kinds": ["user", "app"], "max_age_minutes": 180,
          "max_events": 500, "text_max_chars": 200}


def test_a_new_subscription_names_what_it_reads():
    assert subscriptions.parse(CREATE, update=False)["max_events"] == 500
    for missing in CREATE:
        with pytest.raises(ValueError, match=missing):
            subscriptions.parse({k: v for k, v in CREATE.items() if k != missing}, update=False)


@pytest.mark.parametrize("field, value", [
    ("chat_kinds", []), ("chat_kinds", ["channel"]), ("sender_kinds", ["bot"]), ("max_age_minutes", 4),
    ("max_age_minutes", 10081), ("max_events", 0), ("max_events", 2001), ("text_max_chars", 19),
    ("text_max_chars", True), ("quiet_word", "two words"), ("quiet_word", ""), ("exclude_mentions_me", "yes"),
    ("output_kind", "memory"), ("include_chats", "oc_x"), ("output_header", "x" * 201),
    ("source_id", " "), ("cursor_seq", 5), ("target_confirmed_at", "2026-01-01"), ("unknown", 1)])
def test_an_invalid_field_is_refused_and_named(field, value):
    with pytest.raises(ValueError, match=field):
        subscriptions.parse({**CREATE, field: value}, update=False)


def test_an_update_may_name_any_part_but_not_nothing():
    assert subscriptions.parse({"quiet_word": "NONE"}, update=True) == {"quiet_word": "NONE"}
    with pytest.raises(ValueError, match="at least one"):
        subscriptions.parse({}, update=True)
    with pytest.raises(ValueError, match="cursor and confirmation"):
        subscriptions.parse({"cursor_seq": 1}, update=True)


# --- lines_v1: the line format of lark-chat's hourly digest, kept exactly (a contract, with synthetic rows) ---

ROW = {"sent_at": datetime(2026, 11, 19, 16, 5, 3, tzinfo=UTC), "chat_id": "oc_1", "chat_name": "Ops room",
       "sender_id": "ou_abcdefghijklmnop", "sender_name": "Alice", "text": "hello"}


def test_a_line_is_time_chat_sender_and_text_as_the_collector_writes_it():
    assert subscriptions.render_lines_v1([ROW], text_max_chars=200, zone_name="America/Los_Angeles") == \
        "[2026/11/19 08:05:03]《Ops room》 Alice: hello"
    # The month and the day are not padded, the clock is 24-hour.
    early = {**ROW, "sent_at": datetime(2026, 1, 5, 8, 0, 0, tzinfo=UTC)}
    assert subscriptions.render_lines_v1([early], text_max_chars=200, zone_name="America/Los_Angeles") == \
        "[2026/1/5 00:00:00]《Ops room》 Alice: hello"


def test_a_line_falls_back_to_the_chat_id_and_ten_characters_of_the_sender_id():
    row = {**ROW, "chat_name": "", "sender_name": None, "sender_id": "ou_abcdefghijklmnop"}
    assert subscriptions.render_lines_v1([row], text_max_chars=200, zone_name="UTC") == \
        "[2026/11/19 16:05:03]《oc_1》 ou_abcdefg: hello"
    unknown = {**ROW, "sender_name": "", "sender_id": ""}
    assert "》 ?: " in subscriptions.render_lines_v1([unknown], text_max_chars=200, zone_name="UTC")


def test_the_text_is_one_line_cut_by_utf16_units():
    row = {**ROW, "text": "a\n\n  b\t c﻿d" + "x" * 300}
    line = subscriptions.render_lines_v1([row], text_max_chars=10, zone_name="UTC")
    assert line.endswith("Alice: a b c dxxx")  # ten units, as JavaScript's slice(0, 10)
    emoji = {**ROW, "text": "ab😀cd"}  # the pair is two units: it does not fit in three
    assert subscriptions.render_lines_v1([emoji], text_max_chars=3, zone_name="UTC").endswith("Alice: ab")
    assert subscriptions.render_lines_v1([emoji], text_max_chars=4, zone_name="UTC").endswith("Alice: ab😀")


def test_the_processor_reads_the_count_then_the_lines_newest_first():
    older = {**ROW, "sent_at": datetime(2026, 11, 19, 15, 0, 0, tzinfo=UTC), "text": "older"}
    text = subscriptions.processor_input([ROW, older], text_max_chars=200, zone_name="UTC")
    assert text == ("消息(共 2 条):\n[2026/11/19 16:05:03]《Ops room》 Alice: hello\n"
                    "[2026/11/19 15:00:00]《Ops room》 Alice: older")


# --- the reply ---

@pytest.mark.parametrize("reply", ["SKIP", " skip\n", "**SKIP**", "`Skip`.", "# S_K-I.P", "", "  \n "])
def test_a_reply_that_is_the_quiet_word_or_nothing_is_quiet(reply):
    assert subscriptions.is_quiet(reply, "SKIP")


@pytest.mark.parametrize("reply", ["SKIP the boring ones", "nothing SKIP", "瓜", "SKIPS"])
def test_a_reply_that_says_more_is_not_quiet(reply):
    assert not subscriptions.is_quiet(reply, "SKIP")


@pytest.mark.parametrize("text, expected", [
    ('<at user_id="ou_x">Alice</at> look', "Alice look"),
    ('<at id="all"></at> everyone', " everyone"), ("<at user_id=\"all\">所有人</at>!", "所有人!"),
    ("hi @_user_1 and @_user_22", "hi  and "), ("@所有人 注意", "所有人 注意"), ("＠所有人", "所有人"),
    ("ping @all now, @everyone, @here", "ping all now, everyone, here"), ("@ou_abcdef123 hi", "ou_abcdef123 hi"),
    ("mail a@b.com and @alice stay", "mail a@b.com and @alice stay"), ("<AT ID=ALL></AT>x", "x"),
    ("<at user_id=\"1\">Bob", "Bob")])
def test_nothing_in_a_push_mentions_anybody(text, expected):
    assert subscriptions.strip_mentions(text) == expected


def test_a_push_is_its_header_and_the_reply():
    assert subscriptions.push_text("  - one\n- two ", "🍉 吃瓜速报 · 过去1小时") == "🍉 吃瓜速报 · 过去1小时\n\n- one\n- two"
    assert subscriptions.push_text("- one", "") == "- one"
    assert subscriptions.push_text('<at id="all"></at>', "header") == ""  # nothing left but a mention


def test_a_long_push_is_split_and_cut_at_three_messages():
    line = "x" * 99 + "\n"
    assert not subscriptions.push_parts(line * 100)[-1].endswith(subscriptions.CUT_NOTE)  # 10,000: three messages
    parts = subscriptions.push_parts(line * 150)  # 15,000 characters: four messages, the fourth dropped
    assert len(parts) == 3 and all(len(part) <= 4000 for part in parts)
    assert parts[-1].endswith(subscriptions.CUT_NOTE)
    assert subscriptions.push_parts("short") == ["short"]
    assert len(subscriptions.push_parts(line * 60)) == 2  # 6,000 characters: two messages, nothing cut
    assert not subscriptions.push_parts(line * 60)[-1].endswith(subscriptions.CUT_NOTE)


def test_each_message_of_a_push_has_a_key_that_fits_the_channels_limit():
    keys = subscriptions.idempotency_keys("srun-0123456789ab", 3)
    assert keys == ["srun-0123456789ab", "srun-0123456789ab-2", "srun-0123456789ab-3"]
    assert all(len(key) <= 50 for key in keys)


# --- what changes the confirmation ---

ROW_SUB = {"kind": "subscription", "enabled": False, "notify_channel": "lark", "notify_chat": "ou_a",
           "cron": "5 * * * *", "timezone": "UTC"}


@pytest.mark.parametrize("row, changes, needed", [
    (ROW_SUB, {"enabled": True}, True),
    ({**ROW_SUB, "enabled": True}, {"enabled": True}, False),
    ({**ROW_SUB, "enabled": True}, {"notify_chat": "ou_b"}, True),
    ({**ROW_SUB, "enabled": True}, {"notify_channel": "telegram", "notify_chat": "1"}, True),
    ({**ROW_SUB, "enabled": True}, {"cron": "6 * * * *"}, True),
    ({**ROW_SUB, "enabled": True}, {"cron": "5 * * * *"}, False),
    ({**ROW_SUB, "enabled": True}, {"timezone": "Asia/Shanghai"}, True),
    ({**ROW_SUB, "enabled": True}, {"name": "renamed", "requirement": "other words"}, False),
    ({**ROW_SUB, "enabled": True}, {"enabled": False}, False),
    ({**ROW_SUB, "notify_channel": None, "notify_chat": None}, {"enabled": True}, False),  # nothing to push to
    ({**ROW_SUB, "kind": "agent"}, {"enabled": True}, False)])
def test_a_push_target_needs_the_operators_word_when_it_or_when_it_runs_changes(row, changes, needed):
    assert subscriptions.needs_confirmation(row, changes) is needed


@pytest.mark.parametrize("rule, words", [
    ("5 * * * *", "每小时第 5 分钟（America/Los_Angeles）"), ("*/10 * * * *", "每 10 分钟（America/Los_Angeles）"),
    ("5,35 * * * *", "每小时第 5、35 分钟（America/Los_Angeles）"), ("0,30 * * * *", "每 30 分钟（America/Los_Angeles）"),
    ("0 9 * * 1-5", "每个工作日 09:00（America/Los_Angeles）"),
    ("30 8,18 * * *", "每天 08:30、18:30（America/Los_Angeles）"), ("0 10 * * 1,3", "每周一、三 10:00（America/Los_Angeles）"),
    ("0 9 1 * *", "按 cron 规则 0 9 1 * *（America/Los_Angeles）")])
def test_the_rule_in_words(rule, words):
    assert subscriptions.describe_zh(cron.parse(rule), "America/Los_Angeles") == words


def test_how_often_a_rule_pushes_at_most_in_a_day():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    assert subscriptions.fires_per_day(cron.parse("5 * * * *"), "UTC", now) == 24
    assert subscriptions.fires_per_day(cron.parse("0 9 * * 1-5"), "UTC", now) == 1
    assert subscriptions.fires_per_day(cron.parse("0 9,18 * * *"), "UTC", now) == 2
