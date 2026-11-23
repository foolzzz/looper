"""The default-off intents path, captured before followed-group production changes."""

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
_BASELINE = "9912920e45b6519749547974e953716c0b6c7c32"
_NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
_REPLY = json.dumps({"intents": [{"type": "bug_triage", "title": "Fix synthetic bug", "summary": "Synthetic bug",
                                "confidence": "high", "sources": ["m1"]}], "instructions_seen": []})


@pytest.mark.parametrize("case", ["baseline", "listed_switch_absent", "listed_switch_false", "switch_on_list_empty"])
def test_intents_off_path_matches_main(case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = golden_cli(monkeypatch, tmp_path, runs=[{"reply": _REPLY}], uuid_modules=(processor,))
    monkeypatch.setattr(extraction, "time_", type("Clock", (), {"monotonic": staticmethod(lambda: 10.0)}))

    async def test(pool: Any) -> None:
        await source(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-golden', 'Golden project')")
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('intents.enabled', 'true')")
        await pool.execute(
            "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, kind, run_budget_usd, "
            "run_timeout_mode, run_timeout_seconds) VALUES ('schedule-golden', 'project-golden', "
            "'Golden intents', '*/10 * * * *', 'UTC', $1, 'subscription', 0.3, 'seconds', 20)",
            extraction.INTENTS_INSTRUCTIONS)
        await db_subs.create(pool, "schedule-golden",
                             subscriptions.preset("intents", source_id=SOURCE, zone_name="UTC")["subscription"])
        for n, fields in enumerate([
            {"chat_id": "ou_peer_golden", "kind": "p2p"},
            {"chat_id": "oc_followed_golden"},
            {"chat_id": "oc_followed_golden", "mentions_me": True},
            {"chat_id": "oc_followed_golden", "sender_kind": "app"},
            {"chat_id": "oc_followed_golden", "origin": "import"},
            {"chat_id": "oc_followed_golden", "origin": "history"},
            {"chat_id": "oc_other_golden"},
        ], 1):
            sent = _NOW - timedelta(minutes=[50, 40, 35, 30, 25, 20, 15][n - 1])
            seq = await add(pool, "Synthetic request", sent_at=sent, ingested_at=sent + timedelta(minutes=1), **fields)
            await pool.execute("UPDATE source_events SET external_id = $2 WHERE seq = $1", seq, f"om_golden_{n}")
        if case.startswith("listed_"):
            await pool.execute("INSERT INTO intent_group_reads (source_id, chat_id, added_at, read_from) "
                               "VALUES ($1, 'oc_followed_golden', $2, $2)", SOURCE, _NOW - timedelta(hours=1))
        if case in ("listed_switch_false", "switch_on_list_empty"):
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('intents.group_read.enabled', $1)",
                               "false" if case == "listed_switch_false" else "true")
        schedule = await db_schedules.get_schedule("schedule-golden")
        await db_subs.update(pool, "schedule-golden", {"target_confirmed_at": _NOW,
                            "target_confirmed_version": await subscriptions.config_version(schedule)})
        schedule = await db_schedules.get_schedule("schedule-golden")
        confirmation = await subscriptions.confirmation_text(schedule, now=_NOW, action="确认")
        window = await extraction.plan(pool, schedule, now=_NOW, own_apps=())
        assert window is not None and [r["seq"] for r in window["rows"]] == [1, 3]
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status) "
                           "VALUES ('run-golden', 'schedule-golden', $1, $1, 'manual', 'running')", _NOW)

        async def own_apps() -> tuple[str, ...]:
            return ()

        runner = subscription_runs.SubscriptionRuns(poster=None, own_apps=own_apps,
            runtime_root=tmp_path / "runtime", clock=lambda: _NOW, claude_bin=executable)
        await runner.record(pool, "run-golden", window)
        more = await runner.execute("run-golden")
        tables = await table_rows(pool, ("llm_records", "llm_record_events", "schedules", "schedule_runs",
                                       "subscriptions", "subscription_runs", "intents", "intent_sources"))
        assert_golden("intents-group-read-off", {"window": window, "confirmation": confirmation,
                      "launches": read_launches(tmp_path), "tables": tables, "more": more}, tmp_path,
                      baseline=_BASELINE)

    run_db(test, monkeypatch, setup=seed_role_configs)
