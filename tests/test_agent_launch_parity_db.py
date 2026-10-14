"""Every role starts its runtime exactly as before agents: the argv, the environment, the system prompt bytes.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
scenario works in a new schema of its own, dropped afterwards.

``fixtures/agent_launch_parity_main.json`` is what the code before agents
(``main`` at 73eed752d) started each role of each scenario of
``agent_launch_parity.py`` with: it is the output of
``python tests/agent_launch_parity.py <dsn>`` run on that code, so it is not
derived from the code under test. Here each scenario is stored the way that
code stored it (a layer's ``model``), this code's schema upgrades it (each
model becomes an agent), and every role then runs through this code's tiers
and must match the fixture byte for byte.
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

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

MAIN = json.loads((Path(__file__).parent / "fixtures" / "agent_launch_parity_main.json").read_text("utf-8"))

# The tables a scenario is stored in, as the code before agents had them; the upgrade adds the rest.
_BEFORE_AGENTS = """
CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO schema_migrations (name) VALUES ('agent_setting_layers');
CREATE TABLE projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', work_dir TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    background TEXT NOT NULL DEFAULT '');
CREATE TABLE agent_setting_layers (
    scope TEXT NOT NULL CHECK (scope IN ('system', 'role', 'project', 'role_project')),
    role TEXT CHECK (role IN ('manager', 'planner', 'worker', 'reviewer')),
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    harness_prompt TEXT, model TEXT, effort TEXT,
    env JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(env) = 'array'),
    run_budget_usd DOUBLE PRECISION CHECK (run_budget_usd > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX idx_agent_setting_layers_place
    ON agent_setting_layers (scope, COALESCE(role, ''), COALESCE(project_id, ''));
"""


@pytest.mark.parametrize("scenario", sorted(parity.SCENARIOS))
def test_each_role_starts_its_runtime_as_main_did(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    import asyncpg

    from looper.db import connection

    async def _main() -> dict[str, Any]:
        schema = f"looper_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, server_settings={"search_path": schema})
        try:
            await pool.execute(_BEFORE_AGENTS)
            await parity.seed_projects(pool)
            await parity.seed_layers(pool, scenario)
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
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
