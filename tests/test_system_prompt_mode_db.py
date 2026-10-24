"""The system prompt mode as a role setting: the settings API, the migration from profiles, configuration transfer
and what a real launch records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_profile_registry_db import _call, _model_id, _ok, _runtime

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

BUILTIN = {"manager": "replace", "planner": "replace", "worker": "append", "reviewer": "append"}


async def _profile(name: str, **fields: Any) -> dict[str, Any]:
    return (await _ok("POST", "/api/agent-profiles", {
        "name": name, "runtime_id": "rt-claude-code",
        "model_id": await _model_id("prov-claude-subscription", "sonnet"), **fields}))["profile"]


async def _modes() -> dict[str, str]:
    body = await _ok("GET", "/api/agent-settings")
    return {role: view["effective"]["system_prompt_mode"] for role, view in body["roles"].items()}


def test_role_modes_are_the_builtin_ones_until_set_and_unset_by_null(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(pool: Any) -> None:
        assert await _modes() == BUILTIN
        body = await _ok("PUT", "/api/agent-settings/roles/planner", {"system_prompt_mode": "append"})
        assert body["roles"]["planner"]["values"]["system_prompt_mode"] == "append"
        body = await _ok("PUT", "/api/agent-settings/roles/worker", {"system_prompt_mode": "replace"})
        assert await _modes() == {**BUILTIN, "planner": "append", "worker": "replace"}
        assert "system_prompt_mode" not in body["roles"]["reviewer"]["values"]
        await _ok("PUT", "/api/agent-settings/roles/planner", {"system_prompt_mode": None})
        await _ok("PUT", "/api/agent-settings/roles/worker", {"system_prompt_mode": None})
        assert await _modes() == BUILTIN
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers") == 0

    run_db(run, monkeypatch)


@pytest.mark.parametrize("value", ["auto", "default", "", 1, True, "Replace"])
def test_role_mode_refuses_other_values(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    async def run(_pool: Any) -> None:
        status, body = await _call("PUT", "/api/agent-settings/roles/worker", {"system_prompt_mode": value})
        if value == "":  # an empty value unsets, like every setting
            assert status == 200
            return
        message = 'must be "append" or "replace"' if isinstance(value, str) else "declared schema"
        assert status == 400 and message in body["error"]

    run_db(run, monkeypatch)


def test_mode_is_set_only_on_a_role_layer_and_a_profile_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(_pool: Any) -> None:
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
        for method, path in (("PUT", "/api/agent-settings/system"), ("PUT", f"/api/projects/{project}/agent-settings"),
                             ("PUT", f"/api/projects/{project}/agent-settings/roles/worker")):
            status, body = await _call(method, path, {"system_prompt_mode": "replace"})
            assert status == 400 and "set per role" in body["error"], path
        status, body = await _call("POST", "/api/agent-profiles", {
            "name": "p", "runtime_id": "rt-claude-code",
            "model_id": await _model_id("prov-claude-subscription", "sonnet"), "system_prompt_mode": "replace"})
        assert status == 400 and "unknown field 'system_prompt_mode'" in body["error"]
        profile = await _profile("q")
        assert "system_prompt_mode" not in profile

    run_db(run, monkeypatch)


async def _old_shape(pool: Any) -> None:
    """The schema before the role setting applied to every runtime: the mode on the profiles, not on the layers."""
    await pool.execute("ALTER TABLE agent_setting_layers DROP COLUMN system_prompt_mode")
    await pool.execute("ALTER TABLE agent_profiles ADD COLUMN system_prompt_mode TEXT NOT NULL DEFAULT 'append' "
                       "CHECK (system_prompt_mode IN ('append', 'replace'))")


async def _profile_row(pool: Any, profile_id: str, mode: str, runtime: str = "claude-code") -> None:
    provider = {"claude-code": "prov-claude-subscription", "codex": "prov-codex-login"}[runtime]
    model = await _model_id(provider, "sonnet" if runtime == "claude-code" else "gpt-6.1-sol",
                            "anthropic-messages" if runtime == "claude-code" else "openai-responses")
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id, system_prompt_mode) "
                       "VALUES ($1, $1, $2, $3, $4)", profile_id, f"rt-{runtime}", model, mode)


async def _layer_modes(pool: Any) -> dict[str, str | None]:
    rows = await pool.fetch("SELECT role, system_prompt_mode FROM agent_setting_layers WHERE scope = 'role'")
    return {row["role"]: row["system_prompt_mode"] for row in rows}


async def _profile_column(pool: Any) -> bool:
    return bool(await pool.fetchval(
        "SELECT count(*) FROM pg_attribute WHERE attrelid = to_regclass('agent_profiles') "
        "AND attname = 'system_prompt_mode' AND NOT attisdropped"))


async def _profile_checks(pool: Any) -> int:
    return await pool.fetchval("SELECT count(*) FROM pg_constraint WHERE conrelid = to_regclass('agent_profiles') "
                               "AND pg_get_constraintdef(oid) LIKE '%system_prompt_mode%'")


def test_migration_carries_a_replace_profile_of_any_runtime_to_the_roles_that_used_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db.connection import ensure_schema

    async def run(pool: Any) -> None:
        await _runtime("codex")
        await _old_shape(pool)
        await _profile_row(pool, "rep", "replace", "codex")
        await _profile_row(pool, "app", "append", "codex")
        # The worker role selects the replacing codex profile; the reviewer inherits the shared defaults' appending
        # one; the Planner's replacing profile carries nothing (its built-in value is replace).
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', 'worker', 'rep')")
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', 'planner', 'rep')")
        await pool.execute("INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', 'app')")
        await ensure_schema(pool)
        assert await _layer_modes(pool) == {"worker": "replace", "planner": None}
        assert not await _profile_column(pool) and await _profile_checks(pool) == 0
        assert await _modes() == {**BUILTIN, "worker": "replace"}
        # Once: a later explicit append on the worker stays through the next startup, and nothing else changes.
        await _ok("PUT", "/api/agent-settings/roles/worker", {"system_prompt_mode": "append"})
        await ensure_schema(pool)
        assert await _layer_modes(pool) == {"worker": "append", "planner": None}
        assert not await _profile_column(pool)

    run_db(run, monkeypatch)


def test_migration_keeps_a_mode_the_layer_already_has(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def run(pool: Any) -> None:
        await _old_shape(pool)
        await _profile_row(pool, "rep", "replace")
        await pool.execute("ALTER TABLE agent_setting_layers ADD COLUMN system_prompt_mode TEXT")
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id, system_prompt_mode) "
                           "VALUES ('role', 'worker', 'rep', 'append')")
        await ensure_schema(pool)
        assert (await _layer_modes(pool))["worker"] == "append"

    run_db(run, monkeypatch)


def test_migration_follows_the_shared_defaults_and_the_builtin_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def run(pool: Any) -> None:
        await _old_shape(pool)
        await _profile_row(pool, "rep", "replace")
        await pool.execute("INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', 'rep')")
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', 'reviewer', "
                           "'agent-claude-code-opus')")
        await ensure_schema(pool)
        # The worker follows the shared defaults' replacing profile, the reviewer its own appending one.
        assert await _layer_modes(pool) == {"worker": "replace", "reviewer": None}

    run_db(run, monkeypatch)


def test_migration_on_a_database_without_the_old_column_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def run(pool: Any) -> None:
        assert not await _profile_column(pool)
        await ensure_schema(pool)
        await ensure_schema(pool)
        assert await _layer_modes(pool) == {} and await _modes() == BUILTIN and not await _profile_column(pool)

    run_db(run, monkeypatch)


async def _profile_columns(conn: Any) -> list[str]:
    rows = await conn.fetch("SELECT attname FROM pg_attribute WHERE attrelid = to_regclass('agent_profiles') "
                            "AND attnum > 0 AND NOT attisdropped")
    return [row["attname"] for row in rows]


def test_config_transfer_carries_role_modes_and_ignores_a_profiles_old_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.config_transfer import Action, export_chunks, import_config

    async def run(pool: Any) -> None:
        await _ok("PUT", "/api/agent-settings/roles/worker", {"system_prompt_mode": "replace"})
        await _ok("PUT", "/api/agent-settings/roles/planner", {"system_prompt_mode": "append"})
        async with pool.acquire() as conn:
            document = json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            layers = {e["role"]: e.get("system_prompt_mode") for e in document["settings"]["layers"]
                      if e["scope"] == "role"}
            assert layers == {"worker": "replace", "planner": "append"}
            await conn.execute("DELETE FROM agent_setting_layers")
            async with conn.transaction():
                plan = await import_config(conn, document, apply=True)
            assert not any(c.action is Action.SKIPPED for c in plan.changes if c.section == "settings.layers")
            assert await _layer_modes(pool) == {"worker": "replace", "planner": "append"}
            entry = {"name": "old", "runtime": "claude-code", "provider": "claude-subscription", "model": "sonnet",
                     "effort": None, "agent_teams": True, "system_prompt_mode": "replace", "custom_prompt": ""}
            async with conn.transaction():
                plan = await import_config(conn, {"format": "looper-config/v2", "profiles": [entry]}, apply=True)
            change = next(c for c in plan.changes if c.key == "old")
            assert change.action is Action.ADDED and "system_prompt_mode" not in await _profile_columns(conn)
            assert any("old: its system_prompt_mode is ignored" in note for note in plan.notes)
            exported = next(e for e in json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False)]))["profiles"] if e["name"] == "old")
            assert "system_prompt_mode" not in exported
            # A system layer cannot carry one.
            bad = {"settings": {"layers": [{"scope": "system", "system_prompt_mode": "replace"}]}}
            async with conn.transaction():
                plan = await import_config(conn, bad, apply=True)
            assert any(c.action is Action.SKIPPED for c in plan.changes)

    run_db(run, monkeypatch)


@pytest.mark.parametrize("role", ["manager", "worker"])
@pytest.mark.parametrize("mode", [None, "append", "replace"])
def test_real_launch_applies_and_records_the_role_mode_in_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str, mode: str | None,
) -> None:
    import agent_launch_parity as parity
    from fake_claude import fake_claude
    from tests.test_profile_registry_db import _launch

    async def run(_pool: Any) -> None:
        if mode is not None:
            await _ok("PUT", f"/api/agent-settings/roles/{role}", {"system_prompt_mode": mode})
        used = mode or BUILTIN[role]
        if role == "worker":
            project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
            task = (await _ok("POST", "/api/v1/tasks/create", {
                "requirement": "do it", "project_id": project, "work_dir": str(tmp_path / "work")}))["task_id"]
            seen = await _launch("worker", task, tmp_path / "work", tmp_path)
        else:
            out = tmp_path / "manager-capture.json"
            await parity._manager(fake_claude(tmp_path / "claude-manager", parity._CLAUDE.format(out=str(out))), tmp_path)
            seen = json.loads(out.read_text())
        flag = "--system-prompt-file" if used == "replace" else "--append-system-prompt-file"
        assert flag in seen["argv"]
        records = (await _ok("POST", "/api/v1/runs/list", {}))["records"]
        record = next(row for row in records if row["agent_id"] == ("manager" if role == "manager" else "worker-x"))
        assert record["status"] == "completed" and record["metadata"]["system_prompt_mode"] == used
        detail = await _ok("POST", "/api/v1/runs/get", {"record_id": record["id"]})
        assert detail["record"]["metadata"]["system_prompt_mode"] == used

    run_db(run, monkeypatch)
