"""Manager bot transfer preserves destination identities, credentials and local routing policy."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from looper.agents import config_transfer as transfer
from looper.agents.serve import seed_channel_configs
from looper.db import channel_configs, chat_bindings
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _manager(identity: str) -> dict[str, Any]:
    return {"id": identity, "name": identity, "system_prompt": "Handle synthetic test tasks.", "enabled": False}


def _bot(suffix: int = 2, *, kind: str = "telegram", owner: str = "manager-first", **fields: Any) -> dict[str, Any]:
    return {"channel_id": f"channel-{kind}-{suffix}", "channel_type": kind, "manager_id": owner,
            "display_name": f"Example {suffix}", "enabled": True, "config": {"allowed_user_ids": [str(suffix)]}
            if kind == "telegram" else {}, **fields}


def _document(*bots: dict[str, Any], owners: bool = True) -> dict[str, Any]:
    return {"format": transfer.V4_FORMAT, "channels": list(bots), **({"managers": [
        _manager(identity) for identity in sorted({bot["manager_id"] for bot in bots} - {"manager"})]}
        if owners else {})}


async def _run(pool: Any, document: dict[str, Any], *, apply: bool = True) -> Any:
    async with pool.acquire() as conn:
        return await transfer.run_import(conn, document, apply=apply)


async def _export(pool: Any, *, secrets: bool = False) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        return json.loads("".join([chunk async for chunk in transfer.export_chunks(
            conn, include_projects=True, include_secrets=secrets)]))


async def _sequence(pool: Any) -> tuple[int, bool]:
    return tuple(await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq"))


async def _state(pool: Any) -> dict[str, Any]:
    return {table: [dict(row) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("managers", "channel_configs", "channel_secrets", "manager_chat_bindings",
                          "channel_seen_chats", "channel_unbound_chats")}


def test_same_type_roundtrip_owners_missing_secret_targets_and_readonly_preview(
    monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Any,
) -> None:
    from looper.db.connection import ensure_schema

    async def test(pool: Any) -> None:
        document = _document(_bot(), _bot(3, owner="manager-second"))
        await _run(pool, document)
        await pool.execute("INSERT INTO channel_secrets (channel_id, bot_token) VALUES "
                           "('channel-telegram-2', '222:synthetic-first'), ('channel-telegram-3', '333:synthetic-second')")
        exported = await _export(pool, secrets=True)
        assert exported["format"] == transfer.V4_FORMAT
        assert [row["channel_id"] for row in exported["channels"] if row["channel_id"].endswith(("-2", "-3"))] == [
            "channel-telegram-2", "channel-telegram-3"]
        assert "synthetic-first" not in json.dumps(exported) and "synthetic-second" not in json.dumps(exported)
        assert all("bound_chats_only" not in row for row in exported["channels"])
        assert not ({"manager_chat_bindings", "channel_seen_chats", "channel_unbound_chats"} & exported.keys())
        async with fresh_schema() as target:
            await ensure_schema(target)
            await seed_channel_configs(target)
            before, sequence = await _state(target), await _sequence(target)
            plan = await _run(target, exported, apply=False)
            assert await _state(target) == before and await _sequence(target) == sequence
            changes = [row for row in plan.view()["changes"] if row["section"] == "channels"]
            assert {row["key"] for row in changes if row["action"] == "added"} == {
                "channel-telegram-2", "channel-telegram-3"}
            assert {row["target"]["id"] for row in plan.view(targets=True)["need_secret"]
                    if row.get("target", {}).get("kind") == "channel"} >= {"channel-telegram-2", "channel-telegram-3"}
            await _run(target, exported)
            rows = await target.fetch("SELECT channel_id, manager_id, enabled, bound_chats_only FROM channel_configs "
                                      "WHERE channel_id IN ('channel-telegram-2', 'channel-telegram-3') ORDER BY channel_id")
            assert [tuple(row) for row in rows] == [("channel-telegram-2", "manager-first", False, False),
                                                   ("channel-telegram-3", "manager-second", False, False)]
            before = await _state(target)
            repeated = await _run(target, exported)
            assert repeated.writes == 0 and await _state(target) == before
            assert (await _sequence(target))[0] >= 3

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=30)


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("apply", [False, True])
def test_missing_or_deleted_manager_rejects_entire_graph(monkeypatch: pytest.MonkeyPatch, deleted: bool, apply: bool) -> None:
    async def test(pool: Any) -> None:
        if deleted:
            await _run(pool, {"format": transfer.V4_FORMAT, "managers": [_manager("manager-first")]})
            await pool.execute("UPDATE managers SET deleted_at = now() WHERE id = 'manager-first'")
        before, sequence = await _state(pool), await _sequence(pool)
        with pytest.raises(transfer.ConfigFileError):
            await _run(pool, _document(_bot(), owners=False), apply=apply)
        assert await _state(pool) == before and await _sequence(pool) == sequence

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("bots", [
    [_bot(channel_id="channel-telegram-02")], [_bot(channel_id="telegram-example")],
    [_bot(channel_id="channel-telegram-9223372036854775808")],
    [_bot(channel_id="channel-lark-2")], [_bot(), _bot()],
    [_bot(), _bot(kind="lark_bot", config={"app_id": "cli_example"})],
    [_bot(channel_id="channel-telegram", owner="manager-first")],
])
def test_invalid_ids_type_owner_and_duplicate_suffix_reject_before_writes(monkeypatch: pytest.MonkeyPatch, bots: Any) -> None:
    async def test(pool: Any) -> None:
        before, sequence = await _state(pool), await _sequence(pool)
        with pytest.raises(transfer.ConfigFileError):
            await _run(pool, _document(*bots))
        assert await _state(pool) == before and await _sequence(pool) == sequence

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=15)


def test_existing_id_owner_is_immutable_and_type_cannot_be_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_bot()))
        before = await _state(pool)
        for bot in (_bot(owner="manager-second"), _bot(kind="lark_bot", channel_id="channel-telegram-2")):
            with pytest.raises(transfer.ConfigFileError):
                await _run(pool, _document(bot))
            assert await _state(pool) == before

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("called", [False, True])
def test_consumed_absent_ids_are_not_recycled_and_preview_never_advances_sequence(monkeypatch: pytest.MonkeyPatch, called: bool) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("SELECT setval('channel_bot_seq', 5, $1)", called)
        await _run(pool, {"format": transfer.V4_FORMAT, "managers": [_manager("manager-first")]})
        before, sequence = await _state(pool), await _sequence(pool)
        for apply in (False, True):
            with pytest.raises(transfer.ConfigFileError, match="locally|local|consum|allocat|conflict"):
                await _run(pool, _document(_bot(4), owners=False), apply=apply)
            assert await _state(pool) == before and await _sequence(pool) == sequence
        if called:
            with pytest.raises(transfer.ConfigFileError):
                await _run(pool, _document(_bot(5), owners=False), apply=False)
        else:
            await _run(pool, _document(_bot(5), owners=False), apply=False)
            assert await _sequence(pool) == sequence

    run_db(test, monkeypatch, timeout=15)


def test_local_create_concurrent_with_import_never_reuses_or_collapses_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.channels import bot_api
    from looper.db import config_transfer as db_transfer

    async def test(pool: Any) -> None:
        await _run(pool, {"format": transfer.V4_FORMAT, "managers": [_manager("manager-first")]})
        import_locked, local_started = asyncio.Event(), asyncio.Event()
        reserve = db_transfer.reserve_bot_ids

        async def reserve_while_local_waits(*args: Any, **kwargs: Any) -> Any:
            import_locked.set()
            await local_started.wait()
            return await reserve(*args, **kwargs)

        monkeypatch.setattr(db_transfer, "reserve_bot_ids", reserve_while_local_waits)
        async def local() -> Any:
            await import_locked.wait()
            local_started.set()
            return await bot_api.add_bot({"channel_type": "telegram", "manager_id": "manager-first",
                "display_name": "Locally created", "enabled": False, "config": {}, "bot_token": "999:synthetic-local"})
        results = await asyncio.gather(_run(pool, _document(_bot(2), owners=False)), local(), return_exceptions=True)
        assert not any(isinstance(result, Exception) for result in results), results
        rows = await pool.fetch("SELECT channel_id, display_name FROM channel_configs ORDER BY channel_id")
        assert [tuple(row) for row in rows] == [("channel-telegram-2", "Example 2"),
                                               ("channel-telegram-3", "Locally created")]
        assert (await _sequence(pool))[0] >= max(int(row["channel_id"].rsplit("-", 1)[1]) for row in rows)

    run_db(test, monkeypatch, timeout=20)


def test_unchanged_enabled_retained_changed_disabled_and_absent_rows_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_bot(), _bot(3)))
        await pool.execute("UPDATE channel_configs SET enabled = true WHERE channel_id LIKE 'channel-telegram-%'")
        document = _document(_bot(enabled=False), owners=False)
        unchanged = await _run(pool, document)
        assert unchanged.writes == 0
        assert await pool.fetchval("SELECT enabled FROM channel_configs WHERE channel_id='channel-telegram-2'")
        document["channels"][0]["display_name"] = "Changed locally portable settings"
        preview = await _run(pool, document, apply=False)
        assert any("disabl" in note.lower() and "activat" in note.lower() for note in preview.notes)
        assert await pool.fetchval("SELECT enabled FROM channel_configs WHERE channel_id='channel-telegram-2'")
        await _run(pool, document)
        assert not await pool.fetchval("SELECT enabled FROM channel_configs WHERE channel_id='channel-telegram-2'")
        assert await pool.fetchval("SELECT enabled FROM channel_configs WHERE channel_id='channel-telegram-3'")

    run_db(test, monkeypatch, timeout=15)


def test_placeholders_keep_saved_credentials_and_existing_policy_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        bot = _bot(config={"allowed_user_ids": ["42"], "private_setting": "synthetic-private-value"})
        await _run(pool, _document(bot))
        await pool.execute("INSERT INTO channel_secrets (channel_id, bot_token) VALUES ('channel-telegram-2', '222:synthetic-token')")
        async with pool.acquire() as conn, conn.transaction():
            await chat_bindings.set_bound_chats_only("channel-telegram-2", True, conn=conn)
            await chat_bindings.put_binding("channel-telegram-2", "42", "manager-first", conn=conn)
        exported = await _export(pool)
        added = next(row for row in exported["channels"] if row["channel_id"] == "channel-telegram-2")
        assert added["config"]["private_setting"] == transfer.PLACEHOLDER
        assert "synthetic-token" not in json.dumps(exported)
        before = await _state(pool)
        await _run(pool, exported)
        assert await _state(pool) == before
        assert await channel_configs.get_bot_token("channel-telegram-2", pool=pool) == "222:synthetic-token"

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("field", ["bot_token", "app_secret", "bound_chats_only", "manager_chat_bindings",
                                    "channel_seen_chats", "channel_unbound_chats"])
def test_added_bot_closed_shape_blocks_credentials_and_local_policy(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    async def test(pool: Any) -> None:
        before = await _state(pool)
        with pytest.raises(transfer.ConfigFileError):
            await _run(pool, _document(_bot(**{field: "synthetic-private-value"})))
        assert await _state(pool) == before

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("field", ["manager_chat_bindings", "channel_seen_chats", "channel_unbound_chats"])
def test_local_binding_tables_are_not_managed_transfer_sections(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    async def test(pool: Any) -> None:
        before = await _state(pool)
        with pytest.raises(transfer.ConfigFileError):
            await _run(pool, {**_document(_bot()), field: []})
        assert await _state(pool) == before

    run_db(test, monkeypatch, timeout=15)


@pytest.mark.parametrize("kind", ["lark", "lark_bot"])
def test_duplicate_final_actual_app_identity_rejects_atomically(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from looper.agents.channels import lark_cli

    async def identity(profile: str | None = None, *, fresh: bool = False) -> Any:
        assert fresh
        return lark_cli.Identity(profile or "", "cli_duplicate", "Synthetic", "ou_synthetic")
    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        document = _document(_bot(kind=kind, config={"profile": "example"} if kind == "lark" else {"app_id": "cli_duplicate"}),
            _bot(3, kind="lark_bot", config={"app_id": "cli_duplicate"}))
        before, sequence = await _state(pool), await _sequence(pool)
        for apply in (False, True):
            with pytest.raises(transfer.ConfigFileError):
                await _run(pool, copy.deepcopy(document), apply=apply)
            assert await _state(pool) == before
            if not apply:
                assert await _sequence(pool) == sequence

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("version", ["loopx-config/v1", "looper-config/v1", "looper-config/v2", "looper-config/v3",
                                     None, "looper-config/v5", "unknown"])
def test_unsupported_file_formats_require_the_bridge_before_bot_admission(version: str | None, tmp_path: Any) -> None:
    path = tmp_path / "unsupported.json"
    document = _document(_bot())
    if version is None:
        document.pop("format")
    else:
        document["format"] = version
    path.write_text(json.dumps(document))
    with pytest.raises(transfer.ConfigFileError, match="v2.13.165") as refused:
        transfer.read_file(path)
    assert "required format looper-config/v4" in str(refused.value)
    assert "upgrade" in str(refused.value) and "export v4" in str(refused.value)


def test_import_checks_stored_actual_identity_and_never_partially_writes_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_bot(kind="lark_bot", config={"app_id": "cli_stored"})))
        before = await _state(pool)
        document = _document(_bot(3, kind="lark_bot", owner="manager-second", config={"app_id": "cli_stored"}))
        for apply in (False, True):
            with pytest.raises(transfer.ConfigFileError):
                await _run(pool, document, apply=apply)
            assert await _state(pool) == before

    run_db(test, monkeypatch, timeout=20)


def test_identity_checks_use_final_imported_configs_for_existing_and_new_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _run(pool, _document(_bot(kind="lark_bot", config={"app_id": "cli_previous"})))
        document = _document(_bot(kind="lark_bot", config={"app_id": "cli_changed"}),
                             _bot(3, kind="lark_bot", config={"app_id": "cli_previous"}))
        preview = await _run(pool, document, apply=False)
        assert preview.view()["counts"]["channels"]["added"] == 1
        assert preview.view()["counts"]["channels"]["changed"] == 1
        await _run(pool, document)
        rows = await pool.fetch("SELECT channel_id, config->>'app_id' AS app_id FROM channel_configs ORDER BY channel_id")
        assert [tuple(row) for row in rows] == [("channel-lark_bot-2", "cli_changed"),
                                               ("channel-lark_bot-3", "cli_previous")]

    run_db(test, monkeypatch, timeout=20)


def test_disabled_unconfigured_builtin_lark_roundtrips_without_identity_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock
    from looper.agents.channels import lark_cli
    from looper.db.connection import ensure_schema

    identity = AsyncMock(side_effect=lark_cli.LarkCliError("synthetic unavailable CLI"))
    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        await _run(pool, _document(_bot()))
        document = await _export(pool)
        assert {row["channel_id"] for row in document["channels"]} == {
            "channel-telegram", "channel-lark", "channel-lark_bot", "channel-telegram-2"}
        async with fresh_schema() as target:
            await ensure_schema(target)
            await seed_channel_configs(target)
            preview = await _run(target, document, apply=False)
            assert preview.view()["counts"]["channels"]["added"] == 1
            await _run(target, document)
            row = await target.fetchrow("SELECT manager_id, enabled FROM channel_configs "
                                        "WHERE channel_id='channel-telegram-2'")
            assert tuple(row) == ("manager-first", False)
            assert not await target.fetchval("SELECT bool_or(enabled) FROM channel_configs WHERE channel_type='lark'")
        identity.assert_not_awaited()

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=25)


def test_old_v4_backup_defaults_builtin_owners_to_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from looper.db.connection import ensure_schema

    async def test(pool: Any) -> None:
        await _run(pool, {"format": transfer.V4_FORMAT, "managers": [_manager("manager-first")]})
        document = await _export(pool)
        assert document["format"] == transfer.V4_FORMAT
        for channel in document["channels"]:
            del channel["manager_id"]  # The exporter before Manager bots omitted built-in owners in v4.
        path = tmp_path / "backup.json"
        path.write_text(json.dumps(document))
        restored = transfer.read_file(path)
        async with fresh_schema() as target:
            await ensure_schema(target)
            before = await _state(target)
            await _run(target, restored, apply=False)
            assert await _state(target) == before
            await _run(target, restored)
            assert await target.fetchval("SELECT count(*) FROM managers WHERE id='manager-first'") == 1
            rows = await target.fetch("SELECT channel_id, manager_id FROM channel_configs ORDER BY channel_id")
            assert [tuple(row) for row in rows] == [(channel, "manager") for channel in
                ("channel-lark", "channel-lark_bot", "channel-telegram")]

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=25)


@pytest.mark.parametrize("include_default", [False, True])
def test_lark_bot_backup_ignores_unconfigured_default_competitor(monkeypatch: pytest.MonkeyPatch, include_default: bool) -> None:
    from unittest.mock import AsyncMock
    from looper.agents.channels import lark_cli
    from looper.db.connection import ensure_schema

    identity = AsyncMock(side_effect=lark_cli.LarkCliError("synthetic unavailable CLI"))
    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-first', 'First', 'Be.')")
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, manager_id, config) VALUES "
                           "('channel-lark_bot-2', 'lark_bot', 'manager-first', '{\"app_id\":\"cli_imported\"}')")
        document = await _export(pool)
        if not include_default:
            document["channels"] = [row for row in document["channels"] if row["channel_id"] != "channel-lark"]
        async with fresh_schema() as target:
            await ensure_schema(target)
            await seed_channel_configs(target)
            before = await _state(target)
            preview = await _run(target, document, apply=False)
            assert preview.view()["counts"]["channels"]["added"] == 1
            assert await _state(target) == before
            await _run(target, document)
            row = await target.fetchrow("SELECT manager_id, enabled, config->>'app_id' FROM channel_configs "
                                        "WHERE channel_id='channel-lark_bot-2'")
            assert tuple(row) == ("manager-first", False, "cli_imported")
        identity.assert_not_awaited()

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=25)


@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("unavailable", ["error", "empty"])
def test_lark_backup_without_local_identity_imports_disabled_with_followup(
    monkeypatch: pytest.MonkeyPatch, builtin: bool, unavailable: str,
) -> None:
    from looper.agents.channels import lark_cli
    from looper.db.connection import ensure_schema

    broken = False

    async def identity(profile: str | None = None, *, fresh: bool = False) -> Any:
        assert fresh
        if profile is None:
            return lark_cli.Identity("", "cli_default", "Default", "ou_default")
        assert profile == "work"
        if broken and unavailable == "error":
            raise lark_cli.LarkCliError("synthetic-private-error /private/profile")
        return lark_cli.Identity(profile, "" if broken else "cli_work", "Work", "ou_work")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        nonlocal broken
        lark = _bot(kind="lark", owner="manager" if builtin else "manager-first", config={"profile": "work"})
        if builtin:
            lark["channel_id"] = "channel-lark"
        await _run(pool, _document(lark, _bot(3)))
        document = await _export(pool)
        broken = True
        async with fresh_schema() as target:
            await ensure_schema(target)
            await seed_channel_configs(target)
            before = await _state(target)
            preview = await _run(target, document, apply=False)
            assert await _state(target) == before
            applied = await _run(target, document)
            for plan in (preview, applied):
                assert any(item["key"] == lark["channel_id"] for item in plan.view()["need_secret"])
                assert "synthetic-private-error" not in json.dumps(plan.view())
            assert await target.fetchval("SELECT count(*) FROM managers WHERE id='manager-first'") == 1
            assert await target.fetchval("SELECT count(*) FROM channel_configs WHERE channel_id='channel-telegram-3'") == 1
            row = await target.fetchrow("SELECT enabled, config->>'profile' FROM channel_configs WHERE channel_id=$1",
                                        lark["channel_id"])
            assert tuple(row) == (False, "work")

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=25)


@pytest.mark.parametrize("same_profile", [False, True])
def test_unresolved_lark_identity_does_not_hide_proven_collision(monkeypatch: pytest.MonkeyPatch, same_profile: bool) -> None:
    from unittest.mock import AsyncMock
    from looper.agents.channels import lark_cli

    identity = AsyncMock(side_effect=lark_cli.LarkCliError("synthetic unavailable CLI"))
    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        bots = [_bot(kind="lark", config={"profile": "work"})]
        bots += ([_bot(3, kind="lark", config={"profile": "work"})] if same_profile else [
            _bot(3, kind="lark_bot", config={"app_id": "cli_duplicate"}),
            _bot(4, kind="lark_bot", config={"app_id": "cli_duplicate"})])
        before = await _state(pool)
        for apply in (False, True):
            with pytest.raises(transfer.ConfigFileError, match="identity conflict"):
                await _run(pool, _document(*bots), apply=apply)
            assert await _state(pool) == before

    run_db(test, monkeypatch, timeout=20)


def test_configured_disabled_competitor_is_verified_and_unresolved_import_needs_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.channels import lark_cli

    broken = True
    calls: list[str | None] = []

    async def identity(profile: str | None = None, *, fresh: bool = False) -> Any:
        assert fresh and profile == "work"
        calls.append(profile)
        if broken:
            raise lark_cli.LarkCliError("synthetic unavailable CLI")
        return lark_cli.Identity(profile, "cli_shared", "Work", "ou_work")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        nonlocal broken
        await pool.execute("UPDATE channel_configs SET config='{\"profile\":\"work\"}' WHERE channel_id='channel-lark'")
        document = _document(_bot(kind="lark_bot", config={"app_id": "cli_shared"}), _bot(3))
        before = await _state(pool)
        preview = await _run(pool, document, apply=False)
        assert await _state(pool) == before
        assert any(row["key"] == "channel-lark_bot-2" and "identity verification" in " ".join(row["names"])
                   for row in preview.view()["need_secret"])
        await _run(pool, document)
        assert await pool.fetchval("SELECT enabled FROM channel_configs WHERE channel_id='channel-lark_bot-2'") is False
        assert calls
        broken = False
        before = await _state(pool)
        for apply in (False, True):
            with pytest.raises(transfer.ConfigFileError, match="identity conflict"):
                await _run(pool, document, apply=apply)
            assert await _state(pool) == before

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=20)


@pytest.mark.parametrize("name", [None, "omitted"])
def test_nullable_or_absent_display_name_uses_database_empty_name_and_repeats_unchanged(monkeypatch: pytest.MonkeyPatch, name: Any) -> None:
    async def test(pool: Any) -> None:
        bot = _bot(display_name=name)
        if name == "omitted":
            bot.pop("display_name")
        document = _document(bot)
        await _run(pool, document)
        row = await pool.fetchrow("SELECT display_name, enabled FROM channel_configs WHERE channel_id='channel-telegram-2'")
        assert tuple(row) == ("", False)
        assert (await _run(pool, document)).writes == 0

    run_db(test, monkeypatch, timeout=15)
