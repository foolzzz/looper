"""Every role starts its runtime exactly as before agents: the argv, the environment, the system prompt bytes.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
scenario works in a new schema of its own, dropped afterwards.

``fixtures/agent_launch_parity_main.json`` is what the code before agents
(``main`` at 73eed752d) started each role of each scenario of
``agent_launch_parity.py`` with: it is the output of
``python tests/agent_launch_parity.py <dsn>`` run on that code, so it is not
derived from the code under test. Here the same scenarios run through this
code's tiers and must match it byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

import agent_launch_parity as parity

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

MAIN = json.loads((Path(__file__).parent / "fixtures" / "agent_launch_parity_main.json").read_text("utf-8"))


@pytest.mark.parametrize("scenario", sorted(parity.SCENARIOS))
def test_each_role_starts_its_runtime_as_main_did(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    import asyncpg

    from loopx.db import connection

    async def _main() -> dict[str, Any]:
        schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await parity.seed_projects(pool)
            await parity.seed_layers(pool, scenario)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                monkeypatch.setenv("HOME", str(root / "home"))
                monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
                await parity.seed_rest(scenario, root)
                return await parity.capture(root, scenario)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    captured = asyncio.run(_main())
    assert sorted(captured) == sorted(MAIN[scenario])
    for case, expected in MAIN[scenario].items():
        assert captured[case] == expected, case
