"""What an extraction run gives the processor and what it reads back (``looper.agents.extraction``): the rendered
input against a frozen synthetic sample, and every rule of parsing a reply. Pure: no database, no model."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from looper.agents import extraction
from looper.agents.extraction import Participants, parse, render

UTC = timezone.utc
ZONE = "Asia/Shanghai"
T = datetime(2026, 11, 20, 2, 1, 2, tzinfo=UTC)  # 10:01:02 in ZONE


def row(seq: int, chat: str, text: str, *, minutes: float, sender: str = "ou_zhang", kind: str = "group",
        name: str = "发布群", sender_kind: str = "user", parent: str | None = None) -> dict:
    return {"seq": seq, "external_id": f"om_{seq}", "chat_id": chat, "chat_kind": kind, "chat_name": name,
            "parent_id": parent, "sender_id": sender, "sender_kind": sender_kind, "sender_name": None,
            "sent_at": T + timedelta(minutes=minutes), "mentions_me": False, "mention_ids": [], "text": text}


PEOPLE = Participants({"ou_zhang": "张三", "ou_li": "李四", "ou_wang": "王五"}, "ou_li")


def test_a_leads_input_lists_participants_known_leads_and_each_chats_context_then_window():
    context = [row(10, "oc_release", "登录页的报错谁能看一下？", minutes=0)]
    rows = [row(12, "oc_release", "我来改，今天下班前", minutes=4, sender="ou_li", parent="om_10"),
            row(13, "ou_wang", "周五的评审  别忘了\n带上数据", minutes=2, sender="ou_wang", kind="p2p", name="王五")]
    known = [{"id": 7, "kind": "decision", "summary": "发布定在周三", "chat_name": "发布群",
              "last_sent_at": T - timedelta(days=2)}]
    text, refs = render("leads", rows, context=context, participants=PEOPLE, known=known, zone_name=ZONE,
                        text_max_chars=500)
    # Chats in the order of their first window message; the private chat's message came first.
    assert text == """\
参与者:
P1 王五
P2 张三
P3 李四（用户本人）
已有线索（新消息可能推翻或重复它们）:
L1 [decision] 发布定在周三 —《发布群》2026-11-18
消息（m 开头的要整理；c 开头的是前文，只作上下文）:
《王五》（私聊）
[m1] [2026/11/20 10:03:02] P1: 周五的评审 别忘了 带上数据
《发布群》（群）
[c1] [2026/11/20 10:01:02] P2: 登录页的报错谁能看一下？
[m2] [2026/11/20 10:05:02] P3 ↩c1: 我来改，今天下班前"""
    assert refs == {"m1": 13, "c1": 10, "m2": 12, "P1": "ou_wang", "P2": "ou_zhang", "P3": "ou_li", "L1": 7}


def test_an_intents_input_marks_the_alert_bot_and_lists_recent_intents_with_their_state():
    rows = [row(20, "oc_ops", "支付回调 500 告警触发", minutes=0, sender="cli_alertbot0001", sender_kind="app",
                kind="topic", name="告警群"),
            row(21, "oc_ops", "这个我看下", minutes=1, sender="ou_li", kind="topic", name="告警群")]
    known = [{"id": 42, "type_key": "bug_triage", "state": "pending", "title": "支付回调偶发 500",
              "chat_name": "王五", "created_at": T - timedelta(hours=1)},
             {"id": 40, "type_key": "dev_task", "state": "rejected", "title": "重做首页", "chat_name": "发布群",
              "created_at": T - timedelta(days=3)}]
    text, refs = render("intents", rows, context=[], participants=PEOPLE, known=known, zone_name=ZONE,
                        text_max_chars=500)
    assert text == """\
