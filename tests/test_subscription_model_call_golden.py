"""Subscription output compared with its unchanged main path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import subscription_runs, subscriptions
from looper.agents.serve import seed_role_configs
from looper.db import schedules as db_schedules, subscriptions as db_subs
from tests.db_support import DSN, run_db
from tests.model_call_support import assert_golden, golden_cli, read_launches, table_rows
from tests.subscription_support import DM, SUBSCRIPTION, Poster, add, lark_targets, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
_TABLES = ("llm_records", "llm_record_events", "schedules", "schedule_runs", "subscriptions", "subscription_runs",
           "subscription_deliveries", "schedule_notifications", "message_leads", "message_lead_sources")


async def _seed(pool: Any) -> None:
    await source(pool)
    await pool.execute("INSERT INTO projects (id, name) VALUES ('project-golden', 'Golden project')")
    await pool.execute(
        "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, kind, run_budget_usd, "
        "run_timeout_mode, run_timeout_seconds, notify_channel, notify_chat) VALUES ('schedule-golden', "
        "'project-golden', 'Golden subscription', '7 * * * *', 'UTC', $1, 'subscription', 0.1, 'seconds', 20, "
        "'lark', $2)", "Summarize the new messages.", DM)
    await db_subs.create(pool, "schedule-golden", SUBSCRIPTION)
    await pool.execute("UPDATE source_events SET external_id = 'om_goldensynthetic'")


@pytest.mark.parametrize(("case", "reply", "status", "outcome"), [
    ("success", "The build is green.", "completed", "delivered"),
    ("quiet", "SKIP", "completed", "quiet"),
    ("mentions_only", "@_user_1", "failed", "output_invalid"),
    ("blocked", "", "completed", "skipped"),
    ("timeout", "", "timed_out", "failed"),
    ("budget", "", "failed", "failed"),
    ("runtime", "", "failed", "failed"),
    ("unavailable", "", "failed", "failed"),
])
def test_subscription_preserves_launch_records_delivery_and_failure_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, reply: str, status: str, outcome: str,
) -> None:
    executable = golden_cli(monkeypatch, tmp_path, runs=[{"mode": case, "reply": reply}],
                            uuid_modules=(db_schedules,))

    async def test(pool: Any) -> None:
        await lark_targets(pool)
        await _seed(pool)
        seq = await add(pool, "The build passed.", sent_at=_NOW - timedelta(minutes=5), ingested_at=_NOW)
        await pool.execute("UPDATE source_events SET external_id = 'om_goldensynthetic'")
        if case == "timeout":
            await pool.execute("UPDATE schedules SET run_timeout_seconds = 2")
        if case == "unavailable":
            await pool.execute("UPDATE role_configs SET enabled = false WHERE role = 'processor'")
        schedule = await db_schedules.get_schedule("schedule-golden")
        version = await subscriptions.config_version(schedule)
        await db_subs.update(pool, "schedule-golden", {"target_confirmed_at": _NOW,
                                                       "target_confirmed_version": version})
        schedule = await db_schedules.get_schedule("schedule-golden")
        async def own_apps() -> set[str]:
            return set()

        poster = Poster()
        runner = subscription_runs.SubscriptionRuns(poster=poster, own_apps=own_apps, runtime_root=tmp_path / "runtime",
                                                    clock=lambda: _NOW, claude_bin=executable)
        window = await runner.plan(pool, schedule, now=_NOW, scheduled_for=_NOW)
        assert window is not None and len(window["rows"]) == 1
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status) "
                           "VALUES ('run-golden', 'schedule-golden', $1, $1, 'manual', 'running')", _NOW)
        await runner.record(pool, "run-golden", window)
        assert await runner.execute("run-golden") is False
        assert await pool.fetchval("SELECT status FROM schedule_runs") == status
        assert (await db_subs.get_run("run-golden"))["outcome"] == outcome
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions") == (seq if status == "completed" else 0)
        assert len(poster.sent) == (1 if case == "success" else 0)
        assert_golden(f"subscription-{case}", {"launches": read_launches(tmp_path),
            "tables": await table_rows(pool, _TABLES), "posts": [
                {**sent, "target": vars(sent["target"])} for sent in poster.sent]}, tmp_path)

    run_db(test, monkeypatch, setup=seed_role_configs)
