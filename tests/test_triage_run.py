"""Synthetic triage replies: typed validation, exclusive citations and the upward-only code floor."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import triage_run
from looper.agents.triage_model import Category, ReasonKind


def event(**fields):
    return {"category": "digest", "title": "A decision", "summary": "Synthetic release date changed",
            "reason": "Useful for tomorrow", "confidence": "high", "sources": ["m1"], **fields}


def reply(events=(), ignored=(), **fields):
    return json.dumps({"events": list(events), "ignored": list(ignored), "instructions_seen": [], **fields})


@pytest.mark.parametrize("wrap", [lambda s: s, lambda s: f"```json\n{s}\n```", lambda s: f"Result:\n{s}\nDone."])
def test_json_object_can_be_surrounded_by_prose_or_a_fence(wrap):
    parsed = triage_run.parse(wrap(reply([event()])), {"m1": 11})
    assert parsed.state == "items" and parsed.items[0]["window_sources"] == [11]


def test_none_is_exact_and_marks_the_whole_window_ignored_without_items():
    parsed = triage_run.parse("NONE", {"m1": 11, "m2": 12})
    assert parsed.state == "quiet" and parsed.reason == "处理器回复了 NONE" and not parsed.items
    assert triage_run.parse("Nothing: NONE", {"m1": 11}).state == "invalid"


@pytest.mark.parametrize("fields,refs,reason", [
    ({"sources": ["c1"]}, {"m1": 11, "c1": 9}, "no_sources"),
    ({"sources": ["P1"]}, {"m1": 11, "P1": "synthetic-person"}, "bad_ref"),
    ({"sources": ["m9"]}, {"m1": 11}, "bad_ref"),
    ({"category": "ignore"}, {"m1": 11}, "bad_category"),
    ({"title": "x" * 61}, {"m1": 11}, "too_long"),
    ({"summary": "x" * 401}, {"m1": 11}, "too_long"),
    ({"reason": "x" * 101}, {"m1": 11}, "too_long"),
    ({"reason": " "}, {"m1": 11}, "empty_reason"),
    ({"confidence": "certain"}, {"m1": 11}, "bad_confidence"),
    ({"summary": {"text": "wrong shape"}}, {"m1": 11}, "malformed"),
    ({"title": "unstoreable\x00title"}, {"m1": 11}, "malformed"),
])
def test_invalid_items_are_dropped_individually_with_a_typed_reason(fields, refs, reason):
    parsed = triage_run.parse(reply([event(**fields), event(sources=["m1"])]), refs)
    assert len(parsed.items) == 1 and parsed.dropped == {reason: 1}
    assert all(isinstance(key, triage_run.DropReason) for key in parsed.dropped)


def test_highest_category_wins_and_ties_keep_the_first_item():
    parsed = triage_run.parse(reply(
        [event(sources=["m1", "m2"]), event(category="important", title="First", sources=["m1"]),
         event(category="important", title="Second", sources=["m1"])],
        [{"reason": "Routine", "sources": ["m1", "m2", "m3"]}]), {"m1": 11, "m2": 12, "m3": 13})
    by_title = {item["title"]: item for item in parsed.items}
    assert by_title["First"]["window_sources"] == [11]
    assert by_title["A decision"]["window_sources"] == [12]
    assert by_title[""]["window_sources"] == [13]
    assert parsed.dropped == {}
    assert by_title["Second"]["window_sources"] == []
    assert by_title["A decision"]["sources"] == [11, 12]


def test_instructions_seen_only_resolves_known_message_refs():
    parsed = triage_run.parse(reply([event(sources=["m3"])], instructions_seen=["m3", "P1", "m9"]),
                              {"m3": 13, "P1": "synthetic-person"})
    assert parsed.instructions_seen_seqs == [13]


def test_floor_raises_unanswered_dm_and_mention_but_preserves_important_and_answered():
    async def check():
        rows = [{"seq": n, "chat_id": f"chat-{n}", "sender_id": "synthetic-peer", "chat_kind": "p2p",
                 "mentions_me": False, "sender_kind": "user", "origin": "live",
                 "sent_at": datetime(2026, 1, 1, tzinfo=timezone.utc)} for n in range(1, 6)]
        rows[4].update(chat_kind="group", mentions_me=True)
        conn = AsyncMock()
        conn.fetch.return_value = [{"chat_id": "chat-2", "sent_at": datetime(2026, 1, 2, tzinfo=timezone.utc)}]
        parsed = triage_run.parse(reply([event(category="important", sources=["m3"])],
                                       [{"sources": ["m1", "m2", "m4", "m5"], "reason": "Routine"}]),
                                  {f"m{n}": n for n in range(1, 6)})
        raised = await triage_run.raise_floor(conn, parsed, rows, source={"id": "synthetic-source",
                                             "user_open_id": "synthetic-operator"}, window_end_seq=8)
        items = {seq: item for item in raised.items for seq in item["window_sources"]}
        for seq in (1, 4, 5):
            assert items[seq]["category"] == Category.DIGEST
            assert items[seq]["reason_kind"] == ReasonKind.RAISED
        assert items[2]["category"] == Category.IGNORE
        assert items[3]["category"] == Category.IMPORTANT
        assert items[5]["title"] == "未回复的 @你"
        assert items[1]["summary"] == "" and items[1]["confidence"] == "high"
    asyncio.run(check())


@pytest.mark.parametrize("text", ["NONE", "```\nNONE\n```", "```text\nNONE\n```"])
def test_none_accepts_code_fences_and_keeps_a_typed_marker(text):
    parsed = triage_run.parse(text, {"m1": 11})
    assert parsed.state == "quiet" and parsed.none is True


def test_missing_ignored_is_empty_and_items_are_capped_across_both_lists():
    from looper.agents.extraction import ITEMS_MAX

    assert triage_run.parse(json.dumps({"events": [event()]}), {"m1": 11}).state == "items"
    parsed = triage_run.parse(reply([event()] * (ITEMS_MAX - 1),
                                   [{"sources": ["m1"], "reason": "Routine"}] * 3), {"m1": 11})
    assert len(parsed.items) == ITEMS_MAX
    assert parsed.dropped == {"over_limit": 2}
