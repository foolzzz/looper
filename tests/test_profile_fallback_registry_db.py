"""Profile fallback graphs are validated at the operator API boundary."""

import pytest

from looper.agents import profile_registry
from looper.db import profile_registry as db
from tests.db_support import DSN, run_with_models
from tests.test_api_v1_profiles_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _create(call, **fields):
    model = (await profile_registry.create_model({"provider_id": "prov-test", "name": fields["name"].lower().replace(" ", "-"),
                                                 "protocol": "anthropic-messages"}))["model"]
    status, result = await call("profiles.create", {"runtime_id": "rt-claude-code", "model_id": model["model_id"], **fields})
    assert status == 200, result
    return result["result"]["profile"]


def test_profile_fallback_round_trip_and_clear(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            target = await _create(call, name="Target")
            source = await _create(call, name="Source", fallback_profile_id=target["profile_id"])
            assert source["fallback_profile_id"] == target["profile_id"]
            loaded = await db.get_profile(source["profile_id"])
            assert profile_registry.ResolvedAgentProfile.from_row(loaded).fallback_profile_id == target["profile_id"]
            status, result = await call("profiles.update", {"profile_id": source["profile_id"],
                "fields": {"fallback_profile_id": None}})
            assert status == 200 and result["result"]["profile"]["fallback_profile_id"] is None
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("failure", ["cycle", "too_long", "missing"])
def test_profile_fallback_graph_rejects_invalid_chain(tmp_path, monkeypatch, failure):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            nodes = [await _create(call, name=f"Profile {index}") for index in range(5)]
            for source, target in zip(nodes[:3], nodes[1:4]):
                status, result = await call("profiles.update", {"profile_id": source["profile_id"],
                    "fields": {"fallback_profile_id": target["profile_id"]}})
                assert status == 200, result
            target = nodes[0]["profile_id"] if failure == "cycle" else (
                nodes[4]["profile_id"] if failure == "too_long" else "missing-profile")
            status, result = await call("profiles.update", {"profile_id": nodes[3]["profile_id"],
                "fields": {"fallback_profile_id": target}})
            assert status == 400, result
            assert (await db.get_profile(nodes[3]["profile_id"]))["fallback_profile_id"] is None
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("incoming", [False, True])
def test_profile_fallback_preserves_all_source_roles(tmp_path, monkeypatch, incoming):
    async def run(pool):
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities, builtin) "
                           "VALUES ('rt-limited', 'Limited', 'claude-code', ARRAY['run'], false)")
        async with _api(tmp_path, pool) as call:
            target = await _create(call, name="Target", **({} if incoming else {"runtime_id": "rt-limited"}))
            source = await _create(call, name="Source", **({"fallback_profile_id": target["profile_id"]}
                                                          if incoming else {}))
            profile = target if incoming else source
            fields = {"runtime_id": "rt-limited"} if incoming else {"fallback_profile_id": target["profile_id"]}
            status, result = await call("profiles.update", {"profile_id": profile["profile_id"], "fields": fields})
            assert status == 400, result
    run_with_models(run, monkeypatch)


def test_profile_fallback_target_cannot_be_deleted(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            target = await _create(call, name="Target")
            await _create(call, name="Source", fallback_profile_id=target["profile_id"])
            with pytest.raises(profile_registry.RegistryConflict):
                await profile_registry.delete_profile(target["profile_id"])
            assert await db.get_profile(target["profile_id"])
    run_with_models(run, monkeypatch)


def test_profile_delete_racing_with_fallback_reference_explains_conflict(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            target = await _create(call, name="Target")
            source = await _create(call, name="Source")
            preflight = profile_registry.deletable_profile

            async def checked(profile_id):
                row = await preflight(profile_id)
                await pool.execute("UPDATE agent_profiles SET fallback_profile_id = $1 WHERE id = $2",
                                   target["profile_id"], source["profile_id"])
                return row

            monkeypatch.setattr(profile_registry, "deletable_profile", checked)
            with pytest.raises(profile_registry.RegistryConflict, match="fallback target"):
                await profile_registry.delete_profile(target["profile_id"])
            assert await db.get_profile(target["profile_id"])
    run_with_models(run, monkeypatch)


def test_existing_database_adds_profile_fallback_and_journal_kind(monkeypatch):
    from looper.db import connection
    from tests.db_support import run_db

    async def before(pool):
        await connection.ensure_schema(pool)
        await pool.execute("ALTER TABLE agent_profiles DROP COLUMN fallback_profile_id")
        await pool.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
        await pool.execute("ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check "
                           "CHECK (kind IN ('profile_choice'))")

    async def run(pool):
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                  "WHERE table_schema = current_schema() AND table_name = 'agent_profiles' "
                                  "AND column_name = 'fallback_profile_id')")
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project', 'Example')")
        await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ('task', 'Example', 'project')")
        await pool.execute("INSERT INTO task_journal (task_id, step_id, kind) "
                           "VALUES ('task', '', 'profile_fallback')")
    run_db(run, monkeypatch, before=before, timeout=30)
