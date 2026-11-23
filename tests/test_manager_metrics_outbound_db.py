"""Outbound measurements through the operator API on disposable PostgreSQL."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from looper.agents import chat_settings
from looper.agents.channels import outbound
from looper.agents.channels.lark_cli import LarkCliError
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL required")


def test_outbound_sums_window_and_channels(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.channels import outbound_metrics as metrics
        events = metrics.EventBuffer(clock=lambda: 200000)
        monkeypatch.setattr(metrics, "events", events)
        monkeypatch.setattr(chat_settings, "_outbound_enabled", True)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name) VALUES "
                           "('channel-lark', 'lark', 'Synthetic bot'), "
                           "('channel-telegram', 'telegram', '')")
        for i, (state, error_class, age) in enumerate([
            ("pending", None, 48), ("sending", None, 0), ("sent", None, 0),
            ("failed", "rejected", 0), ("failed", "transient", 0),
            ("failed", "rejected", 25), ("uncertain", "unknown", 0), ("uncertain", "unknown", 25),
        ]):
            await pool.execute("INSERT INTO channel_outbox "
                "(channel_id,idempotency_key,kind,chat,body,source,state,error_class,created_at,sent_at) "
                "VALUES ('channel-lark',$1,'send','synthetic','{}','answer',$2,$3, "
                "now()-$4 * interval '1 hour', CASE WHEN $2='sent' THEN now() END)", str(i), state, error_class, age)
        # Retained deliveries still count even after their channel config is deleted.
        await pool.execute("INSERT INTO channel_outbox (channel_id,idempotency_key,kind,chat,body,source) "
                           "VALUES ('channel-lark-9','orphan','send','synthetic','{}','answer')")
        events.record("channel-lark", metrics.EventKind.BACKOFF, at=200000-86401)
        for kind in [metrics.EventKind.BACKOFF, metrics.EventKind.BACKOFF, metrics.EventKind.BREAKER_OPEN]:
            events.record("channel-lark", kind)
        gateway = SimpleNamespace(breaker=SimpleNamespace(state="open"))
        registry = SimpleNamespace(get=lambda cid: SimpleNamespace(_gateway=gateway) if cid == "channel-lark" else None)
        from looper.agents.http_server import AgentHTTPServer
        servers = []
        start = AgentHTTPServer.start
        async def capture(server):
            server._channels = registry
            servers.append(server)
            await start(server)
        monkeypatch.setattr(AgentHTTPServer, "start", capture)
        async with v1_api(tmp_path, pool) as call:
            server = servers[0]
            for days in (1, 7):
                status, body = await call("manager_metrics.get", {"days": days})
                assert status == 200
                result = body["result"]
                assert result["enabled"] is False  # the outbound switch is independent
                assert result["outbound_since_start"] is True
                assert datetime.fromisoformat(result["outbound_started_at"]) == server._started_at
                rows = {row["channel_id"]: row for row in result["outbound"]}
                assert rows["channel-lark"] == dict(channel_id="channel-lark", name="Synthetic bot", queued=2,
                    backoffs_24h=2, breaker_opens_24h=1, failed_24h={"rejected": 1, "transient": 1},
                    uncertain_24h=1, breaker_state="open")
                assert rows["channel-telegram"]["name"] == "channel-telegram"
                assert rows["channel-telegram"]["queued"] == 0
                assert rows["channel-telegram"]["breaker_state"] is None
                assert rows["channel-lark-9"]["queued"] == 1
            server._started_at -= timedelta(days=2)
            assert (await call("manager_metrics.get"))[1]["result"]["outbound_since_start"] is False
            assert (await call("manager_metrics.get", client=True))[0] == 403
    run_db(check, monkeypatch)


@pytest.mark.parametrize("outcome,channel_id,keyed_window,attempted", [
    ("failed", "channel-lark", 3600, True),
    ("uncertain", "channel-lark", 3600, True),
    ("recover", "channel-lark", 3600, True),
    ("recover", "channel-telegram", 0, False),
])
def test_old_delivery_becomes_visible_when_it_reaches_a_terminal_state(
        monkeypatch, tmp_path, outcome, channel_id, keyed_window, attempted):
    async def check(pool):
        from looper.agents.channels import outbound_metrics as metrics
        from looper.db import channel_outbox

        monkeypatch.setattr(metrics, "events", metrics.EventBuffer())
        monkeypatch.setattr(chat_settings, "_outbound_enabled", True)
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=48)
        row = await channel_outbox.insert_or_get(dict(channel_id=channel_id, idempotency_key="old",
            kind="send", chat="synthetic", body={"text": "hi"}, source="answer"))
        await channel_outbox.mark_sending(row["id"])
        if attempted:
            await channel_outbox.mark_attempted(row["id"], old)
        await pool.execute("UPDATE channel_outbox SET created_at=$1 WHERE id=$2", old, row["id"])
        if outcome == "failed":
            await channel_outbox.mark_failed(row["id"], "rejected", "synthetic refusal")
        elif outcome == "uncertain":
            await channel_outbox.mark_uncertain(row["id"], "synthetic timeout")
        else:
            await channel_outbox.recover(channel_id, now, keyed_window)
        assert (await channel_outbox.get(row["id"]))["state"] == (
            "failed" if outcome == "failed" else "uncertain")
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("manager_metrics.get")
            assert status == 200
            channels = body["result"]["outbound"]
            assert len(channels) == 1
            assert channels[0]["queued"] == 0
            assert channels[0]["failed_24h"] == ({"rejected": 1} if outcome == "failed" else {})
            assert channels[0]["uncertain_24h"] == (0 if outcome == "failed" else 1)
        terminal_at = (await channel_outbox.get(row["id"]))["terminal_at"]
        assert channel_id in await channel_outbox.metric_counts(terminal_at)
        assert channel_id not in await channel_outbox.metric_counts(terminal_at + timedelta(microseconds=1))
    run_db(check, monkeypatch)


def test_outbound_off_and_restart(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.channels import outbound_metrics as metrics
        monkeypatch.setattr(chat_settings, "_outbound_enabled", False)
        monkeypatch.setattr(metrics, "events", metrics.EventBuffer())
        metrics.events.record("channel-lark", metrics.EventKind.BACKOFF)
        async with v1_api(tmp_path, pool) as call:
            assert (await call("manager_metrics.get"))[1]["result"]["outbound"] == {"enabled": False}
            assert await pool.fetchval("SELECT count(*) FROM channel_outbox") == 0
            monkeypatch.setattr(chat_settings, "_outbound_enabled", True)
            monkeypatch.setattr(metrics, "events", metrics.EventBuffer())
            assert (await call("manager_metrics.get"))[1]["result"]["outbound"] == []
    run_db(check, monkeypatch)


def test_gateway_and_outbox_emit_bounded_rolling_events(monkeypatch):
    async def check(pool):
        from looper.agents.channels import outbound_metrics as metrics
        from looper.db import channel_outbox
        now = [0.0]
        events = metrics.EventBuffer(clock=lambda: now[0])
        monkeypatch.setattr(metrics, "events", events)
        gateway = outbound.Gateway("channel-lark", clock=lambda: now[0])
        async def fail():
            raise LarkCliError("limited", code=230020)
        for _ in range(5):
            with pytest.raises(LarkCliError):
                await gateway.write("send", fail, retry=False)
        assert events.counts("channel-lark")[metrics.EventKind.BREAKER_OPEN] == 1
        now[0] = 61
        async def success():
            return "sent"
        await gateway.write("send", success)
        assert gateway.breaker.state == "closed"
        assert events.counts("channel-lark")[metrics.EventKind.BREAKER_RECOVERED] == 1
        # Exercise the durable retry's event site, not just inline gateway retries.
        box = outbound.Outbox("channel-lark", SimpleNamespace(send=lambda **kw: fail()), gateway, channel_outbox)
        row = await channel_outbox.insert_or_get(dict(channel_id="channel-lark", idempotency_key="retry",
            kind="send", chat="synthetic", body={"text": "hi"}, source="answer"))
        await channel_outbox.mark_sending(row["id"])
        await box._deliver(await channel_outbox.get(row["id"]))
        assert events.counts("channel-lark")[metrics.EventKind.BACKOFF] == 1
        assert gateway.counters.backoffs == 1
        assert (await channel_outbox.get(row["id"]))["state"] == "pending"
        attempts = 0
        async def succeeds_on_retry():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await fail()
            return "sent"
        async def no_sleep(_):
            pass
        gateway.sleep = no_sleep
        await gateway.write("edit", succeeds_on_retry)
        assert attempts == 2
        assert events.counts("channel-lark")[metrics.EventKind.BACKOFF] == 2
        for _ in range(10005):
            events.record("channel-lark", metrics.EventKind.BACKOFF)
        assert sum(events.counts("channel-lark").values()) == 10000
        now[0] += 86401
        assert sum(events.counts("channel-lark").values()) == 0
    run_db(check, monkeypatch)


def test_actions_by_category_counts_the_window_through_the_api(monkeypatch, tmp_path):
    async def check(pool):
        await pool.execute("INSERT INTO manager_actions (tool, risk, status, category, receipt, created_at) VALUES "
                           "('looper_projects', 'write', 'ok', 'update', 'a', now()), "
                           "('looper_projects', 'write', 'error', 'update', 'b', now()), "
                           "('looper_schedules', 'write', 'ok', 'control', 'c', now() - interval '3 days'), "
                           "('looper_projects', 'write', 'ok', 'remove', 'd', now() - interval '9 days'), "
                           "('looper_projects', 'write', 'ok', NULL, NULL, now())")  # no category: not counted
        async with v1_api(tmp_path, pool) as call:
            expected = {1: {"update": {"ok": 1, "error": 1}},
                        7: {"update": {"ok": 1, "error": 1}, "control": {"ok": 1}},
                        30: {"update": {"ok": 1, "error": 1}, "control": {"ok": 1}, "remove": {"ok": 1}}}
            for days, by_category in expected.items():
                status, body = await call("manager_metrics.get", {"days": days})
                assert status == 200 and body["result"]["actions"] == {"by_category": by_category}
    run_db(check, monkeypatch)
