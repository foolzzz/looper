"""Operator-local quota days do not split a DST repeat or reuse a spent unit."""
import asyncio
from datetime import datetime

import pytest

from tests.db_support import run_db
from tests.intent_rule_execution_support import URL, configure, fact


@pytest.mark.parametrize("moments,days", [
    (("2026-11-01T08:30:00+00:00","2026-11-01T09:30:00+00:00"), ["2026-11-01","2026-11-01"]),
    (("2026-03-08T09:30:00+00:00","2026-03-08T10:30:00+00:00"), ["2026-03-08","2026-03-08"]),
    (("2026-11-02T07:59:59+00:00","2026-11-02T08:00:00+00:00"), ["2026-11-01","2026-11-02"]),
])
def test_quota_uses_operator_day_across_dst_and_midnight(monkeypatch,tmp_path,moments,days):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool,tmp_path)
        await pool.execute("UPDATE system_settings SET value='America/Los_Angeles' WHERE key='user.timezone'")
        current=[datetime.fromisoformat(moments[0]),"head-a"]
        monkeypatch.setattr(engine,"_now",lambda:current[0])
        async def verifier(*args,**kwargs):
            return fact(current[1])
        service=engine.IntentRuleService(db=pool,verifier=verifier)
        for index,moment in enumerate(moments):
            current[:]=[datetime.fromisoformat(moment),f"head-{index}"]
            await service.submit_discovery("discovery",f"request-{index}","review-system",URL,principal=OPERATOR)
            await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks")==2
        assert [r["quota_day"].isoformat() for r in await pool.fetch("SELECT quota_day FROM intent_rule_executions ORDER BY id")]==days
        assert await pool.fetchval("SELECT sum(used) FROM intent_rule_project_days")==2
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_project_days")==len(set(days))
        assert set(r["timezone"] for r in await pool.fetch("SELECT timezone FROM intent_rule_project_days"))=={"America/Los_Angeles"}
    run_db(check,monkeypatch)


def test_timezone_change_during_final_verify_confirms_without_quota(monkeypatch,tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool,tmp_path)
        entered,release=asyncio.Event(),asyncio.Event()
        calls=0
        async def verifier(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                entered.set()
                await release.wait()
            return fact()
        service=engine.IntentRuleService(db=pool,verifier=verifier)
        await service.submit_discovery("discovery","request","review-system",URL,principal=OPERATOR)
        worker=asyncio.create_task(service.run_once())
        await asyncio.wait_for(entered.wait(),2)
        from looper.db import system_settings
        await system_settings.put_settings({"user.timezone":"Pacific/Honolulu"},pool=pool)
        release.set()
        await worker
        assert await pool.fetchval("SELECT count(*) FROM tasks")==0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_project_days")==0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work")=="config_changed"
    run_db(check,monkeypatch)
