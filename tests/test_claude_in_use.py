"""The Claude Code CLI and login are required only when an Agent Profile in use runs on Claude Code."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper import ops_config
from looper.agents import self_check
from looper.agents.agent_settings import AgentType
from looper.agents.profile_registry import BUILTIN_PROFILE
from looper.db import profile_registry
from tests.db_support import DSN, run_db


@pytest.mark.parametrize(("in_use", "statuses"), [
    (False, ["skip", "skip"]),
    (True, ["fail", "skip"]),
    (None, ["fail", "skip"]),
], ids=["no-claude-profile", "claude-profile", "unknown"])
def test_claude_cli_is_checked_unless_no_profile_in_use_runs_on_it(monkeypatch, in_use, statuses):
    monkeypatch.setenv("PATH", "/nonexistent")
    checks = self_check.check_claude("claude", in_use)
    assert [check.status for check in checks] == statuses
    assert (self_check.NO_CLAUDE_PROFILE in checks[0].detail) is (in_use is False)


def test_doctor_passes_without_claude_when_no_profile_uses_it(tmp_path, monkeypatch, capsys):
    from looper.agents import service

    ok = self_check.Check("unrelated", "Unrelated", self_check.OK, "ready")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(self_check, "check_database", AsyncMock(return_value=ok))
    monkeypatch.setattr(self_check, "provider_limit_checks_for_database", AsyncMock(return_value=[]))
    for name in ("check_codex_login", "check_grok_login", "check_cursor_login", "check_coco_login",
                 "check_git", "check_lark_cli", "check_projects_root"):
        monkeypatch.setattr(self_check, name, lambda *args: ok)
    monkeypatch.setattr(self_check, "check_pg_dump", lambda *args: (ok, None))
    for name in ("check_ttadk", "check_step_worktrees", "llmrec_check", "gateway_check"):
        monkeypatch.setattr(self_check, name, AsyncMock(return_value=ok))
    monkeypatch.setattr(service, "doctor_check", lambda: ok)
    ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE)
    codes = {}
    for in_use in (False, True):
        monkeypatch.setattr(self_check, "_database_facts", AsyncMock(
            return_value=self_check._DatabaseFacts(claude_in_use=in_use)))
        codes[in_use] = self_check.doctor(ops)
    assert codes == {False: 0, True: 1}
    output = capsys.readouterr().out
    assert output.count("skip  Claude Code CLI") == 1 and output.count("fail  Claude Code CLI") == 1


def test_a_database_that_cannot_be_read_leaves_claude_required(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(profile_registry, "runtime_types_in_use", broken)
    assert asyncio.run(self_check.claude_runtime_in_use()) is None


pytestmark_db = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
ROLES = [role.value for role in AgentType]


async def _types(pool) -> set[str]:
    return await profile_registry.runtime_types_in_use(BUILTIN_PROFILE.id, ROLES, pool=pool)


async def _codex_profile(pool, name: str, fallback: str | None = None) -> str:
    await pool.execute(
        "INSERT INTO agent_runtimes (id, name, type, executable, capabilities, builtin) "
        "VALUES ('rt-codex', 'codex', 'codex', '', ARRAY['run'], true) ON CONFLICT DO NOTHING")
    await pool.execute(
        "INSERT INTO agent_profiles (id, name, runtime_id, model_id, fallback_profile_id) "
        "SELECT $1, $1, 'rt-codex', id, $2 FROM llm_models WHERE provider_id = 'prov-codex-login' LIMIT 1",
        name, fallback)
    return name


@pytestmark_db
def test_runtime_types_in_use_follow_every_layer_and_fallback(monkeypatch):
    async def test(pool):
        # Nothing chosen: every role runs the built-in Claude Code profile.
        assert await _types(pool) == {"claude-code"}
        codex = await _codex_profile(pool, "agent-codex")
        for role in (r for r in ROLES if r != "reviewer"):
            await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', $1, $2)",
                               role, codex)
        assert await _types(pool) == {"codex", "claude-code"}  # the reviewer role still runs the built-in profile
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', 'reviewer', $1)",
                           codex)
        assert await _types(pool) == {"codex"}
        # A Claude Code profile at a project layer, or as a fallback, counts.
        claude = BUILTIN_PROFILE.id
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p1', 'p1')")
        await pool.execute("INSERT INTO agent_setting_layers (scope, project_id, profile_id) VALUES ('project', 'p1', $1)",
                           claude)
        assert await _types(pool) == {"codex", "claude-code"}
        await pool.execute("DELETE FROM agent_setting_layers WHERE scope = 'project'")
        assert await _types(pool) == {"codex"}
        fallback = await _codex_profile(pool, "agent-codex-fallback", claude)
        await pool.execute("UPDATE agent_setting_layers SET profile_id = $1 WHERE role = 'worker'", fallback)
        assert await _types(pool) == {"codex", "claude-code"}
        # The system layer's profile covers every role.
        await pool.execute("DELETE FROM agent_setting_layers")
        await pool.execute("INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', $1)", codex)
        assert await _types(pool) == {"codex"}

    run_db(test, monkeypatch, timeout=30)


@pytestmark_db
def test_runtime_types_in_use_include_workflow_presets_and_project_allow_lists(monkeypatch):
    async def test(pool):
        codex = await _codex_profile(pool, "agent-codex")
        await pool.execute("INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', $1)", codex)
        assert await _types(pool) == {"codex"}
        claude = BUILTIN_PROFILE.id
        # A workflow preset's per-role profile, and a custom role's profile.
        await pool.execute("INSERT INTO workflows (key, name, definition) VALUES ('w1', 'w1', $1::jsonb)",
                           json.dumps({"profiles": {"worker": claude}}))
        assert await _types(pool) == {"codex", "claude-code"}
        await pool.execute("UPDATE workflows SET definition = $1::jsonb",
                           json.dumps({"roles": [{"key": "r", "kind": "worker", "profile_id": claude}]}))
        assert await _types(pool) == {"codex", "claude-code"}
        await pool.execute("DELETE FROM workflows")
        assert await _types(pool) == {"codex"}
        # A project that lets its Planners choose among profiles names them; an empty list names none.
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p1', 'p1'), ('p2', 'p2')")
        assert await _types(pool) == {"codex"}
        await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ('p1', $1)", claude)
        assert await _types(pool) == {"codex", "claude-code"}

    run_db(test, monkeypatch, timeout=30)


@pytestmark_db
def test_runtimes_in_use_carry_the_executable_of_the_runtime_a_profile_launches(monkeypatch):
    async def test(pool):
        codex = await _codex_profile(pool, "agent-codex")
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, executable, capabilities) "
                           "VALUES ('rt-pi-idle', 'pi-idle', 'pi', '/gone/pi', ARRAY['run']), "
                           "('rt-pi-used', 'pi-used', 'pi', '/opt/pi/bin/pi', ARRAY['run'])")
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "SELECT 'agent-pi', 'agent-pi', 'rt-pi-used', id FROM llm_models LIMIT 1")
        await pool.execute("INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', $1)", codex)
        used = await profile_registry.runtimes_in_use(BUILTIN_PROFILE.id, ROLES, pool=pool)
        assert [r["executable"] for r in used if r["type"] == "pi"] == []
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', 'worker', 'agent-pi')")
        used = await profile_registry.runtimes_in_use(BUILTIN_PROFILE.id, ROLES, pool=pool)
        assert [r["executable"] for r in used if r["type"] == "pi"] == ["/opt/pi/bin/pi"]

    run_db(test, monkeypatch, timeout=30)
