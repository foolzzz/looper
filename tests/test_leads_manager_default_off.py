"""Leads off (no ``leads.enabled`` row): the default Manager's tool listing, composed system prompt and process
signature are what they were before leads existed; on, each changes, and only by the leads search.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents.ops import leads as ops_leads
from looper.agents.ops.service import MANAGER_TOOLS
from tests.db_support import DSN, run_db
from tests.test_manager_tools_db import _converse

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TERMS = ("leads", "飞书消息线索")


def _prepared(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every turn the Manager prepares to run (its system prompt and its process signature)."""
    from looper.agents.tiers.manager import ManagerAgent

    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
    return prepared


def test_off_the_manager_is_launched_as_before_and_on_only_the_leads_search_is_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        prepared = _prepared(monkeypatch)
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'leads.%'") == 0
        off = await _converse(tmp_path / "off", [[("looper_overview", {})]], ["hi"])
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        on = await _converse(tmp_path / "on", [[("looper_overview", {})]], ["hi"])
        before, after = prepared

        # The listing: the catalog as it is, in its order; on, the search is the one tool added.
        assert off.tools == [tool.name for tool in MANAGER_TOOLS]
        assert on.tools == [*off.tools, ops_leads.LEADS_TOOL]
        # The system prompt: TOOLS_CONTRACT alone; on, the leads contract after it and nothing else changed.
        assert not any(term in before.system_prompt.text for term in TERMS)
        assert before.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT"
        assert after.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT+LEADS_CONTRACT"
        assert after.system_prompt.text.replace("\n" + ops_leads.LEADS_CONTRACT, "", 1) == before.system_prompt.text
        # The process signature: the command, the prompt, the environment, the library, the binding and the
        # recording route, plus the listing's hash only when templates or workflows put it there before leads.
        workflows = any(tool.name == "looper_task_create" and "workflow" in tool.schema.get("properties", {})
                        for tool in MANAGER_TOOLS)
        assert len(before.signature) == 6 + workflows
        assert not any(term in " ".join(before.signature[0]) for term in TERMS)
        assert len(after.signature) == 7 and after.signature != before.signature

    run_db(test, monkeypatch, timeout=60)
