"""The extraction subscription's real one-shot runs, compared with unchanged main."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import extraction, processor, subscription_runs, subscriptions
from looper.agents.serve import seed_role_configs
from looper.db import schedules as db_schedules, subscriptions as db_subs
from tests.db_support import DSN, run_db
from tests.model_call_support import assert_golden, golden_cli, read_launches, table_rows
from tests.subscription_support import SOURCE, add, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
_REPLY = json.dumps({"leads": [{"kind": "decision", "summary": "Ship on Friday", "confidence": "high",
                                "sources": ["m1"]}], "instructions_seen": []})
_CASES = {
    "success": ([{"reply": _REPLY}], "completed", "extracted", 1),
    "quiet": ([{"reply": "NONE"}], "completed", "quiet", 0),
    "reformat": ([{"reply": "Unreadable answer."}, {"reply": _REPLY}], "completed", "extracted", 1),
    "invalid": ([{"reply": "Unreadable answer."}, {"reply": "Still unreadable."}], "completed", "skipped", 0),
    "blocked": ([{"mode": "blocked"}], "completed", "skipped", 0),
    "timeout": ([{"mode": "timeout"}], "timed_out", "failed", 0),
    "budget": ([{"mode": "budget"}], "failed", "failed", 0),
    "runtime": ([{"mode": "runtime"}], "failed", "failed", 0),
    "no_result": ([{"mode": "no_result"}], "failed", "failed", 0),
    "unavailable": ([], "failed", "failed", 0),
}


@pytest.mark.parametrize("case", _CASES)
def test_extraction_preserves_launch_prompt_records_and_window_outcome(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runs, status, outcome, leads = _CASES[case]
    executable = golden_cli(monkeypatch, tmp_path, runs=runs, uuid_modules=(processor,))
    # The enclosing retry shares a deadline; keep elapsed wall time outside its prompt and launch contract.
    monkeypatch.setattr(extraction, "time_", type("Clock", (), {"monotonic": staticmethod(lambda: 10.0)}))

    async def test(pool: Any) -> None:
        await source(pool)
        if case == "unavailable":
            await pool.execute("UPDATE role_configs SET enabled = false WHERE role = 'processor'")
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-golden', 'Golden project')")
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        await pool.execute(
            "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, kind, run_budget_usd, "
            "run_timeout_mode, run_timeout_seconds) VALUES ('schedule-golden', 'project-golden', "
            "'Golden extraction', '7 * * * *', 'UTC', $1, 'subscription', 0.1, 'seconds', $2)",
            extraction.LEADS_INSTRUCTIONS, 2 if case == "timeout" else 20)
        fields = subscriptions.preset("leads", source_id=SOURCE, zone_name="UTC")["subscription"]
        await db_subs.create(pool, "schedule-golden", fields)
        seq = await add(pool, "We will ship on Friday.", sent_at=_NOW - timedelta(minutes=5),
                        ingested_at=_NOW - timedelta(minutes=4))
        await pool.execute("UPDATE source_events SET external_id = 'om_goldensynthetic' WHERE seq = $1", seq)
        schedule = await db_schedules.get_schedule("schedule-golden")
        version = await subscriptions.config_version(schedule)
        await db_subs.update(pool, "schedule-golden", {"target_confirmed_at": _NOW,
                                                       "target_confirmed_version": version})
        schedule = await db_schedules.get_schedule("schedule-golden")
        window = await extraction.plan(pool, schedule, now=_NOW, own_apps=())
        assert window is not None and len(window["rows"]) == 1 and window.get("snapshot")
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status) "
                           "VALUES ('run-golden', 'schedule-golden', $1, $1, 'manual', 'running')", _NOW)

        async def own_apps() -> tuple[str, ...]:
            return ()

        runner = subscription_runs.SubscriptionRuns(poster=None, own_apps=own_apps,
            runtime_root=tmp_path / "runtime", clock=lambda: _NOW, claude_bin=executable)
        await runner.record(pool, "run-golden", window)
        more = await runner.execute("run-golden")
        row = await pool.fetchrow("SELECT status FROM schedule_runs WHERE id = 'run-golden'")
        stream = await db_subs.get_run("run-golden")
        assert row["status"] == status and stream["outcome"] == outcome and more is False
        assert await pool.fetchval("SELECT count(*) FROM message_leads") == leads
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id = 'schedule-golden'") \
            == (seq if status == "completed" else 0)
        launches = read_launches(tmp_path)
        assert len(launches) == len(runs)
        rows = await table_rows(pool, ("llm_records", "llm_record_events", "schedules", "schedule_runs",
                                      "subscriptions", "subscription_runs", "message_leads", "message_lead_sources",
                                      "message_lead_people", "schedule_notifications"))
        assert_golden(f"extraction-{case}", {"launches": launches, "tables": rows, "more": more}, tmp_path)

    run_db(test, monkeypatch, setup=seed_role_configs)