参与者:
P1 cli_alertb（告警机器人）
P2 李四（用户本人）
近期意图（新消息可能是同一件事）:
I1 [bug_triage · 待处理] 支付回调偶发 500 —《王五》2026-11-20
I2 [dev_task · 已驳回] 重做首页 —《发布群》2026-11-17
消息（m 开头的要整理；c 开头的是前文，只作上下文）:
《告警群》（话题群）
[m1] [2026/11/20 10:01:02] P1: 支付回调 500 告警触发
[m2] [2026/11/20 10:02:02] P2: 这个我看下"""
    assert refs["I1"] == 42 and refs["I2"] == 40 and refs["P1"] == "cli_alertbot0001"


def test_a_kind_shows_only_its_own_known_block_and_empty_blocks_are_left_out():
    rows = [row(1, "oc_a", "hello", minutes=0)]
    lead = [{"id": 1, "kind": "fact", "summary": "x", "chat_name": "a", "last_sent_at": T}]
    text, _ = render("intents", rows, context=[], participants=PEOPLE, known=lead and [], zone_name=ZONE,
                     text_max_chars=500)
    assert "已有线索" not in text and "近期意图" not in text
    text, _ = render("leads", rows, context=[], participants=PEOPLE, known=[], zone_name=ZONE, text_max_chars=500)
    assert "已有线索" not in text


def test_known_leads_stop_at_forty_lines_or_eight_thousand_characters():
    rows = [row(1, "oc_a", "hello", minutes=0)]
    many = [{"id": n, "kind": "fact", "summary": f"fact {n}", "chat_name": "a", "last_sent_at": T} for n in range(60)]
    _, refs = render("leads", rows, context=[], participants=PEOPLE, known=many, zone_name=ZONE, text_max_chars=500)
    assert sum(1 for ref in refs if ref[0] == "L") == 40
    long = [{**lead, "summary": "长" * 190} for lead in many]
    text, refs = render("leads", rows, context=[], participants=PEOPLE, known=long, zone_name=ZONE, text_max_chars=500)
    shown = [line for line in text.splitlines() if line.startswith("L")]
    assert sum(len(line) + 1 for line in shown) <= extraction.KNOWN_LEADS_CHARS < sum(len(line) + 1 for line in shown) + 220


def test_a_message_is_cut_to_text_max_chars_and_a_reply_to_a_message_outside_the_input_has_no_mark():
    rows = [row(1, "oc_a", "x" * 30, minutes=0, parent="om_999")]
    text, _ = render("leads", rows, context=[], participants=PEOPLE, known=[], zone_name=ZONE, text_max_chars=20)
    assert text.endswith("P1: " + "x" * 20) and "↩" not in text


def test_the_context_keeps_the_newest_hundred_and_only_chats_and_parents_still_in_the_window():
    rows = [row(500, "oc_a", "now", minutes=600, parent="om_7"), row(501, "oc_b", "later", minutes=601)]
    before = [row(n, "oc_a", f"before {n}", minutes=n) for n in range(10, 140)] + [row(3, "oc_gone", "x", minutes=1)]
    parents = [row(7, "oc_a", "the parent", minutes=-5), row(8, "oc_a", "not a parent", minutes=-4)]
    chosen = extraction.select_context(rows, before, parents)
    assert len(chosen) == extraction.CONTEXT_MAX
    assert [r["seq"] for r in chosen][:2] == [40, 41]  # the parent (oldest) and the oldest before-rows dropped first
    assert 3 not in {r["seq"] for r in chosen} and 8 not in {r["seq"] for r in chosen}
    chosen = extraction.select_context(rows, before[:3], parents)
    assert [r["seq"] for r in chosen] == [7, 10, 11, 12]


# --- Parsing ---

NOW = datetime(2026, 11, 20, 15, 30, tzinfo=ZoneInfo(ZONE))
REFS = {"m1": 101, "m2": 102, "m3": 103, "c1": 90, "P1": "ou_zhang", "P2": "ou_li", "L1": 7, "L2": 8, "I1": 42}
APPS = {103}
TYPES = {"dev_task", "bug_triage", "alert_triage"}


def lead(**fields) -> dict:
    return {"kind": "decision", "summary": "发布改到周五", "confidence": "high", "sources": ["m1"], **fields}


def leads(*items, **extra) -> str:
    return json.dumps({"leads": list(items), **extra}, ensure_ascii=False)


def parsed_leads(reply: str, **kw):
    return parse("leads", reply, REFS, now=NOW, valid_days=90, enabled_types=TYPES, app_seqs=APPS, **kw)


def intent(**fields) -> dict:
    return {"type": "bug_triage", "title": "看登录页报错", "summary": "张三请用户看登录页报错", "confidence": "high",
            "sources": ["m1"], **fields}


def parsed_intents(*items, **extra):
    reply = json.dumps({"intents": list(items), **extra}, ensure_ascii=False)
    return parse("intents", reply, REFS, now=NOW, valid_days=90, enabled_types=TYPES, app_seqs=APPS)


@pytest.mark.parametrize("reply", ["NONE", "  NONE\n", "```\nNONE\n```", '{"leads": []}', '```json\n{"leads": []}\n```'])
def test_none_or_an_empty_list_is_quiet(reply):
    assert parsed_leads(reply).state == "quiet"


@pytest.mark.parametrize("reply", ["Nothing here.", "none", "[]", '{"items": []}', '{"leads": {}}', '"NONE"',
                                   '{not json} at all', '{"leads": [', "```\nnotes only\n```"])
def test_anything_else_than_none_or_an_object_with_the_list_is_invalid(reply):
    found = parsed_leads(reply)
    assert found.state == "invalid" and found.reason


@pytest.mark.parametrize("wrap", [
    "Here are the leads:\n{}\nThat is all.",                                    # prose around the object
    "{} trailing",                                                                # words after it
    "Notes first.\n```json\n{}\n```\nLater:\n```\nnot json\n```",               # the JSON fence among others
    "```\nmy reasoning, not json\n```\n```json\n{}\n```",                         # a fence before the JSON one
])
def test_the_object_is_found_inside_prose_or_among_several_fences(wrap):
    found = parsed_leads(wrap.format(leads(lead())))
    assert found.state == "items" and len(found.items) == 1 and found.items[0]["sources"] == [101]


def test_a_reply_too_long_to_search_whole_is_unreadable_even_with_one_object_found():
    reply = '{"leads": []} ' + "{} " * 199 + leads(lead())
    found = parsed_leads(reply)
    assert found.state == "invalid" and found.reason


@pytest.mark.parametrize("cost, timeout_s, failure", [(0.6, 60.0, "budget"), (0.01, 0.5, "timeout")])
def test_an_unreadable_reply_with_no_cap_or_time_left_to_ask_again_is_that_failure_not_a_skip(cost, timeout_s,
                                                                                               failure):
    from looper.agents import processor

    asks: list[str] = []

    async def run(reminder: str, budget_usd: float, left_s: float) -> processor.ProcessorRun:
        asks.append(reminder)
        return processor.ProcessorRun("Sure, here you go.", 7, cost)

    asked = asyncio.run(extraction.ask(run, parsed_leads, budget_usd=0.6, timeout_s=timeout_s))
    assert asks == [""] and (asked.skipped, asked.parsed) == (None, None)
    assert asked.result.failure.value == failure and "could not be read" in asked.result.detail
    assert (asked.result.record_id, asked.cost_usd) == (7, cost)


def test_a_fenced_object_is_read_and_unknown_fields_are_ignored_at_every_level():
    reply = "```json\n" + leads(lead(tool="Bash", command="rm -rf /", extra={"x": 1}), tool="run",
                                command="ls") + "\n```"
    found = parsed_leads(reply)
    assert found.state == "items" and not found.dropped
    [item] = found.items
    assert set(item) == {"kind", "summary", "detail", "confidence", "sources", "window_sources", "about",
                         "valid_until", "supersedes", "same_as"}
    assert item["sources"] == [101] and item["window_sources"] == [101] and item["same_as"] is None


@pytest.mark.parametrize("item, reason", [
    (lead(kind="rumor"), "unknown_kind"),
    ("not an object", "unknown_kind"),
    (lead(confidence="certain"), "bad_confidence"),
    (lead(summary="   "), "empty_summary"),
    (lead(summary=None), "empty_summary"),
    (lead(summary="长" * 201), "too_long"),
    (lead(detail="长" * 601), "too_long"),
    (lead(sources=["m9"]), "unknown_ref"),
    (lead(sources=["P1"]), "unknown_ref"),
    (lead(sources="m1"), "unknown_ref"),
    (lead(about=["m1"]), "unknown_ref"),
    (lead(supersedes=["I1"]), "unknown_ref"),
    (lead(same_as="L9"), "unknown_ref"),
    (lead(sources=["c1"]), "no_window_source"),
    (lead(sources=[]), "no_window_source"),
    (lead(sources=None), "no_window_source"),
    (lead(supersedes=["L1"], same_as="L2"), "both_links"),
    (lead(valid_until="next week"), "bad_date"),
    (lead(valid_until="2026-02-30"), "bad_date"),
])
def test_each_bad_lead_is_dropped_and_counted_by_its_reason(item, reason):
    found = parsed_leads(leads(item, lead(summary="ok")))
    assert found.state == "items" and dict(found.dropped) == {reason: 1} and len(found.items) == 1


@pytest.mark.parametrize("item, reason", [
    (intent(type="weekly_notes"), "unknown_type"),
    ("not an object", "unknown_type"),
    (intent(confidence="sure"), "bad_confidence"),
    (intent(title=""), "empty_summary"),
    (intent(title="长" * 61), "too_long"),
    (intent(summary="长" * 401), "too_long"),
    (intent(sources=["L1"]), "unknown_ref"),
    (intent(same_as="L1"), "unknown_ref"),
    (intent(sources=["c1"]), "no_window_source"),
    (intent(sources=["m3"]), "app_only_not_alert"),
])
def test_each_bad_intent_is_dropped_and_counted_by_its_reason(item, reason):
    found = parsed_intents(item, intent(title="ok"))
    assert found.state == "items" and dict(found.dropped) == {reason: 1} and len(found.items) == 1


def test_an_intent_whose_sources_are_all_alert_messages_is_kept_only_as_alert_triage():
    found = parsed_intents(intent(type="alert_triage", sources=["m3"]), intent(sources=["m3", "m1"], same_as="I1"))
    assert not found.dropped and [i["type"] for i in found.items] == ["alert_triage", "bug_triage"]
    assert found.items[1]["same_as"] == 42 and found.items[1]["sources"] == [103, 101]


def test_valid_until_is_the_end_of_the_named_day_clamped_to_tomorrow_and_the_valid_days():
    tomorrow = datetime(2026, 11, 21, tzinfo=ZoneInfo(ZONE))
    found = parsed_leads(leads(lead(valid_until="2026-11-25"), lead(valid_until="2026-11-01"),
                               lead(valid_until="2027-12-31"), lead(), lead(valid_until=None)))
    until = [item["valid_until"] for item in found.items]
    assert until[0] == datetime(2026, 11, 26, tzinfo=ZoneInfo(ZONE))
    assert until[1] == tomorrow  # a day already past still lasts until tomorrow
    assert until[2] == until[3] == until[4] == NOW + timedelta(days=90)


def test_more_than_fifty_items_keep_the_first_fifty():
    found = parsed_leads(leads(*[lead(summary=f"lead {n}") for n in range(53)]))
    assert len(found.items) == 50 and found.dropped == {"over_limit": 3}
    assert found.items[-1]["summary"] == "lead 49"


def test_sources_about_and_supersedes_are_capped_and_deduplicated():
    refs = {**REFS, **{f"m{n}": 200 + n for n in range(4, 20)}, **{f"P{n}": f"ou_{n}" for n in range(3, 10)},
            **{f"L{n}": n for n in range(3, 9)}}
    item = lead(sources=["m1", "m1", *[f"m{n}" for n in range(4, 20)]], about=[f"P{n}" for n in range(1, 10)],
                supersedes=[f"L{n}" for n in range(1, 9)])
    [found] = parse("leads", leads(item), refs, now=NOW, valid_days=90, enabled_types=TYPES, app_seqs=()).items
    assert found["sources"] == [101, *range(204, 213)][:9] and len(found["about"]) == 5 and found["supersedes"] == [7, 8, 3]


def test_an_unknown_ref_past_the_source_cap_is_ignored_with_the_rest():
    refs = {**REFS, **{f"m{n}": 200 + n for n in range(4, 14)}}
    item = lead(sources=[*[f"m{n}" for n in range(4, 14)], "m99"])
    found = parse("leads", leads(item), refs, now=NOW, valid_days=90, enabled_types=TYPES, app_seqs=())
    assert not found.dropped and len(found.items[0]["sources"]) == 10


def test_instructions_seen_maps_refs_to_seqs_and_ignores_unknown_ones():
    found = parsed_leads(leads(lead(), instructions_seen=["m2", "c1", "m2", "m77", "P1", 3]))
    assert found.instructions_seen_seqs == [102, 90]
    found = parsed_leads(leads(instructions_seen=["m2"]))
    assert found.state == "quiet" and found.instructions_seen_seqs == [102]


def test_the_contracts_keep_their_json_shape_and_the_intent_types_are_appended_to_the_instructions():
    assert '"leads": [{"kind": "decision|commitment|request|status|issue|fact"' in extraction.LEADS_CONTRACT
    assert '"intents": [{"type": "<a key from Intent types>"' in extraction.INTENTS_CONTRACT
    assert "Use same_as when a recent intent is the same matter." in extraction.INTENTS_CONTRACT
    for contract in extraction.CONTRACTS.values():
        assert "reply with exactly NONE" in contract and "instructions_seen" in contract
    text = extraction.instructions("intents", extraction.INTENTS_INSTRUCTIONS,
                                   [{"key": "weekly_notes", "name": "周报", "guidance": "要写进周报的\n进展"}])
    assert text.endswith("\n\n## Intent types\n- weekly_notes 周报: 要写进周报的 进展")
    assert extraction.instructions("leads", "x", []) == "x"


def test_an_input_over_sixty_thousand_characters_leaves_its_newest_rows_for_the_next_window():
    rows = [row(n, "oc_a", "字" * 400, minutes=n) for n in range(1, 301)]
    kept, context, text, refs = extraction.fit("leads", rows, before=[], parents=[], participants=PEOPLE, known=[],
                                               zone_name=ZONE, text_max_chars=500)
    assert len(text) <= extraction.INPUT_MAX_CHARS and 100 < len(kept) < 300
    assert [r["seq"] for r in kept] == list(range(1, len(kept) + 1))  # the oldest are kept, in order
    assert max(seq for ref, seq in refs.items() if ref[0] == "m") == len(kept)
    small = rows[:3]
    assert extraction.fit("leads", small, before=[], parents=[], participants=PEOPLE, known=[], zone_name=ZONE,
                          text_max_chars=500)[0] == small


def test_malformed_fields_drop_their_item_only_and_a_far_date_is_clamped():
    found = parsed_leads(leads(lead(kind=[]), lead(confidence={}), lead(kind={"a": 1}), lead(valid_until="9999-12-31"),
                               lead(valid_until="0001-01-01"), lead(summary="kept")))
    assert dict(found.dropped) == {"unknown_kind": 2, "bad_confidence": 1}
    assert [item["summary"] for item in found.items] == ["发布改到周五", "发布改到周五", "kept"]
    assert found.items[0]["valid_until"] == NOW + timedelta(days=90)
    assert found.items[1]["valid_until"] == datetime(2026, 11, 21, tzinfo=ZoneInfo(ZONE))
    found = parsed_intents(intent(type=["dev_task"]), intent(confidence=[]), intent(title="kept"))
    assert dict(found.dropped) == {"unknown_type": 1, "bad_confidence": 1} and len(found.items) == 1


@pytest.mark.parametrize("anchor", ["group", "base", "off"])
def test_context_anchored_on_a_group_row_stays_inside_its_range(anchor):
    first = row(3, "g", "first", minutes=3, parent="om_1")
    group = row(4, "g", "group", minutes=4, parent="om_2")
    if anchor != "off":
        group.update(via_group=True, read_from=T + timedelta(minutes=2))
        first.update(via_group=anchor == "group", read_from=T + timedelta(minutes=2))
    base = row(5, "g", "base", minutes=5, parent="om_0")
    parents = [row(n, "g", "old parent", minutes=-n) for n in range(3)]
    before = [row(6, "g", "before", minutes=1), row(7, "g", "inside range", minutes=2)]
    selected = extraction.select_context([first, group, base], before, parents)
    seqs = {r["seq"] for r in selected}
    if anchor == "group":
        assert seqs == {0, 7}  # original-scope parent remains, old group parent and before do not
    elif anchor == "base":
        assert seqs == {0, 1, 6, 7}  # original anchor's parent and before retain their scope
    else:
        assert seqs == {0, 1, 2, 6, 7}


def test_group_rows_are_labelled_and_the_note_is_added_only_with_them():
    assert extraction.GROUP_REQUESTS_NOTE == (
        Path(__file__).parent / "fixtures" / "intents_group_requests_note.txt").read_text().rstrip("\n")
    rows = [{**row(1, "g", "request", minutes=0), "via_group": True, "read_from": T},
            row(2, "h", "mention", minutes=1, name="另一群")]
    text, _ = render("intents", rows, context=[], participants=PEOPLE, known=[], zone_name=ZONE, text_max_chars=500)
    assert "《发布群》（群 · 全部消息）" in text and "《另一群》（群）" in text
    assert text.count(extraction.GROUP_LABEL) == 1
    base = extraction.instructions("intents", "request", [])
    assert extraction.instructions("intents", "request", [], group=False) == base
    assert extraction.instructions("intents", "request", [], group=True) == base + "\n\n" + extraction.GROUP_REQUESTS_NOTE
    assert extraction.instructions("leads", "request", [], group=True) == "request"


@pytest.mark.parametrize("kind,sources,dropped", [("alert_triage", ["m1"], True), ("custom", ["m1"], True),
    ("dev_task", ["m1"], False), ("bug_triage", ["m1"], False), ("alert_triage", ["m2"], False)])
def test_a_group_row_intent_must_be_a_dev_task_or_a_bug(kind, sources, dropped):
    found = parse("intents", json.dumps({"intents": [intent(type=kind, sources=sources)]}), REFS,
                  now=NOW, valid_days=90, enabled_types=TYPES | {"custom"}, app_seqs=(), group_seqs={101})
    assert len(found.items) == (0 if dropped else 1)
    assert dict(found.dropped) == ({"group_type": 1} if dropped else {})
