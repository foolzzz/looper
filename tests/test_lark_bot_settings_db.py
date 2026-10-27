"""Bot-token Lark settings and configuration migration on an isolated PostgreSQL schema."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.db_support import DSN, run_with_channel_configs
from tests.test_lark_channel_db import _call

requires_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPEN_ID = "ou_allowed000001"
OTHER = "ou_other00000001"
SECRET = "synthetic-app-secret"
CONFIG = {"app_id": "cli_main", "domain": "feishu", "allowed_open_ids": [OPEN_ID]}


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> Any:
    from looper.agents.channels import lark_api, lark_bot_settings

    class Api:
        instances: list[Any] = []
        identities: list[tuple[str, str, str]] = []
        failure: str | None = None
        names_failure = False

        def __init__(self, app_id: str, secret: str, *, base_url: str) -> None:
            self.app_id, self.secret, self.base_url = app_id, secret, base_url
            self.closed = False
            self.instances.append(self)

        async def bot_info(self) -> Any:
            self.identities.append((self.app_id, self.secret, self.base_url))
            if self.failure:
                raise lark_api.LarkApiError(self.failure)
            return SimpleNamespace(app_name="HelperBot", open_id="ou_bot00000001")

        async def names(self, ids: Any) -> dict[str, str]:
            if self.names_failure:
                raise lark_api.LarkApiError("names unavailable")
            return {i: {OPEN_ID: "Ada Lovelace", OTHER: "Alan Turing"}[i] for i in ids}

        async def search_people(self, query: str) -> tuple[list[dict[str, str]], bool]:
            if self.failure:
                raise lark_api.LarkApiError(self.failure)
            assert query == "alan"
            return [{"open_id": OTHER, "name": "Alan Turing", "email": "alan@example.test",
                     "department": "Research"}], False

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(lark_api, "LarkApi", Api)
    lark_bot_settings.forget()
    yield Api
    lark_bot_settings.forget()
    assert all(api.closed for api in Api.instances)


@requires_db
def test_seed_and_secret_api_contract(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    from looper.agents.serve import seed_channel_configs
    from looper.db.channel_configs import get_bot_token

    async def test(pool: Any) -> None:
        await seed_channel_configs(pool)
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_id = 'channel-lark_bot'") == 1
        status, row = await _call("GET", "channel-lark_bot")
        assert status == 200 and row["enabled"] is False and row["has_bot_token"] is False
        assert row["channel_type"] == "lark_bot" and row["display_name"] == "Lark (bot token)"
        assert row["config"]["app_id"] == "" and row["config"]["domain"] == "feishu"
        assert row["config"]["allowed_open_ids"] == []

        status, row = await _call("PUT", "channel-lark_bot", {
            "enabled": True, "config": CONFIG, "bot_token": SECRET})
        assert status == 200 and row["has_bot_token"] is True
        assert row["config"]["allowlist_app_id"] == CONFIG["app_id"]
        assert await get_bot_token("channel-lark_bot") == SECRET
        for path in ("channel-lark_bot", "channel_configs", "/api/v1/channels/status"):
            if path.startswith("/api/v1/"):
                status, answer = await _call("POST", path, {"channel_type": "lark_bot"})
            else:
                status, answer = await _call("GET", path)
            assert status == 200, answer
            assert SECRET not in json.dumps(answer)
            if path != "/api/v1/channels/status":
                rows = answer["configs"] if path.endswith("configs") else [answer]
                for item in rows:
                    assert "bot_token" not in item and "bot_token" not in item["config"]

        status, row = await _call("PUT", "channel-lark_bot", {"bot_token": "***"})
        assert status == 200 and row["has_bot_token"] is True
        assert await get_bot_token("channel-lark_bot") == SECRET
        status, _ = await _call("PUT", "channel-lark_bot", {"bot_token": 42})
        assert status == 400 and await get_bot_token("channel-lark_bot") == SECRET
        status, _ = await _call("PUT", "channel-lark_bot", {
            "bot_token": "replacement", "config": {**CONFIG, "domain": "unknown"}})
        assert status == 400 and await get_bot_token("channel-lark_bot") == SECRET
        status, row = await _call("PUT", "channel-lark_bot", {"bot_token": ""})
        assert status == 200 and row["has_bot_token"] is False
        assert await get_bot_token("channel-lark_bot") is None

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("app_id", 1, "invalid credential request"),
    ("app_id", "a", "invalid channel configuration; check the sender allowlist and bot settings"),
    ("app_id", "a bcd", "invalid channel configuration; check the sender allowlist and bot settings"),
    ("app_id", "a" * 65, "invalid channel configuration; check the sender allowlist and bot settings"),
    ("domain", "unknown", "invalid credential request"),
    ("domain", [], "invalid credential request"),
])
@requires_db
def test_invalid_configuration_is_refused(monkeypatch: pytest.MonkeyPatch, platform: Any,
                                          field: str, value: Any, message: str) -> None:
    async def test(pool: Any) -> None:
        status, answer = await _call("PUT", "channel-lark_bot", {
            "config": {**CONFIG, field: value}, "bot_token": SECRET})
        assert status == 400 and answer["error"] == message
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets WHERE channel_id = 'channel-lark_bot'") == 0

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_allowlist_binding_status_names_and_people(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    from looper.db.channel_configs import record_rejected

    async def test(pool: Any) -> None:
        status, saved = await _call("PUT", "channel-lark_bot", {
            "config": {**CONFIG, "app_id": " cli_main ", "allowlist_app_id": "forged"}, "bot_token": SECRET})
        assert status == 200 and saved["config"]["app_id"] == "cli_main"
        assert saved["config"]["allowlist_app_id"] == "cli_main"
        await record_rejected("channel-lark_bot", OTHER, chat_id="oc_team")
        await record_rejected("channel-lark_bot", OTHER, chat_id="oc_team")
        await record_rejected("channel-lark_bot", OPEN_ID)
        status, view = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark_bot"})
        assert status == 200 and view["problem"] is None
        assert view["bot"] == {"name": "HelperBot", "app_id": "cli_main", "open_id": "ou_bot00000001", "profile": ""}
        assert view["names"] == {OPEN_ID: "Ada Lovelace", OTHER: "Alan Turing"}
        [rejected] = view["rejected"]
        assert (rejected["open_id"], rejected["name"], rejected["count"], rejected["chat_id"]) == (
            OTHER, "Alan Turing", 2, "oc_team")
        assert set(rejected) == {"open_id", "name", "count", "last_at", "chat_id"}

        status, saved = await _call("PUT", "channel-lark_bot", {
            "config": {**saved["config"], "app_id": "cli_other", "allowlist_app_id": "forged"}})
        assert status == 200 and saved["config"]["allowlist_app_id"] == "cli_main"
        _, view = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark_bot"})
        assert "allowlist was made for app cli_main" in view["problem"]
        status, saved = await _call("PUT", "channel-lark_bot", {
            "config": {**saved["config"], "allowed_open_ids": [OTHER]}})
        assert status == 200 and saved["config"]["allowlist_app_id"] == "cli_other"
        status, saved = await _call("PUT", "channel-lark_bot", {
            "config": {**saved["config"], "allowed_open_ids": []}})
        assert status == 200 and "allowlist_app_id" not in saved["config"]

        status, found = await _call("POST", "/api/v1/channels/people", {"channel_type": "lark_bot", "q": " alan "})
        assert status == 200 and found["complete"] is False and found["people"][0]["open_id"] == OTHER
        for query in ("", "a" * 51):
            assert (await _call("POST", "/api/v1/channels/people", {"channel_type": "lark_bot", "q": query}))[0] == 400
        platform.failure = "permission denied"
        status, answer = await _call("POST", "/api/v1/channels/people", {"channel_type": "lark_bot", "q": "alan"})
        assert status == 503 and answer["error"]["code"] == "unavailable"
        assert "permission denied" in answer["error"]["message"]
        assert all(api.base_url == "https://open.feishu.cn" for api in platform.instances)
        platform.failure = None
        await _call("PUT", "channel-lark_bot", {"config": {**CONFIG, "domain": "lark"}})
        await _call("POST", "/api/v1/channels/status", {"channel_type": "lark_bot"})
        await _call("POST", "/api/v1/channels/people", {"channel_type": "lark_bot", "q": "alan"})
        assert all(api.base_url == "https://open.larksuite.com" for api in platform.instances[-3:])

    run_with_channel_configs(test, monkeypatch)


def test_check_problems_and_identity_cache(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    from looper.agents.channels import lark_bot_settings as settings

    async def test() -> None:
        assert "app_id" in (await settings.check({}, SECRET))[0]
        assert "app secret" in (await settings.check(CONFIG, None))[0]
        assert platform.instances == []
        assert (await settings.check(CONFIG, SECRET))[0] is None
        assert (await settings.check(CONFIG, SECRET))[0] is None
        assert len(platform.identities) == 1
        assert SECRET not in repr(settings._bots)
        await settings.check(CONFIG, SECRET, fresh=True)
        assert len(platform.identities) == 2
        await settings.check(CONFIG, "another-synthetic-secret")
        await settings.check({**CONFIG, "domain": "lark"}, SECRET)
        assert len(platform.identities) == 4
        assert platform.identities[-1][2] == "https://open.larksuite.com"
        monkeypatch.setattr(settings, "IDENTITY_TTL_SECONDS", -1)
        await settings.check(CONFIG, SECRET)
        assert len(platform.identities) == 5
        problem, bot = await settings.check({**CONFIG, "allowlist_app_id": "cli_other"}, SECRET)
        assert bot is not None and "open_ids differ per app" in problem
        platform.failure = "invalid credentials"
        assert await settings.check(CONFIG, SECRET, fresh=True) == (
            "the platform rejected the app credentials: invalid credentials", None)
        settings.forget()
        assert not settings._bots

    asyncio.run(test())


@pytest.mark.parametrize(("field", "value"), [("app_id", None), ("app_id", 1), ("app_id", "abc"),
                                             ("app_id", "a bcd"), ("app_id", "a" * 65),
                                             ("domain", "unknown"), ("domain", None), ("domain", [])])
def test_stored_config_validates_fields(field: str, value: Any) -> None:
    from looper.agents.channels.lark_bot_settings import stored_config

    with pytest.raises(ValueError, match=field):
        stored_config({**CONFIG, field: value})


@pytest.mark.parametrize("key", ["app_secret", "bot_token", "token", "extra"])
def test_stored_config_refuses_unknown_keys_without_exposing_values(key: str) -> None:
    from looper.agents.channels.lark_bot_settings import stored_config

    with pytest.raises(ValueError, match=key) as error:
        stored_config({**CONFIG, key: SECRET, "another_unknown": SECRET})
    assert "another_unknown" in str(error.value)
    assert SECRET not in str(error.value)


@requires_db
def test_config_secret_is_refused_before_any_write(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    from looper.agents.channels.allowlist import channel_view
    from looper.db.channel_configs import get_config, get_bot_token

    async def test(pool: Any) -> None:
        before = await get_config("channel-lark_bot")
        status, answer = await _call("PUT", "channel-lark_bot", {
            "enabled": True, "config": {**CONFIG, "app_secret": SECRET}, "bot_token": SECRET})
        assert status == 400 and answer["error"] == "invalid credential request"
        assert SECRET not in json.dumps(answer)
        after = await get_config("channel-lark_bot")
        assert after == before and await get_bot_token("channel-lark_bot") is None
        assert SECRET not in json.dumps(channel_view(after), default=str)
        for path in ("channel-lark_bot", "channel_configs"):
            status, answer = await _call("GET", path)
            assert status == 200 and SECRET not in json.dumps(answer)

    run_with_channel_configs(test, monkeypatch)


def test_config_secret_api_rejection_does_not_reach_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.channels.allowlist import channel_view
    from looper.db import channel_configs

    row = {"channel_id": "channel-lark_bot", "channel_type": "lark_bot", "config": CONFIG,
           "enabled": False, "has_bot_token": False}

    async def get_config(channel_id: str) -> dict[str, Any]:
        assert channel_id == row["channel_id"]
        return row

    async def list_configs() -> list[dict[str, Any]]:
        return [row]

    async def write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid configuration reached storage")

    monkeypatch.setattr(channel_configs, "get_config", get_config)
    monkeypatch.setattr(channel_configs, "list_configs", list_configs)
    monkeypatch.setattr(channel_configs, "set_config", write)
    monkeypatch.setattr(channel_configs, "set_bot_token", write)

    async def test() -> None:
        status, answer = await _call("PUT", "channel-lark_bot", {
            "config": {**CONFIG, "app_secret": SECRET}, "bot_token": SECRET})
        assert status == 400 and answer["error"] == "invalid credential request"
        assert SECRET not in json.dumps(answer)
        assert SECRET not in json.dumps(channel_view(row))
        for path in ("channel-lark_bot", "channel_configs"):
            status, answer = await _call("GET", path)
            assert status == 200 and SECRET not in json.dumps(answer)
            if path.endswith("configs"):
                assert answer["configs"][0]["channel_id"] == row["channel_id"]
            else:
                assert answer["config"] == CONFIG

    asyncio.run(test())


@requires_db
@pytest.mark.parametrize("apply", [False, True])
def test_import_rejects_extra_config_key_with_reason(monkeypatch: pytest.MonkeyPatch, apply: bool) -> None:
    from looper.agents.config_transfer import ConfigFileError, export_chunks, import_config
    from looper.db.channel_configs import get_config, list_configs

    async def test(pool: Any) -> None:
        before = await get_config("channel-lark_bot")
        async with pool.acquire() as conn:
            before_channels = await list_configs(pool=conn)
            data = json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            data["channels"][0]["display_name"] = "Must not be imported"
            entry = next(row for row in data["channels"] if row["channel_type"] == "lark_bot")
            entry["config"] = {**CONFIG, "extra": SECRET}
            async with conn.transaction():
                with pytest.raises(ConfigFileError, match="channels: unknown config keys: extra") as error:
                    await import_config(conn, data, apply=apply)
                assert SECRET not in str(error.value)
                # Read before commit so rollback cannot hide partial import writes.
                assert await list_configs(pool=conn) == before_channels
        assert await get_config("channel-lark_bot") == before

    run_with_channel_configs(test, monkeypatch)


def test_stored_config_defaults_and_allowlist_binding() -> None:
    from looper.agents.channels.lark_bot_settings import bound_config, stored_config

    async def test() -> None:
        assert stored_config({"description": "kept"}) == {"app_id": "", "domain": "feishu", "description": "kept"}
        assert stored_config({"app_id": " a_B1 ", "domain": "lark"}) == {"app_id": "a_B1", "domain": "lark"}
        bound = await bound_config({}, {**CONFIG, "allowlist_app_id": "forged"})
        assert bound["allowlist_app_id"] == "cli_main"
        changed_app = await bound_config(bound, {**bound, "app_id": "cli_other", "allowlist_app_id": "forged"})
        assert changed_app["allowlist_app_id"] == "cli_main"
        changed_ids = await bound_config(changed_app, {**changed_app, "allowed_open_ids": [OTHER]})
        assert changed_ids["allowlist_app_id"] == "cli_other"
        empty = await bound_config(changed_ids, {**changed_ids, "allowed_open_ids": []})
        assert "allowlist_app_id" not in empty
        unbound = await bound_config(CONFIG, CONFIG)
        assert unbound["allowlist_app_id"] == "cli_main"

    asyncio.run(test())


@requires_db
def test_status_and_people_without_credentials(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    async def test(pool: Any) -> None:
        status, view = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark_bot"})
        assert status == 200 and view["bot"] is None and "app_id" in view["problem"]
        status, answer = await _call("POST", "/api/v1/channels/people", {"channel_type": "lark_bot", "q": "alan"})
        assert status == 503 and answer["error"]["code"] == "unavailable" and "app_id" in answer["error"]["message"]
        await _call("PUT", "channel-lark_bot", {"config": CONFIG})
        status, view = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark_bot"})
        assert status == 200 and view["bot"] is None and "app secret" in view["problem"]
        status, answer = await _call("POST", "/api/v1/channels/people", {"channel_type": "lark_bot", "q": "alan"})
        assert status == 503 and answer["error"]["code"] == "unavailable"
        assert "app secret" in answer["error"]["message"]
        assert platform.instances == []

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_status_keeps_working_when_name_lookup_fails(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    async def test(pool: Any) -> None:
        await _call("PUT", "channel-lark_bot", {"config": CONFIG, "bot_token": SECRET})
        platform.names_failure = True
        status, view = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark_bot"})
        assert status == 200 and view["bot"] is not None and view["names"] == {} and view["problem"] is None

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_config_export_import_round_trip(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:
    from looper.agents.config_transfer import Action, ConfigFileError, export_chunks, import_config
    from looper.db.channel_configs import list_configs

    async def test(pool: Any) -> None:
        config = {**CONFIG, "allowlist_app_id": "cli_main", "description": "bot-token channel"}
        await _call("PUT", "channel-lark_bot", {
            "enabled": True, "config": config, "bot_token": SECRET})
        async with pool.acquire() as conn:
            data = json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            entry = next(row for row in data["channels"] if row["channel_type"] == "lark_bot")
            assert entry["config"] == config and SECRET not in json.dumps(data)
            await conn.execute("DELETE FROM channel_configs WHERE channel_id = 'channel-lark_bot'")
            async with conn.transaction():
                plan = await import_config(conn, data, apply=True)
            assert any(c.section == "channels" and c.key == "channel-lark_bot" and c.action == Action.ADDED
                       for c in plan.changes)
            assert any(n.key == "channel-lark_bot" and n.names == ("app secret (Settings → Channels)",)
                       for n in plan.need_secret)
            assert json.loads(await conn.fetchval("SELECT config FROM channel_configs "
                                                  "WHERE channel_id = 'channel-lark_bot'")) == config
            again = await import_config(conn, data, apply=False)
            assert any(c.section == "channels" and c.key == "channel-lark_bot" and c.action == Action.UNCHANGED
                       for c in again.changes)
            entry["config"]["app_id"] = " cli_other "
            async with conn.transaction():
                normalized = await import_config(conn, data, apply=True)
            assert not any(c.key == "channel-lark_bot" and c.action == Action.SKIPPED for c in normalized.changes)
            assert json.loads(await conn.fetchval("SELECT config FROM channel_configs "
                                                  "WHERE channel_id = 'channel-lark_bot'"))["app_id"] == "cli_other"
            entry["config"]["domain"] = "unknown"
            before_invalid = await list_configs(pool=conn)
            for apply in (False, True):
                async with conn.transaction():
                    with pytest.raises(ConfigFileError, match="channels: domain must be one of feishu, lark") as error:
                        await import_config(conn, data, apply=apply)
                    assert SECRET not in str(error.value)
                    assert await list_configs(pool=conn) == before_invalid

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_old_channel_constraints_are_widened_idempotently(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import connection
    from tests.test_gate_channels_db import _task_with_gate

    async def test(pool: Any) -> None:
        await pool.execute("ALTER TABLE task_user_gate_posts DROP CONSTRAINT task_user_gate_posts_channel_check")
        await pool.execute("ALTER TABLE task_user_gate_posts ADD CONSTRAINT task_user_gate_posts_channel_check "
                           "CHECK (channel IN ('lark', 'telegram'))")
        await pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_notify_channel_check")
        await pool.execute("ALTER TABLE schedules ADD CONSTRAINT schedules_notify_channel_check "
                           "CHECK (notify_channel IN ('lark', 'telegram'))")
        await pool.execute("DELETE FROM schema_migrations WHERE name IN ('task_user_gate_posts_lark_bot', "
                           "'schedules_notify_channel_lark_bot')")
        await _task_with_gate(pool, origin="channel-lark_bot", chat=OPEN_ID, kind="user")
        message_id = await pool.fetchval("SELECT id FROM task_user_gate_messages WHERE task_id = 'task-a' LIMIT 1")
        project_id = await pool.fetchval("SELECT project_id FROM tasks WHERE id = 'task-a'")

        async def write(post: bool) -> None:
            if post:
                await pool.execute("INSERT INTO task_user_gate_posts (message_id, task_id, channel, chat, external_id) "
                                   "VALUES ($1, 'task-a', 'lark_bot', $2, 'om_post')", message_id, OPEN_ID)
            else:
                await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, "
                                   "notify_channel, notify_chat) VALUES ('schedule-a', $1, 'daily', '0 1 * * *', "
                                   "'UTC', 'check', 'lark_bot', $2)", project_id, OPEN_ID)

        for post in (False, True):
            with pytest.raises(asyncpg.CheckViolationError):
                await write(post)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        await write(False)
        await write(True)
        assert await pool.fetchval("SELECT channel FROM task_user_gate_posts WHERE external_id = 'om_post'") == "lark_bot"
        assert await pool.fetchval("SELECT notify_channel FROM schedules WHERE id = 'schedule-a'") == "lark_bot"

    run_with_channel_configs(test, monkeypatch)
