"""Channel notices carry counts; the dashboard keeps the measured result."""
from collections import Counter

import pytest

from looper.agents import acceptance_result as result, completion_events, task_notice
from looper.agents.links import DashboardUrl
from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS, manager_address


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_notice_counts_follow_delivery_before_extra_and_link(group, status):
    section = "## Evidence\n- s1 c1 (criterion 1): passed at abcdef123456"
    evidence = result.ResultEvidence(section, result.CheckCounts(Counter({(result.ResultState.PASSED, True): 1})))
    delivery = "## Process record\nDelivered locally."
    summary = "## Delivered\nFixture ready.\n\n## Open issues\nNone.\n\n" + section + "\n\n" + delivery
    text = task_notice.notice_text("fixture", "Measure", status, summary,
        channel=task_notice.NotifyChannel.LARK, group=group, evidence=evidence,
        delivery_summary=delivery, extra="Branch fixture", dashboard_url=DashboardUrl("https://dashboard.example", public=True))
    assert text.count("Checks: 1 passed") == 1
    assert section not in text and "abcdef123456" not in text
    assert text.count(delivery) == 1
    assert text.index(delivery) < text.index("Checks:") < text.index("Branch fixture") < text.index("https://")
    assert section in task_notice.web_end_text(status, "Measure", summary, delivery)


def test_group_removes_only_the_exact_payload_section():
    original = "## Delivered\nFixture\n\n## Evidence\nAuthor-written heading."
    measured = "## Evidence\n- s1 c1 (criterion 1): not run"
    evidence = result.ResultEvidence.from_payload({"section": measured, "line": "Checks: 1 not run"})
    text = task_notice.notice_text("fixture", "Measure", "completed", original + "\n\n" + measured,
                                  channel=task_notice.NotifyChannel.TELEGRAM, group=True, evidence=evidence)
    assert original in text and measured not in text


@pytest.mark.parametrize("raw", [None, {}, {"section": [], "line": "Checks: 1 passed"}])
def test_malformed_payload_has_no_notice_line(raw):
    kwargs = dict(channel=task_notice.NotifyChannel.TELEGRAM)
    assert task_notice.notice_text("f", "Measure", "failed", "Failed", **kwargs,
        evidence=result.ResultEvidence.from_payload(raw)) == task_notice.notice_text("f", "Measure", "failed", "Failed", **kwargs)


def test_completion_facts_do_not_forward_acceptance_payload():
    payload = {"task_id": "fixture", "status": "completed", "summary": "## Delivered\nReady"}
    def envelope(payload):
        return MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=manager_address("fixture"),
                                      message_type=MessageType.GOAL_STATUS, payload=payload)
    assert completion_events.facts(envelope(payload)) == completion_events.facts(envelope({**payload,
        "acceptance": {"section": "## Evidence\nFixture", "line": "Checks: 1 passed"}}))
