"""Why a message was or was not reported: parsing, the typed lines, and what stays unchanged.

The recorded message-stream operations and the subscription run view are the values of
``eec8557e0102ce5f4f467cb38d85ce35215a10ae`` (the parent this change is built on). A leads or
intents count object has no ``important`` key, so its view stays that baseline.
"""

import json
from datetime import datetime, timezone

import pytest

from looper import cli
from looper.agents import api_v1
from looper.agents.action_levels import ActionLevel
from looper.agents.schedules import subscription_run_view
from looper.agents.triage_why import NOT_HANDED, Case, UnusableRef, WhyAnswer, format_lines, parse_ref
from looper.agents.v1_domains import message_stream

PARENT = "eec8557e0102ce5f4f467cb38d85ce35215a10ae"
WHEN = datetime(2026, 11, 23, 12, 5, tzinfo=timezone.utc)
# Recorded from message_stream.OPERATIONS at PARENT. This change does not edit that module.
MESSAGE_STREAM_AT_PARENT = (
    ("message_stream.status", "Read the message stream's sources: mode, health, counts and the last run.",
     "read", True, None, "inside"),
    ("message_stream.runs", "List a source's recent ingest runs, newest first.", "read", True, None, "inside"),
    ("message_stream.configure",
     "Create the message source, point it at an outside collector's directory, or turn bridging on or off.",
     "write", True, None, "inside"),
    ("message_stream.chats", "List chats and their recent event counts.", "read", True, None, "inside"),
    ("message_stream.chat_update",
     "Include or exclude a chat from ingestion and all future subscription windows.", "write", True, None, "inside"),
    ("message_stream.delete_preview",
     "Preview deletion and its limits, then request a short-lived confirmation token.", "read", True, None, "inside"),
    ("message_stream.delete", "Confirm a previewed deletion and keep a tombstone against reimport.",
     "high", True, None, "inside"),
)
# subscription_run_view's keys at PARENT, before a triage run grew extract_counts.
RUN_AT_PARENT = {
    "from_seq": 10, "to_seq": 31, "events_in_window": 21, "events_selected": 18, "events_too_old": 2,
    "events_over_cap": 1, "record_id": None, "outcome": "dashboard_only", "output_text": "- gossip",
    "notification_id": None, "skip_reason": None,
}


def _operation(op):
    return (op.operation_id, op.summary, op.risk, op.operator_only, op.tool, op.level.value)


def test_a_reference_is_a_seq_a_message_id_or_a_link():
    assert parse_ref("42").seq == 42 and parse_ref("  42 ").kind == "seq"
    assert parse_ref("om_AbC123").external_id == "om_AbC123"
    link = "https://example.invalid/open?openMessageId=om_FromQuery"
    assert parse_ref(link).external_id == "om_FromQuery"
    assert parse_ref("https://example.invalid/messages/om_InPath99").external_id == "om_InPath99"
    assert parse_ref("http://example.invalid/m?message_id=om_plain").external_id == "om_plain"
    assert parse_ref("https://example.invalid/m?open_message_id=om_snake").external_id == "om_snake"
    for garbage in ("", "   ", "hello world", "om_has space", "om_x\x01",
                    "https://applink.feishu.cn/client/chat/open?openChatId=oc_synthetic",
                    "9223372036854775808"):
        with pytest.raises(UnusableRef):
            parse_ref(garbage)


