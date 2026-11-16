"""Audience delivery and operator report text contracts."""

from datetime import datetime, timezone

import pytest

from looper.agents.schedule_notify import run_text
from looper.db.schedules import FAILURES


def message(summary, *, status="completed", error=None, format="deliverable"):
    return run_text({"name": "每日分享", "notify_format": format, "timezone": "UTC",
                     "id": "example", "project_id": "example"}, "Example", {
        "status": status, "summary": summary, "error": error, "cost_usd": 0.25,
        "task_id": "example", "scheduled_for": datetime(2030, 1, 1, tzinfo=timezone.utc),
    }, base="https://looper.example")


@pytest.mark.parametrize("summary,expected", [
    ("## Delivered\n\n第一段\n\n第二段\n\n## How to use\n不要发送\n## Verification\n不要发送", "第一段\n\n第二段"),
    ("第一段\n第二段", "第一段\n第二段"),
    ("\n## Daily message\n\n## Greeting\n\n第一段\n第二段\n", "第一段\n第二段"),
])
def test_deliverable_success(summary, expected):
    assert message(summary) == expected


def test_deliverable_long_text_stops_at_line_boundary():
    line = "内容" * 300
    result = message("\n".join([line] * 10))
    assert len(result) <= 4000
    assert result == "\n".join([line] * 6 + ["…"])
    assert message("x" * 4001) == "…"


@pytest.mark.parametrize("status", FAILURES)
def test_deliverable_failure_uses_closing_reason_only(status):
    result = message("## Why it ended\n\n### 请求失败\n稍后重试\n\n## Done\n无\n## Not done\n无", status=status)
    assert result == "今天的「每日分享」没能按时生成。\n原因：\n请求失败\n稍后重试"


def test_deliverable_failure_falls_back_to_error_and_limits_reason():
    assert message("## Done\n无", status="gate_failed", error="连接失败\n请重试") == (
        "今天的「每日分享」没能按时生成。\n原因：\n连接失败\n请重试")
    result = message(None, status="failed", error="\n".join(["x" * 300] * 4))
    assert result.split("原因：\n")[1] == "x" * 300 + "\n" + "x" * 300 + "\n…"


def test_report_preserves_lines_and_operator_details():
    result = message("## Delivered\n\n第一段\n第二段", format="report")
    assert "## Delivered\n第一段\n第二段\n花费 $0.25 · https://looper.example" in result
    for summary in ("\n".join(str(n) for n in range(20)), "\n".join(["x" * 300] * 5)):
        body = message(summary, format="report").splitlines()[1:-1]
        assert len(body) <= 12
        assert len("\n".join(body)) <= 1200
        assert body[-1].endswith("…")
    assert message("x" * 1201, format="report").splitlines()[1] == "x" * 1199 + "…"
