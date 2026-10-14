"""A v2.6.0 database with realistic rows, and what every role starts its runtime with on it.

``V2_6_0_ROWS`` fills the tables of ``fixtures/schema_v2_6_0.sql`` (the
schema of the v2.6.0 release, ``git show v2.6.0:looper/db/schema.sql``) the
way that release stored them: role configs keyed by ``agent_id`` (with
``role`` repeating it), registered agents, settings layers and a task's
overrides that name them by ``agent_id``, and saved sessions that name theirs
by ``session_agent_id``. SQL only, so it runs before any newer schema renames
those names.

``capture(root)`` runs every role of ``CASES`` through the agent tiers with a
stand-in ``claude`` (``agent_launch_parity.capture``) on the database the
caller set up and migrated. Run as a script on the code before the rename
(``main`` at 0c5d5ab8a), it prints the captures as JSON: that is how
``fixtures/profile_rename_parity_main.json`` was made
(``tests/test_profile_rename_migration_db.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import agent_launch_parity as parity  # noqa: E402

SCHEMA_V2_6_0 = Path(__file__).parent / "fixtures" / "schema_v2_6_0.sql"
PROJECT_A, PROJECT_B = parity.PROJECT_A, parity.PROJECT_B
TASK_A, TASK_B = parity.TASKS[PROJECT_A], parity.TASKS[PROJECT_B]
API_KEY = "sk-test-rename-parity-0123456789"

# Every role, outside and in both projects; the Planner, Workers and Reviewers of project A run task A, which chose
# a profile of its own for its Workers and Reviewers.
CASES = ["manager", *(f"{role}@{project}" for project in (PROJECT_A, PROJECT_B)
                      for role in ("planner", "worker", "reviewer"))]

V2_6_0_ROWS = f"""
INSERT INTO projects (id, name, background) VALUES
    ('{PROJECT_A}', 'calc', 'Calc background.'),
    ('{PROJECT_B}', 'docs', '');
INSERT INTO tasks (id, requirement, project_id, work_dir, status) VALUES
    ('{TASK_A}', '{parity.REQUIREMENT}', '{PROJECT_A}', '/work/calc', 'running'),
    ('{TASK_B}', '{parity.REQUIREMENT}', '{PROJECT_B}', '/work/docs', 'running');

-- Each role's own settings, as v2.6.0's seeding and the Agents page left them: role repeats the key.
INSERT INTO agent_configs (agent_id, display_name, role, enabled, system_prompt, default_prompt, max_turns,
                           timeout_seconds, max_concurrency) VALUES
    ('manager', 'Manager', 'manager', true, 'Manage, edited.', 'Manage.', 0, 0, 1),
    ('planner', 'Planner', 'planner', true, 'Plan the task.', 'Plan the task.', 0, 1200, 1),
    ('worker', 'Worker', 'worker', true, 'Work carefully.', 'Work.', 0, 86400, 3),
    ('reviewer', 'Reviewer', 'reviewer', false, 'Review.', 'Review.', 5, 86400, 2);

-- A runtime registered by hand before runtimes were built in, which a profile still uses.
INSERT INTO agent_runtimes (id, name, type, executable, capabilities, builtin) VALUES
    ('rt-custom-beta', 'claude-beta', 'claude-code', '',
     ARRAY['add_dirs', 'autonomous_turns', 'claude_plugins', 'compaction_events', 'cost_report', 'mcp',
           'resident_session', 'resume', 'run'], false);
-- An API key provider with a priced catalog, a variable of its own and a concurrency limit.
INSERT INTO llm_providers (id, name, protocols, auth, base_url, models, env, max_concurrency) VALUES
    ('prov-glm000000001', 'glm', ARRAY['anthropic-messages'], 'api_key', 'https://open.bigmodel.cn/api/anthropic',
     '[{{"name": "glm-5.3[1m]", "context_window": 1000000, "input_usd_per_mtok": 0.6, "output_usd_per_mtok": 2.2,
        "cache_read_usd_per_mtok": 0.11, "cache_write_usd_per_mtok": 0, "efforts": null,
        "background_model": "glm-4.7"}},
       {{"name": "glm-4.7"}}]'::jsonb,
     '[{{"key": "API_TIMEOUT_MS", "value": "3000000"}}]'::jsonb, 4);
INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES ('prov-glm000000001', '{API_KEY}');
INSERT INTO agents (id, name, runtime_id, provider_id, model, effort) VALUES
    ('agent-sonnet0000001', 'claude-code/sonnet', 'rt-claude-code', 'prov-claude-subscription', 'sonnet', NULL),
    ('agent-glm000000001', 'claude-code/glm/glm-5.3[1m]', 'rt-claude-code', 'prov-glm000000001', 'glm-5.3[1m]',
     'max'),
    ('agent-beta00000001', 'beta/haiku', 'rt-custom-beta', 'prov-claude-subscription', 'haiku', NULL);

-- The layers name their agent by agent_id.
INSERT INTO agent_setting_layers (scope, role, project_id, harness_prompt, agent_id, effort, env, run_budget_usd)
VALUES
    ('system', NULL, NULL, 'System harness H0.', 'agent-glm000000001', 'high',
     '[{{"key": "PARITY_ZONE", "value": "UTC"}}, {{"key": "PARITY_MODE", "value": "two words"}}]'::jsonb, NULL),
    ('role', 'planner', NULL, NULL, 'agent-sonnet0000001', NULL, '[]'::jsonb, NULL),
    ('role', 'worker', NULL, 'Worker harness HW.', NULL, NULL, '[]'::jsonb, 2),
    ('role', 'reviewer', NULL, NULL, 'agent-beta00000001', NULL, '[]'::jsonb, NULL),
    ('project', NULL, '{PROJECT_A}', NULL, NULL, NULL, '[{{"key": "ZED", "value": "1"}}]'::jsonb, 0.5),
    ('role_project', 'worker', '{PROJECT_A}', NULL, 'agent-sonnet0000001', 'low', '[]'::jsonb, NULL),
    ('role_project', 'reviewer', '{PROJECT_B}', NULL, NULL, NULL, '[{{"key": "PARITY_ZONE", "value": null}}]'::jsonb,
     NULL);
-- Task A chose its Workers' and its Reviewers' agent.
INSERT INTO task_agent_overrides (task_id, role, agent_id) VALUES
    ('{TASK_A}', 'worker', 'agent-glm000000001'),
    ('{TASK_A}', 'reviewer', 'agent-sonnet0000001');

-- Saved sessions name the agent that ran them by session_agent_id. The Manager's ran on the built-in agent with
-- Claude Code's own login; the system layer moved it to an API key provider since, so it starts a new session.
INSERT INTO agent_sessions (agent_id, session_id, session_totals, runtime_type, session_agent_id, provider_id) VALUES
    ('manager', 'sess-manager-1', '{{"cost_usd": 0.25}}'::jsonb, 'claude-code', 'agent-claude-code-opus',
     'prov-claude-subscription'),
    ('planner-{TASK_A}', 'sess-planner-a', '{{}}'::jsonb, 'claude-code', 'agent-sonnet0000001',
     'prov-claude-subscription');
"""


async def seed_v2_6_0(pool: Any) -> None:
    """The v2.6.0 schema and ``V2_6_0_ROWS``, in the pool's schema."""
    await pool.execute(SCHEMA_V2_6_0.read_text(encoding="utf-8"))
    await pool.execute(V2_6_0_ROWS)


async def capture(root: Path) -> dict[str, dict[str, Any]]:
    """What every case of ``CASES`` started Claude Code with; the caller set up and migrated the database, and set
    ``HOME`` (under ``root``)."""
    parity.SCENARIOS["profile_rename"] = {"layers": [], "extras": False, "cases": CASES}
    for project_id in (PROJECT_A, PROJECT_B):
        parity._work_dir(root, project_id).mkdir(parents=True, exist_ok=True)
    return await parity.capture(root, "profile_rename")


async def _generate(dsn: str) -> dict[str, Any]:
    import uuid

    import asyncpg

    from looper.db import connection

    schema = f"looper_parity_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, server_settings={"search_path": schema})
    try:
        await seed_v2_6_0(pool)
        await connection.ensure_schema(pool)
        connection._pool = pool
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            os.environ["HOME"] = str(root / "home")
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            return await capture(root)
    finally:
        connection._pool = None
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_generate(sys.argv[1])), indent=1, ensure_ascii=False, sort_keys=True))
