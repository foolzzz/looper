"""Recovery commits idle-check states before the supervisor can replay their wakes."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import db, ops_config
from looper.agents import operator_notes, profile_registry, serve
from looper.db import idle_checks as store
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_idle_recovery_finishes_before_supervisor_replay(monkeypatch, tmp_path):
    class Checked(Exception):
        pass

    async def run(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('llmrec.enabled', 'false')")
        ids = []
        for _ in range(2):
            ids.append(await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
                items=[{"section": "logins", "key": "login:startup", "title": "Synthetic login"}],
                channel_id="channel-lark", chat="ou_synthetic_startup"))
        await pool.execute("UPDATE idle_checks SET state='sending', finished_at=now() WHERE id=$1", ids[1])
        called = False

        async def replay(_):
            nonlocal called
            called = True
            rows = [await store.get(check_id) for check_id in ids]
            assert [(row["state"], row["reason"]) for row in rows] == [("failed", "restart"), ("uncertain", None)]
            assert await pool.fetchval("SELECT outcome FROM proactive_deliveries WHERE dedupe_key=$1",
                                       f"idle-{ids[1]}") == "unknown"
            raise Checked

        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_enabled_bot_rows", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_advisory_startup_checks", AsyncMock())
        monkeypatch.setattr(serve.AgentSupervisor, "start_all", replay)
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=
            serve.self_check.Check("database", "Database", "ok", "ready")))
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN,
                                   host="127.0.0.1", port=0, operator_notes_root=str(tmp_path))
        with pytest.raises(Checked):
            await serve._run_agent_system(ops=ops, runtime_root=tmp_path, work_dir=tmp_path, skip_backup=True,
                                         runner_supervisor=SimpleNamespace(stop=AsyncMock()))
        assert called, "startup must reach the supervisor replay boundary"

    run_db(run, monkeypatch, timeout=60)
