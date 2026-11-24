"""The leads check's input, reply and verdicts, without a database or a model: what it shows of each item, how a
reply is read (every way it can be wrong), what a verdict changes and how a run's count line says it. Every message,
chat and person is synthetic."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from looper.agents import extraction, leads_check

UTC = timezone.utc
ROWS = {
    1: {"seq": 1, "external_id": "om_1", "chat_id": "oc_team", "chat_kind": "group", "chat_name": "研发群",
        "parent_id": None, "sender_id": "ou_zhang", "sender_kind": "user", "sender_name": "张三",
        "sent_at": datetime(2026, 11, 20, 2, 1, tzinfo=UTC), "text": "登录页的报错谁能看一下？"},
    2: {"seq": 2, "external_id": "om_2", "chat_id": "oc_team", "chat_kind": "group", "chat_name": "研发群",
        "parent_id": "om_1", "sender_id": "ou_user", "sender_kind": "user", "sender_name": "李四",
        "sent_at": datetime(2026, 11, 20, 2, 5, tzinfo=UTC), "text": "我来改，今天下班前"},
}
PEOPLE = extraction.Participants({"ou_zhang": "张三", "ou_user": "李四"}, "ou_user")


def _lead(**fields):
    return {"kind": "commitment", "summary": "李四今天下班前修好登录页的报错", "detail": "", "confidence": "high",
            "sources": [2, 1], "window_sources": [2], "about": [], **fields}


def _render(items, **kw):
    return leads_check.render("leads", items, ROWS, participants=PEOPLE, zone_name="Asia/Shanghai",
                              text_max_chars=500, **kw)


def test_each_item_shows_its_kind_confidence_and_the_messages_it_cites_by_their_own_refs():
    text, refs = _render([_lead(), _lead(kind="fact", summary="登录页有报错", sources=[1], window_sources=[])])
    assert text.splitlines()[:3] == ["参与者:", "P1 张三", "P2 李四（用户本人）"]
    assert "X1 线索 [commitment] 置信度 high：李四今天下班前修好登录页的报错" in text
    # A window message is m, a context one c; a reply points at its parent; a message cited twice keeps its ref.
    assert "[c1] [2026/11/20 10:01:00] P1: 登录页的报错谁能看一下？" in text
    assert "[m1] [2026/11/20 10:05:00] P2 ↩c1: 我来改，今天下班前" in text
    assert text.count("[c1]") == 2 and "《研发群》（群）" in text
    assert {key: value for key, value in refs.items() if key[0] in "XmcP"} == {
        "X1": 0, "X2": 1, "m1": 2, "c1": 1, "P1": "ou_zhang", "P2": "ou_user"}


def test_an_intent_shows_its_type_title_and_summary_and_each_type_is_described_once():
    item = {"type": "bug_triage", "title": "看登录页报错", "summary": "张三请你看", "confidence": "medium",
            "sources": [1], "window_sources": [1]}
    text, _ = leads_check.render("intents", [item, {**item, "title": "又一条"}], ROWS, participants=PEOPLE,
                                 zone_name="Asia/Shanghai", text_max_chars=500,
                                 types={"bug_triage": {"name": "bug 排查", "guidance": "有人报告问题"}})
    assert "意图类型（每条意图按它的类型说明判断）:\n- bug_triage bug 排查: 有人报告问题\n待复核的条目" in text
    assert text.count("有人报告问题") == 1
    assert "X1 意图 [bug_triage bug 排查] 置信度 medium：看登录页报错\n摘要：张三请你看" in text


def test_items_past_the_input_limit_are_left_out_and_so_unchecked(monkeypatch):
    monkeypatch.setattr(leads_check, "INPUT_MAX_CHARS", 300)
    text, refs = _render([_lead(), _lead(summary="第二条")])
    assert "X1" in refs and "X2" not in refs and "第二条" not in text


def _reply(*verdicts, **extra):
    return json.dumps({"verdicts": list(verdicts), **extra}, ensure_ascii=False)


REFS = {"X1": 0, "X2": 1, "m1": 2, "c1": 1}


@pytest.mark.parametrize("reply", [
    "NONE",
    "not json",
    "[]",
    json.dumps({"verdict": []}),
    _reply({"item": "X9", "verdict": "keep"}),  # no item it names was shown
    "```json\n" + _reply({"item": "X1", "verdict": "maybe"}) + "\n```",
])
def test_a_reply_that_gives_no_readable_verdict_fails_the_check(reply):
    assert leads_check.parse(reply, REFS).state == "failed"


@pytest.mark.parametrize("bad", [
    None,  # no verdict for X2 at all
    {"item": "X2", "verdict": "fix"},  # fix without its new confidence
    {"item": "X2", "verdict": "fix", "confidence": "certain"},
    {"item": "X2", "verdict": "keep", "confidence": "very"},
    {"item": "X2", "verdict": "discard"},
    {"item": "X2", "verdict": "drop", "reason": ["no"]},
    {"item": 2, "verdict": "drop"},
])
def test_a_missing_or_bad_verdict_leaves_only_that_item_unchecked(bad):
    found = leads_check.parse(_reply({"item": "X1", "verdict": "keep"}, *([bad] if bad else [])), REFS)
    assert found.state == "checked" and set(found.verdicts) == {0}


def test_a_reason_past_its_length_is_cut_and_keeps_its_verdict():
    found = leads_check.parse(_reply({"item": "X1", "verdict": "drop", "reason": "无" * 250}), REFS)
    assert found.verdicts[0] == leads_check.Verdict("drop", "无" * leads_check.REASON_MAX)


def test_two_verdicts_for_one_item_leave_it_unchecked():
    found = leads_check.parse(_reply({"item": "X1", "verdict": "keep"}, {"item": "X2", "verdict": "keep"},
                                     {"item": "X2", "verdict": "drop"}), REFS)
    assert set(found.verdicts) == {0}


def test_unknown_fields_are_ignored_and_instructions_seen_names_messages_only():
    found = leads_check.parse(_reply({"item": "X1", "verdict": "fix", "confidence": "low", "reason": " 只是猜测 ",
                                      "tool": "Bash"},
                                     {"item": "X2", "verdict": "drop", "confidence": ""},
                                     instructions_seen=["m1", "X1", "m7", 3], note="ignored"), REFS)
    assert found.verdicts == {0: leads_check.Verdict("fix", "只是猜测", "low"), 1: leads_check.Verdict("drop")}
    assert found.instructions_seen_seqs == [2]


def test_only_a_verdict_changes_an_item():
    items = [_lead(), _lead(), _lead(), _lead(), _lead()]
    checked = leads_check.Checked("checked", verdicts={
        0: leads_check.Verdict("keep"), 1: leads_check.Verdict("fix", "猜测", "low"),
        2: leads_check.Verdict("drop", "出处没说"), 3: leads_check.Verdict("fix", "没问题", "high")})
    leads_check.apply(items, checked, record_id=7)
    # A fix to the confidence it already has changes nothing: it is a keep.
    assert [item.get("check") for item in items] == [
        {"verdict": "keep", "reason": None, "record_id": 7}, {"verdict": "fix", "reason": "猜测", "record_id": 7},
        {"verdict": "drop", "reason": "出处没说", "record_id": 7}, {"verdict": "keep", "reason": "没问题", "record_id": 7},
        None]
    assert [item["confidence"] for item in items] == ["high", "low", "high", "high", "high"]
    assert leads_check.counts(checked, cost_usd=0.00031) == {
        "checked": 4, "check_kept": 2, "check_dropped": 1, "check_fixed": 1, "check_failed": False,
        "check_cost_usd": 0.00031}


def test_a_failed_check_changes_no_item_and_counts_as_failed():
    items = [_lead()]
    leads_check.apply(items, leads_check.Checked("failed", "the reply is not a JSON object"), record_id=7)
    assert "check" not in items[0]
    assert leads_check.counts(None, cost_usd=None)["check_failed"] is True


def test_the_count_line_says_what_the_check_did_only_when_it_is_on():
    counts = {"new": 2, "merged": 0, "superseded": 0, "dropped": {}, "instructions_seen": 0, "more": False}
    assert extraction.count_line("leads", counts) == "新线索 2，合并 0，推翻 0，丢弃 0"
    checked = {**counts, "checked": 3, "check_kept": 1, "check_dropped": 1, "check_fixed": 1, "check_failed": False,
               "check_cost_usd": 0.0003}
    assert extraction.count_line("leads", checked) == (
        "新线索 2，合并 0，推翻 0，丢弃 0；复核 3 条：保留 1，复核丢弃 1，改置信度 1")
    failed = {**checked, "checked": 0, "check_kept": 0, "check_dropped": 0, "check_fixed": 0, "check_failed": True}
    assert extraction.count_line("intents", failed).endswith("；复核没有完成，其余条目未经复核入库")


def test_an_unpriced_check_keeps_its_cost_unknown():
    assert leads_check.counts(leads_check.Checked("checked"), cost_usd=None)["check_cost_usd"] is None
