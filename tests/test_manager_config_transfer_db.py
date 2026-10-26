"""Portable Manager graphs on disposable PostgreSQL schemas, without native runtime launches."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents import config_transfer as transfer
from looper.db import managers
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _manager(identity: str = "manager-review", **values: Any) -> dict[str, Any]:
    return {"id": identity, "name": identity.removeprefix("manager-").title(),
            "system_prompt": "Review public example changes.", "enabled": False, **values}


def _template(**values: Any) -> dict[str, Any]:
    return {"key": "review", "name": "Review", "requirement": "Review {{url}}.",
            "inputs": [{"key": "url", "label": "URL", "kind": "url", "required": True}], **values}


def _document(*entries: dict[str, Any], **sections: Any) -> dict[str, Any]:
    return {"format": transfer.V4_FORMAT, "managers": list(entries), **sections}


async def _run(pool: Any, document: dict[str, Any], *, apply: bool = True) -> Any:
    async with pool.acquire() as conn:
        return await transfer.run_import(conn, document, apply=apply)


async def _export(pool: Any, *, include_projects: bool = True) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        return json.loads("".join([chunk async for chunk in transfer.export_chunks(
            conn, include_projects=include_projects, include_secrets=False)]))


async def _snapshot(pool: Any) -> dict[str, Any]:
    async with pool.acquire() as conn:
        tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        rows = {row["tablename"]: [item["value"] for item in await conn.fetch(
            f'SELECT to_jsonb(t)::text AS value FROM "{row["tablename"]}" t ORDER BY value')]
            for row in tables}
        sequences = await conn.fetch("SELECT sequencename FROM pg_sequences WHERE schemaname = current_schema()")
        rows["sequences"] = {row["sequencename"]: tuple(await conn.fetchrow(
            f'SELECT last_value, is_called FROM "{row["sequencename"]}"')) for row in sequences}
        return rows


async def _codex_runtime(pool: Any) -> None:
    await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities, builtin) VALUES "
                       "('rt-config-codex', 'config-codex', 'codex', "
                       "ARRAY['run', 'resident_session', 'resume'], true)")


def _registry() -> dict[str, Any]:
    return {
        "providers": [{"name": "Example account", "vendor_type": "custom", "auth": "api_key",
                       "endpoints": {"openai-responses": {"base_url": "https://models.example.invalid/v1"}}}],
        "models": [{"provider": "Example account", "name": "example-model", "protocol": "openai-responses"}],
        "profiles": [{"name": "Example profile", "runtime": "config-codex", "agent_teams": False,
                      "model": {"provider": "Example account", "name": "example-model",
                                "protocol": "openai-responses"}}],
    }


def test_preview_is_read_only_including_revisions_sequences_and_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _codex_runtime(pool)
        before = await _snapshot(pool)
        document = _document(_manager(templates=[_template(project="Example", profiles={"reviewer": "Example profile"})]),
                             projects=[{"name": "Example"}], **_registry())
        plan = await _run(pool, document, apply=False)
        assert plan.view()["counts"]["managers"]["added"] == 1
        assert plan.view()["counts"]["manager.templates"]["added"] == 1
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=20)


def test_roundtrip_resolves_target_local_profile_and_project_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def test(pool: Any) -> None:
        await _codex_runtime(pool)
        document = _document(_manager(profile="Example profile", templates=[_template(
            project="Example", profiles={"planner": "Example profile", "reviewer": "Example profile"},
            role_templates=["security"])]), projects=[{"name": "Example", "role_templates": [
                {"key": "security", "kind": "review", "role_prompt": "Review security boundaries."}]}], **_registry())
        await _run(pool, document)
        exported = await _export(pool)
        source = await managers.get_template("manager-review", "review", pool=pool)
        async with fresh_schema() as target:
            await ensure_schema(target)
            await _codex_runtime(target)
            # The same natural references already have independent target-local ids.
            await _run(target, {"format": transfer.V3_FORMAT, **_registry(), "projects": [{"name": "Example"}]})
            target_profile = await target.fetchval("SELECT id FROM agent_profiles WHERE name = 'Example profile'")
            target_project = await target.fetchval("SELECT id FROM projects WHERE name = 'Example'")
            assert target_profile != source["planner_profile_id"] and target_project != source["project_id"]
            await _run(target, exported)
            stored = await managers.get_template("manager-review", "review", pool=target)
            assert stored["project_id"] == target_project
            assert stored["planner_profile_id"] == stored["reviewer_profile_id"] == target_profile
            assert (await managers.get_manager("manager-review", pool=target))["profile_id"] == target_profile
            assert (await _export(target))["managers"] == exported["managers"]

    run_db(test, monkeypatch, timeout=30)


def test_default_entry_changes_only_name_and_description(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        before = await managers.get_manager("manager", pool=pool)
        roles = await pool.fetch("SELECT * FROM role_configs ORDER BY role")
        await _run(pool, _document({"id": "manager", "name": "Coordinator", "description": "Example metadata"}))
        after = await managers.get_manager("manager", pool=pool)
        assert (after["name"], after["description"], after["revision"]) == ("Coordinator", "Example metadata", 1)
        assert {k: v for k, v in after.items() if k not in ("name", "description", "revision", "updated_at")} == {
            k: v for k, v in before.items() if k not in ("name", "description", "revision", "updated_at")}
        assert await pool.fetch("SELECT * FROM role_configs ORDER BY role") == roles
        assert await pool.fetchval("SELECT count(*) FROM agent_sessions") == 0

    run_db(test, monkeypatch, timeout=15)


def test_default_only_export_keeps_the_legacy_v3_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        first = await _export(pool, include_projects=False)
        assert first["format"] == transfer.V3_FORMAT and "managers" not in first
        assert set(first) == {"format", "looper_version", "exported_at", "secrets", "runtimes", "providers",
                              "models", "profiles", "settings", "channels", "installs", "catalog"}
        await _run(pool, _document({"id": "manager", "name": "Renamed default", "description": "Local"}))
        second = await _export(pool, include_projects=False)
        first.pop("exported_at")
        second.pop("exported_at")
        assert second == first

    run_db(test, monkeypatch, timeout=15)


def test_default_owned_added_bot_selects_v4_with_default_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        assert (await _export(pool))["format"] == transfer.V3_FORMAT
        await pool.execute("UPDATE managers SET name = 'Coordinator', description = 'Example coordinator' "
                           "WHERE id = 'manager'")
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, manager_id, display_name) "
                           "VALUES ('channel-telegram-2', 'telegram', 'manager', 'Example bot')")
        document = await _export(pool)
        assert document["format"] == transfer.V4_FORMAT
        assert document["managers"] == [{"id": "manager", "name": "Coordinator", "description": "Example coordinator"}]
        assert next(channel for channel in document["channels"] if channel["channel_id"] == "channel-telegram-2")["manager_id"] == "manager"

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("version", ["looper-config/v1", "looper-config/v2", "looper-config/v3"])
def test_legacy_formats_reject_manager_graph_fields(version: str, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"format": version, "managers": []}))
    with pytest.raises(transfer.ConfigFileError, match="requires looper-config/v4"):
        transfer.read_file(path)


@pytest.mark.parametrize("identity", ["unknown", "manager-deleted"])
def test_unknown_or_deleted_manager_identity_is_rejected(identity: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_manager("manager-deleted")))
        await pool.execute("UPDATE managers SET deleted_at = now() WHERE id = 'manager-deleted'")
        before = await _snapshot(pool)
        with pytest.raises(transfer.ConfigFileError, match="immutable|deleted"):
            await _run(pool, _document(_manager(identity)))
        assert await _snapshot(pool) == before
        assert all(entry["id"] != "manager-deleted" for entry in (await _export(pool)).get("managers", []))

    run_db(test, monkeypatch, timeout=20)


def test_manager_manager_roundtrips_separately_from_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document({"id": "manager", "name": "Coordinator"},
                                   _manager("manager-manager", name="Scenario", templates=[_template()])))
        entries = {entry["id"]: entry for entry in (await _export(pool))["managers"]}
        assert entries["manager"] == {"id": "manager", "name": "Coordinator", "description": ""}
        assert entries["manager-manager"]["name"] == "Scenario"
        assert entries["manager-manager"]["templates"][0]["key"] == "review"
        plan = await _run(pool, _document(*entries.values()))
        assert plan.writes == 0

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("inputs,reason", [
    ([{"key": "bad key", "label": "Bad", "kind": "text"}], "key"),
    ([{"key": "url", "label": "URL", "kind": "text", "pattern": "["}], "compile"),
])
def test_malformed_template_inputs_and_patterns_are_rejected(inputs: Any, reason: str,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        with pytest.raises(transfer.ConfigFileError, match=reason):
            await _run(pool, _document(_manager(templates=[_template(inputs=inputs)])), apply=False)
        assert await pool.fetchval("SELECT count(*) FROM managers") == 1

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("manager,projects,reason", [
    (_manager(profile="Missing"), [], "Agent Profile"),
    (_manager(templates=[_template(project="Missing")]), [], "no project"),
    (_manager(templates=[_template(project="Example", role_templates=["missing"])]), [{"name": "Example"}],
     "role template"),
])
def test_missing_graph_dependencies_fail_before_writes(manager: Any, projects: Any, reason: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        before = await _snapshot(pool)
        with pytest.raises(transfer.ConfigFileError, match=reason):
            await _run(pool, _document(manager, projects=projects))
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("entries,reason", [
    ([{"id": "manager", "name": "Default", "enabled": True}], "default Manager"),
    ([_manager(), _manager()], "duplicate id"),
    ([_manager(), _manager("manager-another", name="Review")], "duplicate name"),
    ([_manager(name="Straße"), _manager("manager-another", name="STRASSE")], "duplicate name"),
])
def test_invalid_graph_identity_and_default_overrides_are_rejected(entries: Any, reason: str,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        with pytest.raises(transfer.ConfigFileError, match=reason):
            await _run(pool, _document(*entries))
        assert await pool.fetchval("SELECT count(*) FROM managers") == 1

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("entry,reason", [
    (_manager(description=None), "description"),
    (_manager(templates=[_template(role_templates=None)]), "role_templates"),
    (_manager(templates=[_template(inputs=[{"key": "url", "label": "URL", "kind": "url", "extra": True}])]),
     "unknown field"),
])
def test_direct_import_rejects_invalid_closed_graph_fields(entry: Any, reason: str,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        before = await _snapshot(pool)
        with pytest.raises(transfer.ConfigFileError, match=reason):
            await _run(pool, _document(entry), apply=False)
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("profile", ["Missing", "Example profile"])
def test_imported_fixed_role_requires_a_compatible_registered_profile(profile: str,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _codex_runtime(pool)
        await _run(pool, {"format": transfer.V3_FORMAT, **_registry()})
        if profile == "Example profile":
            await pool.execute("UPDATE agent_runtimes SET capabilities = ARRAY['resident_session', 'resume'] "
                               "WHERE id = 'rt-config-codex'")
        before = await _snapshot(pool)
        document = _document(_manager(templates=[_template(project="Example", role_templates=["security"])]),
                             projects=[{"name": "Example", "role_templates": [{"key": "security", "kind": "review",
                                        "role_prompt": "Review security.", "profile": profile}]}])
        with pytest.raises(transfer.ConfigFileError, match="role template|profile"):
            await _run(pool, document)
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=20)


def test_existing_fixed_role_profile_must_be_on_the_project_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _codex_runtime(pool)
        await _run(pool, {"format": transfer.V3_FORMAT, **_registry(), "projects": [{"name": "Example",
            "role_templates": [{"key": "security", "kind": "review", "role_prompt": "Review security.",
                                "profile": "Example profile"}]}]})
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "SELECT 'agent-allowed', 'Allowed profile', runtime_id, model_id "
                           "FROM agent_profiles WHERE name = 'Example profile'")
        await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) "
                           "SELECT id, 'agent-allowed' FROM projects WHERE name = 'Example'")
        before = await _snapshot(pool)
        with pytest.raises(transfer.ConfigFileError, match="allow|list|profile"):
            await _run(pool, _document(_manager(templates=[_template(project="Example", role_templates=["security"])])))
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=20)


def test_concurrent_import_enables_respect_the_eight_manager_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(*[_manager(f"manager-seed-{i}", enabled=True) for i in range(6)]))
        results = await asyncio.gather(*[_run(pool, _document(_manager(identity, enabled=True)))
                                        for identity in ("manager-first", "manager-second")], return_exceptions=True)
        assert sum(isinstance(result, transfer.Plan) for result in results) == 1
        assert sum(isinstance(result, transfer.ConfigFileError) for result in results) == 1
        assert await managers.count_enabled(conn=pool) == 8

    run_db(test, monkeypatch, timeout=20)


def test_name_swap_is_rejected_consistently_by_preview_and_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_manager("manager-first", name="First"),
                                   _manager("manager-second", name="Second")))
        document = _document(_manager("manager-first", name="Second"), _manager("manager-second", name="First"))
        before = await _snapshot(pool)
        reasons = []
        for apply in (False, True):
            with pytest.raises(transfer.ConfigFileError, match="rename|used|occupied") as error:
                await _run(pool, document, apply=apply)
            reasons.append(str(error.value))
            assert await _snapshot(pool) == before
        assert reasons[0] == reasons[1]

    run_db(test, monkeypatch, timeout=20)


def test_apply_failure_rolls_back_the_entire_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        before = await _snapshot(pool)

        async def refuse_write(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("synthetic template write failure")

        monkeypatch.setattr(managers, "put_template", refuse_write)
        with pytest.raises(RuntimeError, match="synthetic template write failure"):
            await _run(pool, _document(_manager(templates=[_template()]), projects=[{"name": "Example"}]))
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=20)


def test_repeated_import_is_unchanged_and_notifies_only_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import managers_api

    async def test(pool: Any) -> None:
        observed: list[str] = []

        async def changed(identity: str) -> None:
            # Separate connection sees the committed template when the listener runs.
            assert await managers.get_template(identity, "review", pool=pool) is not None
            observed.append(identity)

        monkeypatch.setattr(managers_api, "_listener", changed)
        document = _document(_manager(templates=[_template()]))
        await _run(pool, document)
        assert observed == ["manager-review"]
        before = await _snapshot(pool)
        plan = await _run(pool, document)
        assert plan.writes == 0 and observed == ["manager-review"]
        assert await _snapshot(pool) == before

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("incoming", [{"format": "looper-config/v3"}, _document(),
                                       _document(_manager()), _document(_manager(templates=[]))])
def test_absent_and_empty_lists_preserve_existing_target_templates(incoming: Any,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_manager(templates=[_template()])))
        before = await managers.get_template("manager-review", "review", pool=pool)
        await _run(pool, copy.deepcopy(incoming))
        assert await managers.get_template("manager-review", "review", pool=pool) == before

    run_db(test, monkeypatch, timeout=15)


def test_repoint_is_explicit_and_keeps_the_existing_target_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        document = _document(_manager(templates=[_template(repoint=True)]))
        preview = await _run(pool, document, apply=False)
        assert any("requires repointing" in note and "no local directory" in note for note in preview.notes)
        await _run(pool, document)
        assert (await managers.get_template("manager-review", "review", pool=pool))["work_dir"] == ""
        await pool.execute("UPDATE manager_task_templates SET work_dir = '/target/example' WHERE manager_id = 'manager-review'")
        exported = await _export(pool)
        template = next(entry for entry in exported["managers"] if entry["id"] == "manager-review")["templates"][0]
        assert template["repoint"] is True and "work_dir" not in template
        preview = await _run(pool, exported, apply=False)
        assert any("existing target directory is preserved" in note for note in preview.notes)
        await _run(pool, exported)
        assert (await managers.get_template("manager-review", "review", pool=pool))["work_dir"] == "/target/example"

    run_db(test, monkeypatch, timeout=20)
