"""Audience notifications use the real scheduler and outbox, with a recording sender."""

import pytest

from looper.agents.schedule_notify import Kind
from looper.db import schedules as db_schedules
from tests.db_support import DSN
from tests.schedule_support import ScheduleEnv, TELEGRAM_CHAT, allow_telegram, run_scheduled, with_sender

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("notify,status,expected", [
    ("always", "completed", 1), ("failures", "completed", 0),
    ("failures", "failed", 1), ("never", "failed", 0), ("alerts", "failed", 0),
])
def test_audience_run_delivery_and_dashboard_alerts(monkeypatch, tmp_path, notify, status, expected):
    async def test(env: ScheduleEnv):
        sender = with_sender(env)
        await allow_telegram(env)
        schedule = await env.schedule(notify=notify, notify_format="deliverable",
                                      notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT})
        sid = schedule["schedule_id"]
        if status == "completed":
            run_id = await env.scheduler.run_now(sid)
            run = await db_schedules.get_run(run_id)
            await env.end_task(run["task_id"], "completed", summary="## Delivered\n消息\n正文\n## Verification\n通过")
            await env.scheduler.idle()
        else:
            run_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                                   trigger="manual", status=status,
                                                   error="连接失败", finished_at=env.clock.now)
            await env.scheduler._notifier.run_ended(run_id)
        assert len(sender.sent) == expected
        if expected:
            assert sender.sent[0][2] == ("消息\n正文" if status == "completed" else
                                        "今天的「" + schedule["name"] + "」没能按时生成。\n原因：\n连接失败")
        saved = await db_schedules.get_schedule(sid)
        # All alert/recovery kinds share this routing boundary; none may call the sender.
        for kind in Kind:
            if kind is not Kind.RUN:
                await env.scheduler._notifier._deliver(saved, kind, "operator alert", run_id=run_id)
        notices = await db_schedules.list_notifications(sid, limit=100, pool=env.pool)
        assert len(sender.sent) == expected
        alerts = [row for row in notices if row["kind"] != "run"]
        assert len(alerts) == len(Kind) - 1
        assert all(row["status"] == "dashboard" and row["error"] is None for row in alerts)
    run_scheduled(test, monkeypatch, tmp_path)
