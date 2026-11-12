"""Schedules offer explicit built-in bot bindings and never reroute a revoked target."""

from dataclasses import replace
import json
import uuid

import pytest

from looper.agents import schedules
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.ops import Turn
from looper.agents.ops.service import DEFAULT_SCOPE, ManagerOps, Platform
from looper.agents.serve import seed_channel_configs
from looper.db import chat_bindings, tasks
from tests.db_support import DSN, v1_api
from tests.schedule_support import ScheduleEnv, run_scheduled, with_sender

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def bind(env, channel="lark", chat="oc_x", name=None):
    await seed_channel_configs(env.pool)
    await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES "
                           "('manager-report', 'Reports', 'Inspect reports') ON CONFLICT DO NOTHING")
    async with env.pool.acquire() as conn, conn.transaction():
        await chat_bindings.put_binding(f"channel-{channel}", chat, "manager-report", conn=conn)
    if name:
        await chat_bindings.record_seen(f"channel-{channel}", chat, name, suppressed=False, pool=env.pool)


@pytest.mark.parametrize("channel,chat,name", [
    ("lark", "oc_x", "Project reports"), ("lark_bot", "oc_x", None), ("telegram", "-123", None),
])
def test_operator_can_select_named_or_unobserved_binding(monkeypatch, tmp_path, channel, chat, name):
    async def test(env: ScheduleEnv):
        await bind(env, channel, chat, name)
        field, sender = ("allowed_user_ids", "123") if channel == "telegram" else ("allowed_open_ids", "ou_abcdefgh12")
        await env.pool.execute("UPDATE channel_configs SET config = $2::jsonb WHERE channel_id = $1",
                               f"channel-{channel}", json.dumps({field: [sender]}))
        target = {"channel": channel, "chat": chat}
        offered = await schedules.notify_targets()
        label = "Telegram" if channel == "telegram" else "Lark"
        assert {**target, "label": f"{label} · {name or chat} (bound to Reports)",
                "channel_enabled": False} in offered
        assert any(t["channel"] == channel and t["chat"] == sender for t in offered)
        async with v1_api(tmp_path / "http", env.pool) as call:
            status, answer = await call("schedule_admin.create", {"project_id": env.project_id, "fields": {
                "name": "Reports", "cron": "*/5 * * * *", "requirement": "Inspect reports", "notify_target": target}})
        assert status == 200 and answer["result"]["schedule"]["notify_target"] == target, answer
        status, answer = await env.call("POST", "/api/v1/schedule_admin/create", {
            "project_id": env.project_id, "fields": {"name": "Refused", "cron": "*/5 * * * *",
                "requirement": "Inspect reports", "notify_target": {"channel": channel, "chat": "unknown"}}})
        assert status == 400 and "not one the channel allows" in answer["error"]["message"]
        await env.pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name) "
                               "VALUES ($1, $2, 'Another bot')", f"channel-{channel}-2", channel)
        async with env.pool.acquire() as conn, conn.transaction():
            await chat_bindings.put_binding(f"channel-{channel}-2", chat + "2", "manager-report", conn=conn)
        assert not any(t["chat"] == chat + "2" for t in await schedules.notify_targets())
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("channel", ["lark", "lark_bot"])
def test_bound_notice_uses_exact_bot_and_chat_then_stops_after_unbinding(
    monkeypatch, tmp_path, caplog, lark_cli_stand_in, channel,
):
    async def test(env: ScheduleEnv):
        await bind(env, channel)
        with_sender(env)
        registry = ChannelRegistry(env.bus)
        adapter = LarkChannelAdapter(channel_type=channel, profile="other", runtime_root=tmp_path,
            allowlist=SenderAllowlist(f"channel-{channel}", frozenset({"ou_abcdefgh12"})))
        # Install the real adapter and CLI stand-in without starting an event consumer.
        registry._channels[adapter.channel_id] = adapter
        env.scheduler._notifier._sender = registry.notify
        target = {"channel": channel, "chat": "oc_x"}
        sid = (await env.schedule(notify="always", notify_target=target))["schedule_id"]
        for removed in (False, True):
            if removed:
                async with env.pool.acquire() as conn, conn.transaction():
                    await chat_bindings.delete_binding(f"channel-{channel}", "oc_x", conn=conn)
                # A binding of the same chat on another bot must not become a fallback.
                other = "lark_bot" if channel == "lark" else "lark"
                await bind(env, other)
            run = (await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid}))["run"]
            await env.end_task(run["task_id"], "completed", summary="Report ready")
            await env.scheduler.idle()
        calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
        sends = [call for call in calls if "+messages-send" in call]
        assert len(sends) == 1
        assert sends[0][:8] == ["--profile", "other", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_x"]
        notices = await env.pool.fetch("SELECT status, error FROM schedule_notifications WHERE schedule_id = $1 "
                                       "ORDER BY created_at", sid)
        assert [n["status"] for n in notices] == ["sent", "failed"]
        assert "unavailable" in notices[-1]["error"]
        assert any(r.levelname == "WARNING" and sid in r.getMessage() for r in caplog.records)
        page = await env.ok("POST", "/api/v1/schedule_admin/project_list", {"project_id": env.project_id})
        assert page["schedules"][0]["notify_target"] == target
        assert not any((t["channel"], t["chat"]) == (channel, "oc_x") for t in page["limits"]["notify_targets"])
        detail = await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid})
        assert detail["schedule"]["notify_target_available"] is False
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("target_chat", ["oc_x", "oc_other", "ou_abcdefgh12"])
@pytest.mark.parametrize("task_scope", ["own", "projects"])
def test_scoped_manager_only_selects_its_own_binding(monkeypatch, tmp_path, target_chat, task_scope):
    async def test(env: ScheduleEnv):
        await bind(env)
        async with env.pool.acquire() as conn, conn.transaction():
            await chat_bindings.put_binding("channel-lark", "oc_other", "manager", conn=conn)
        await env.pool.execute("UPDATE channel_configs SET config = '{\"allowed_open_ids\": [\"ou_abcdefgh12\"]}' "
                               "WHERE channel_id = 'channel-lark'")
        ops = ManagerOps(Platform(bus=env.bus, task_router=None, runtime_root=tmp_path),
                         url="http://127.0.0.1:9", token="test")

        async def call(arguments):
            turn = Turn(channel="lark", channel_id="channel-lark", chat="oc_x", sender="ou_abcdefgh12",
                        message_id=uuid.uuid4().hex)
            ops.begin_turn(turn)
            try:
                return await ops.call("looper_schedules", arguments)
            finally:
                ops.end_turn(turn)

        # Exercise the schedule write boundary with the tool enabled for a scoped Manager.
        ops.set_scope(replace(DEFAULT_SCOPE, manager_id="manager-report",
            read_only=frozenset({"looper_projects"}),
            tasks=(tasks.TaskFilter(manager_id="manager-report") if task_scope == "own"
                   else tasks.TaskFilter(project_ids=(env.project_id,)))))
        fields = {"name": "Report", "cron": "*/5 * * * *", "requirement": "Inspect reports",
                  "notify_target": {"channel": "lark", "chat": target_chat}}
        answer = await call({"action": "create", "project": "alerts", "fields": fields})
        if target_chat == "oc_x":
            assert answer["status"] == "ok", answer
        else:
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", answer
            assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0
        sid = (await env.schedule(notify_target={"channel": "lark", "chat": "oc_x"}))["schedule_id"]
        updated = await call({"action": "update", "schedule": sid, "fields": {"notify_target": fields["notify_target"]}})
        assert updated["status"] == ("ok" if target_chat == "oc_x" else "error"), updated
        if target_chat != "oc_x":
            assert updated["error"]["code"] == "forbidden"
        implicit = await call({"action": "create", "project": "alerts", "fields": {
            "name": "Dashboard report", "cron": "*/5 * * * *", "requirement": "Inspect reports"}})
        assert implicit["status"] == "ok", implicit
        assert await env.pool.fetchval("SELECT notify_chat FROM schedules WHERE name = 'Dashboard report'") is None
    run_scheduled(test, monkeypatch, tmp_path)
