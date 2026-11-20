"""``manager.stable_context`` off (no row): the Manager's user turns, composed system prompt, tool listing and process
signature are what they were before the switch existed; on, only the turns' start and the contract's end change.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents.ops.service import MANAGER_TOOLS
from looper.agents.tiers.state_view import STABLE_CONTEXT_CONTRACT, STABLE_CONTEXT_KEY
from tests.db_support import DSN, run_db
from tests.test_leads_manager_default_off import _prepared
from tests.test_manager_tools_db import _converse

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_off_the_manager_is_launched_and_told_as_before_and_on_only_the_turns_start_and_contract_end_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        prepared = _prepared(monkeypatch)
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1", STABLE_CONTEXT_KEY) == 0
        off = await _converse(tmp_path / "off", [[("looper_overview", {})], []], ["hi", "again"])
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", STABLE_CONTEXT_KEY)
        on = await _converse(tmp_path / "on", [[("looper_overview", {})], []], ["hi", "again"])
        before, _, after, _ = prepared

        # The user turns: the sections, the channel line and the message, as _build_user_message writes them.
        assert off.turns == [f"Projects:\n  (none yet)\n\nChannel: the Looper dashboard chat\n\n{text}"
                             for text in ("hi", "again")]
        rev = on.turns[0].partition("\n")[0].removeprefix("Looper state (rev ").removesuffix("):")
        assert on.turns == [f"Looper state (rev {rev}):\n{off.turns[0]}",
                            f"Looper state: unchanged since rev {rev}\n\nChannel: the Looper dashboard chat\n\nagain"]
        # The listing: the catalog as it is, both ways.
        assert off.tools == on.tools == [tool.name for tool in MANAGER_TOOLS]
        # The system prompt: TOOLS_CONTRACT alone; on, the block's contract after it and nothing else changed.
        assert "Looper state" not in before.system_prompt.text
        assert before.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT"
        assert after.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT+STABLE_CONTEXT_CONTRACT"
        assert after.system_prompt.text == before.system_prompt.text + "\n" + STABLE_CONTEXT_CONTRACT
        # The process signature: its parts as before (the listing's hash only when workflows put it there); on, the
        # prompt's hash differs, so turning the switch either way starts a new process.
        workflows = any(tool.name == "looper_task_create" and "workflow" in tool.schema.get("properties", {})
                        for tool in MANAGER_TOOLS)
        assert len(before.signature) == len(after.signature) == 6 + workflows
        assert "Looper state" not in " ".join(before.signature[0])
        assert before.signature[1] == before.system_prompt.sha256 != after.signature[1]

    run_db(test, monkeypatch, timeout=60)
