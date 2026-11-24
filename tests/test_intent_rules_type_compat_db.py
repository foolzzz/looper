"""Residual built-in rule metadata cannot leak through legacy type entry points."""
from __future__ import annotations

import pytest

from looper.agents import extraction, intents, leads_model
from looper.db.intents import IntentRefused
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("TRUE", False), ("invalid", False), ("true", True)])
def test_rules_enabled_reads_only_its_exact_boolean_key(monkeypatch, value, expected):
    async def check(pool):
        if value is not None:
            await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.rules.enabled',$1)", value)
        assert await leads_model.rules_enabled(pool) is expected
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_config") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("builtin,rules_value,intents_on,available,visible", [
    (True, None, True, True, False),
    (True, "false", True, True, False),
    (True, "true", True, False, False),
    (True, "true", False, True, False),
    (True, "true", True, True, True),
    (False, None, True, False, True),
])
def test_legacy_type_entry_points_hide_only_unavailable_builtin_seed(monkeypatch, builtin, rules_value, intents_on, available, visible):
    async def check(pool):
        monkeypatch.setattr(leads_model, "RULE_CLASSIFICATION_AVAILABLE", available)
        await pool.execute("DELETE FROM intent_type_versions WHERE type_key='mr_review'")
        await pool.execute("DELETE FROM intent_types WHERE key='mr_review'")
        await pool.execute("INSERT INTO intent_types(key,version,builtin) VALUES('mr_review',1,$1)", builtin)
        await pool.execute("INSERT INTO intent_type_versions(type_key,version,name,guidance,priority,edited_via) "
                           "VALUES('mr_review',1,'MR review','Classify review requests','normal','builtin')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled',$1)", "true" if intents_on else "false")
        if rules_value is not None:
            await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.rules.enabled',$1)", rules_value)
        for result in ((await intents.types_list())["types"], await extraction.intent_types(pool), (await intents.stats())["types"]):
            assert ("mr_review" in [row["key"] for row in result]) is visible
        for call in (lambda: intents.type_history("mr_review"), lambda: intents.type_save("mr_review", name="Changed"),
                     lambda: intents.type_rollback("mr_review", 1), lambda: intents.type_archive("mr_review"),
                     lambda: intents.set_project_template("mr_review", "missing", None)):
            if not visible:
                with pytest.raises(IntentRefused) as error:
                    await call()
                assert error.value.code == "not_found"
        if not visible:
            # Three legacy built-ins + seven custom types still fit: a hidden seed uses no capacity.
            for n in range(7):
                await intents.type_create(f"custom{n}", name="Custom", guidance="Synthetic requests", priority="normal")
            with pytest.raises(IntentRefused):
                await intents.type_create("overflow", name="Custom", guidance="Synthetic requests", priority="normal")
    run_db(check, monkeypatch)
