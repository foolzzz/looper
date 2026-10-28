"""Authentic, immutable v2.13.165 initialization and shared database witnesses."""
import asyncio
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from tests.db_support import DSN

COMMIT = "f2935092d44c4707d92d4b60c32a334a934d4db9"
FINGERPRINT = "b2bca629d4d68b756f42bc6d4567b9883985343c120f2543a0f9cda9da6732a3"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def bridge_source(tmp_path_factory):
    external = os.environ.get("LOOPER_TEST_BRIDGE_SOURCE")
    source = Path(external).resolve() if external else tmp_path_factory.mktemp("bridge-source")
    if not external:
        result = subprocess.run(["git", "archive", COMMIT, "looper"], cwd=ROOT, capture_output=True, timeout=30)
        assert result.returncode == 0, "Required v2.13.165 source missing; fetch the pinned tag first"
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(source, filter="data")
    assert hashlib.sha256((source / "looper/db/schema.sql").read_bytes()).hexdigest() == FINGERPRINT
    return source


_DRIVER = r'''
import asyncio, hashlib, sys
from pathlib import Path
source, url, schema, refuse = sys.argv[1:]
sys.path.insert(0, source)
import looper
from looper.db import connection
assert Path(looper.__file__).resolve() == Path(source).resolve() / 'looper/__init__.py'
assert hashlib.sha256(connection.SCHEMA_PATH.read_bytes()).hexdigest() == 'b2bca629d4d68b756f42bc6d4567b9883985343c120f2543a0f9cda9da6732a3'
async def main():
    import asyncpg
    conn = await asyncpg.connect(url, server_settings={'search_path': schema}, command_timeout=20)
    try:
        try:
            await connection.ensure_schema(conn)
        except connection.RegistrySchemaVersionError:
            if refuse != 'True': raise
        else:
            assert refuse != 'True', 'bridge binary accepted schema 4'
    finally:
        await conn.close()
asyncio.run(asyncio.wait_for(main(), 40))
'''


async def run_bridge(source, url, schema="public", *, refuse=False):
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "SYSTEMROOT")}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", _DRIVER, str(source), url, schema, str(refuse),
        cwd=source, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
        assert process.returncode == 0, (stdout + stderr).decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def initialize_bridge(conn, source, *, url=None, seed=True):
    await run_bridge(source, url or DSN, await conn.fetchval("SELECT current_schema()"))
    if seed:
        await conn.execute(SEED)


SEED = r'''
INSERT INTO projects (id, name, task_workspaces) VALUES
 ('proj-bridge', 'Bridge example', NULL), ('proj-explicit', 'Explicit worktree', 'worktree');
INSERT INTO tasks (id, requirement, project_id, status, config) VALUES
 ('task-bridge', 'Preserve this task', 'proj-bridge', 'completed', '"{\"worker\":{\"max_turns\":9}}"');
INSERT INTO task_plans (task_id, plan_id, status, final_summary) VALUES
 ('task-bridge', 'plan-bridge', 'completed', 'Keep the plan');
INSERT INTO system_settings (key, value) VALUES ('bridge-choice', 'preserved');
INSERT INTO agent_setting_layers (scope, harness_prompt) VALUES ('system', 'Synthetic system harness.');
INSERT INTO agent_sessions (agent_id, session_id, session_totals, selected_profile_id, pending_handoff) VALUES
 ('manager-bridge', 'bridge-session', '{"input_tokens":17,"cost_usd":0.25}', NULL, 'Keep the handoff'),
 ('manager-empty', '', '{}', 'agent-claude-code-opus', NULL);
INSERT INTO agent_sessions (agent_id, session_id, runtime_type, provider_id) VALUES
 ('manager-codex', 'native-codex', 'codex', NULL),
 ('manager-provider', 'known-provider', NULL, 'prov-codex-login');
INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count) VALUES
 ('item-bridge', 'skill', 'bridge-skill', 'a2d7b5303e492932b064847ce5fd6fdd06e348568e0c3d9df62b7c12c2cea457', 12, 1);
INSERT INTO library_files (item_id, path, content, sha256) VALUES
 ('item-bridge', 'SKILL.md', 'Bridge bytes', '79336d771a2c6ef9f881dd15939173ec0151c8bf93c94225ce6bd2098cccae6e');
INSERT INTO library_installs (id, item_id, scope, roles) VALUES
 ('install-bridge', 'item-bridge', 'global', '{worker}');
INSERT INTO llm_records (agent_id, session_id, input_tokens, output_tokens, cost_usd, metadata) VALUES
 ('bridge-worker', 'bridge-history', 17, 3, 0.25, '{"agent":{"name":"Original evidence"}}');
INSERT INTO llm_model_migration_map (legacy_provider_id, legacy_model, protocol, provider_id, model_id)
 SELECT provider_id, name, protocol, provider_id, id FROM llm_models
 WHERE provider_id='prov-claude-subscription' AND name='opus';
INSERT INTO channel_configs (channel_id, channel_type, display_name, config) VALUES
 ('channel-telegram', 'telegram', 'Builtin example', '{"allowed_user_ids":"7, 8"}'),
 ('channel-telegram-201', 'telegram', 'Custom example', '{"allowed_user_ids":[7," 8 ",7]}');
INSERT INTO task_steps (id, task_id, step_index, instruction) VALUES ('step-bridge', 'task-bridge', 0, 'Ask');
INSERT INTO task_user_gates (step_id, task_id, topic) VALUES ('step-bridge', 'task-bridge', 'Preserve history');
INSERT INTO task_user_gate_messages (step_id, task_id, role, kind, text, post_state, channel, channel_id) VALUES
 ('step-bridge', 'task-bridge', 'planner', 'reply', 'Already handled', 'none', NULL, NULL);
INSERT INTO task_user_gate_posts (message_id, task_id, channel, channel_id, chat, external_id)
 SELECT id, task_id, NULL, 'channel-telegram-201', '7', '11' FROM task_user_gate_messages;
INSERT INTO chat_messages (session_id, message_id, role, content, channel) VALUES
 ('telegram-7', 'bridge-message', 'user', 'Synthetic public example', 'telegram');
INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, planner_session_id) VALUES
 ('schedule-bridge', 'proj-bridge', 'Bridge schedule', '0 0 * * *', 'UTC', 'Preserve', 'planner-bridge');
INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status, task_id, summary) VALUES
 ('run-bridge', 'schedule-bridge', '2026-10-20T00:00:00Z', 'scheduled', 'completed', 'task-bridge', 'Keep the run');
'''

TABLES = ("projects", "tasks", "task_plans", "system_settings", "agent_setting_layers", "library_items",
          "library_files", "library_installs", "agent_sessions", "llm_records", "channel_configs",
          "chat_messages", "schedules", "schedule_runs", "task_user_gate_messages", "task_user_gate_posts",
          "llm_model_migration_map", "schema_migrations", "registry_schema_version", "schema_fingerprint")


async def witness(conn):
    return {table: [dict(row) for row in await conn.fetch(f'SELECT * FROM {table} ORDER BY 1')]
            for table in TABLES if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)}


from looper.db.bridge_upgrade import structure  # noqa: E402,F401