def test_each_case_is_one_chinese_fact_per_line():
    off = WhyAnswer(case=Case.TRIAGE_OFF, cause="disabled")
    assert format_lines(off) == ["分流没开", "原因：开关关闭"]
    assert off.as_dict() == {"case": "triage_off", "cause": "disabled"}
    missing = WhyAnswer(case=Case.TRIAGE_OFF, cause="no_subscription")
    assert format_lines(missing) == ["分流没开", "原因：没有分流订阅"]
    scoped = WhyAnswer(case=Case.OUT_OF_SCOPE, rule="chat_excluded")
    assert format_lines(scoped) == ["不在分流范围", "规则：会话被排除"]
    waiting = WhyAnswer(case=Case.NOT_YET, cursor_seq=4, next_run_at=WHEN)
    assert format_lines(waiting) == ["还没轮到这条消息", "游标：4", "下次运行：2026-11-23T12:05:00+00:00"]
    assert waiting.as_dict()["next_run_at"] == "2026-11-23T12:05:00+00:00"
    assert WhyAnswer(case=Case.NOT_YET, cursor_seq=4).as_dict()["next_run_at"] is None
    skipped = WhyAnswer(case=Case.SKIPPED, skip_reason="invalid_reply", run_id="run-1")
    assert format_lines(skipped) == ["这一段被跳过", "跳过原因：invalid_reply", "运行：run-1"]
    event = WhyAnswer(case=Case.EVENT, event_id="E-7", category="important", reason="synthetic-reason-delta",
                      reason_kind="model", handling=NOT_HANDED)
    assert format_lines(event) == [
        "事件：E-7", "类别：重要", "理由：synthetic-reason-delta", "理由来源：模型给出", NOT_HANDED]
    assert "seq" not in event.as_dict() and event.as_dict()["handling"] == NOT_HANDED
    raised = WhyAnswer(case=Case.EVENT, event_id="E-8", category="digest", reason="代码保底", reason_kind="raised")
    assert format_lines(raised)[-1] == "理由来源：代码调高" and "handling" not in raised.as_dict()
    uncited = WhyAnswer(case=Case.UNCITED, run_id="run-2")
    assert format_lines(uncited) == ["忽略", "未引用：处理器没有提到这条消息", "运行：run-2"]
    unknown = WhyAnswer(case=Case.UNKNOWN, checked=("in_scope", "covering_runs=0"))
    assert format_lines(unknown) == ["无法判断", "已核对：in_scope、covering_runs=0"]


def test_message_stream_and_other_runs_stay_as_recorded_at_the_parent():
    assert tuple(_operation(op) for op in message_stream.OPERATIONS) == MESSAGE_STREAM_AT_PARENT
    assert subscription_run_view(RUN_AT_PARENT) == RUN_AT_PARENT
    leads = {**RUN_AT_PARENT, "extract_counts": {"new": 1, "merged": 2}}
    assert subscription_run_view(leads) == RUN_AT_PARENT
    assert subscription_run_view({**RUN_AT_PARENT, "extract_counts": json.dumps({"new": 1})}) == RUN_AT_PARENT
    assert subscription_run_view({**RUN_AT_PARENT, "extract_counts": "not-json"}) == RUN_AT_PARENT
    counts = {"important": 1, "digest": 0, "ignore": 2, "uncited": 1, "raised": 0, "none": False, "dropped": {}}
    view = subscription_run_view({**RUN_AT_PARENT, "extract_counts": counts})
    assert view["extract_counts"] == counts and set(view) == set(RUN_AT_PARENT) | {"extract_counts"}
    assert subscription_run_view({**RUN_AT_PARENT, "extract_counts": json.dumps(counts)})["extract_counts"] == counts


def test_why_is_an_operator_read_with_no_manager_tool():
    operation = api_v1.find("POST", "/api/v1/triage/why")
    assert operation is not None and operation.operation_id == "triage.why"
    assert operation.tool is None and operation.operator_only and operation.risk == "read"
    assert operation.level is ActionLevel.INSIDE
    assert operation.summary == "Say why a stored message was or was not reported."
    assert operation.description.startswith(operation.summary)
    assert operation.result["required"] == ["case"]


def test_help_lists_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["msgflow", "why", "--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "3 triage is off" in text and "5 no such message" in text and "6 database unusable" in text


def test_a_missing_config_is_exit_6(tmp_path, capsys):
    code = cli.main(["msgflow", "why", "1", "--config", str(tmp_path / "missing.toml"),
                     "--runtime-root", str(tmp_path / "runtime")])
    assert code == 6
    assert capsys.readouterr().err.strip()
