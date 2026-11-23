"""CLI leads backfill compared with its unchanged main path."""

from __future__ import annotations

import datetime as datetime_module
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import extraction, msgflow_commands, processor, subscriptions
from looper.agents.serve import seed_role_configs
from looper.db import connection, subscriptions as db_subs
from tests.db_support import DSN, run_db
from tests.model_call_support import assert_golden, golden_cli, read_launches, table_rows
from tests.subscription_support import DM, SOURCE, add, source

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
        "'lark', $2)", extraction.LEADS_INSTRUCTIONS, DM)
    await add(pool, "We will ship on Friday.", sent_at=_NOW - timedelta(minutes=5), ingested_at=_NOW)
    fields = subscriptions.preset("leads", source_id=SOURCE, zone_name="UTC")["subscription"]
    await db_subs.create(pool, "schedule-golden", fields)
    await pool.execute("UPDATE source_events SET external_id = 'om_goldensynthetic'")


@pytest.mark.parametrize("case", ["success", "reformat", "invalid", "blocked", "budget", "runtime", "timeout", "unavailable"])
def test_backfill_preserves_launch_records_progress_and_failure_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str,
) -> None:
    reply = json.dumps({"leads": [{"kind": "decision", "summary": "Ship on Friday", "confidence": "high",
                                   "sources": ["m1"]}], "instructions_seen": []})
    runs = ([{"reply": "Unreadable."}, {"reply": reply if case == "reformat" else "Unreadable again."}]
            if case in {"reformat", "invalid"} else [{"mode": case, "reply": reply}])
    executable = golden_cli(monkeypatch, tmp_path, runs=runs)
    monkeypatch.setattr(extraction, "time_", type("Clock", (), {"monotonic": staticmethod(lambda: 10.0)}))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return _NOW

    monkeypatch.setattr(datetime_module, "datetime", FixedDateTime)

    async def test(pool: Any) -> None:
        from looper.agents import source_service

        await _seed(pool)
        if case == "timeout":
            monkeypatch.setattr(processor, "PROCESSOR_TIMEOUT_SEC", 2)
        if case == "unavailable":
            await pool.execute("UPDATE role_configs SET enabled = false WHERE role = 'processor'")

        async def init(*args: Any, **kwargs: Any) -> Any:
            return pool

        async def close() -> None:
            pass

        async def own_apps() -> set[str]:
            return set()

        monkeypatch.setattr(connection, "init_pool", init)
        monkeypatch.setattr(connection, "close_pool", close)
        monkeypatch.setattr(source_service, "own_bot_app_ids", own_apps)
        code, output = await msgflow_commands._backfill("unused-test-database", days=1,
            schedule_id="schedule-golden", run=True, budget_usd=0.1,
            claude_bin=executable, runtime_root=tmp_path / "runtime")
        assert code == (1 if case in {"runtime", "timeout", "unavailable"} else 0)
        assert len(read_launches(tmp_path)) == (0 if case == "unavailable" else len(runs))
        assert await pool.fetchval("SELECT count(*) FROM message_leads") == (1 if case in {"success", "reformat"} else 0)
        settings = [json.loads(row["row"]) for row in await pool.fetch(
            "SELECT to_jsonb(t)::text AS row FROM system_settings t ORDER BY key")]
        assert_golden(f"backfill-{case}", {"launches": read_launches(tmp_path),
            "tables": {**await table_rows(pool, _TABLES), "system_settings": settings},
            "exit_code": code, "output": output}, tmp_path)

    run_db(test, monkeypatch, setup=seed_role_configs)
