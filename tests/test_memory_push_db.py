"""At-most-once memory delivery with disposable PostgreSQL and synthetic channels."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from looper.agents import memory_push as push, memory_write
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.db import memory as store
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime.now(timezone.utc)
CHANNEL, DM = "channel-lark", "ou_memory_synthetic"


class Channels:
    def __init__(self):
        self.posts, self.edits = [], []
        self.outcome, self.edit_ok, self.wait = PostOutcome.SENT, True, None

    async def post(self, channel, target, text, **kwargs):
        self.posts.append((channel, target, text, kwargs))
        if self.wait is not None:
            await self.wait.wait()
        return PostReceipt(self.outcome is PostOutcome.SENT, (f"om_memory_{len(self.posts)}",)
                           if self.outcome is PostOutcome.SENT else (), outcome=self.outcome)

    async def edit(self, channel, target, external, text):
        self.edits.append((channel, target, external, text))
        return PostReceipt(self.edit_ok)


async def setup(pool, tmp_path, *, enabled=True, target=True):
    repo = sup.synthetic_vault(tmp_path)
    settings = await sup.configure_write(repo, pool)
    await pool.execute("INSERT INTO channel_configs(channel_id,channel_type,display_name,enabled,config) "
        "VALUES($1,'lark','Synthetic memory bot',true,$2::jsonb)", CHANNEL, json.dumps({"operator_sender_id": DM}))
    raw = settings.to_json() | {"enabled": enabled, "push_channel": CHANNEL if target else None}
    await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
    channels = Channels()
    service = push.MemoryPushService(channels, clock=lambda: NOW)
    cid = await sup.candidate(pool)
    await sup.revise_current(pool, cid, settings, note_path=sup.NOTE["path"])
    return SimpleNamespace(repo=repo, channels=channels, service=service, cid=cid, settings=settings)


@pytest.mark.parametrize("enabled,target", [(False, True), (True, False)])
def test_off_is_fixed_empty_baseline(monkeypatch, tmp_path, enabled, target):
    """Parent 94bfd7e7c has no memory delivery: zero rows/calls, even with a pending candidate."""
    async def check(pool):
        env = await setup(pool, tmp_path, enabled=enabled, target=target)
        await env.service.run_once()
        await push.defer_result(env.cid, channel_id=CHANNEL, chat=DM)
        assert await pool.fetchval("SELECT count(*) FROM memory_posts") == 0
        assert env.channels.posts == env.channels.edits == []
    run_db(check, monkeypatch)


def test_one_push_per_revision_and_updated_quote(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        await env.service.run_once()
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        row = await pool.fetchrow("SELECT * FROM memory_posts")
        assert row["state"] == "sent" and row["complete"]
        async with store.transaction(pool) as conn:
            await store.add_revision(conn, env.cid, action="edit", actor="user", via="dashboard", made_by="operator",
                changes=[sup.NOTE, {**sup.INDEX, "line": "- [[weekly]] updated"}], title="Updated",
                subject="mem: Updated", diff_stats={}, note_path=sup.NOTE["path"], pending_days=7, now=NOW)
        await env.service.run_once()
        assert len(env.channels.posts) == 2
        assert env.channels.posts[-1][3]["reply_to"] == "om_memory_1"
        assert env.channels.edits[-1][3] == f"🔄 M-{env.cid:04d} 已更新，请看新消息"
    run_db(check, monkeypatch)


def test_unknown_and_crashed_claims_never_resend(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        env.channels.outcome = PostOutcome.UNKNOWN
        await env.service.run_once()
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        assert await pool.fetchval("SELECT state FROM memory_posts") == "uncertain"
        await pool.execute("UPDATE memory_posts SET state='sending'")
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        assert await pool.fetchval("SELECT state FROM memory_posts") == "uncertain"
    run_db(check, monkeypatch)


def test_two_service_instances_hold_session_lock_during_send(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        env.channels.wait = asyncio.Event()
        first = asyncio.create_task(env.service.run_once())
        while not env.channels.posts:
            await asyncio.sleep(0)
        await push.MemoryPushService(env.channels, clock=lambda: NOW).run_once()
        env.channels.wait.set()
        await first
        assert len(env.channels.posts) == 1
    run_db(check, monkeypatch, max_size=5)


@pytest.mark.parametrize("state,edit_ok", [("written", True), ("blocked", True), ("conflict", True),
                                         ("rejected", True), ("expired", True), ("rejected", False)])
def test_final_edits_and_quoted_fallback(monkeypatch, tmp_path, state, edit_ok):
    async def check(pool):
        env = await setup(pool, tmp_path)
        await env.service.run_once()
        env.channels.edit_ok = edit_ok
        row = (await store.get(env.cid, db=pool))["candidate"]
        if state in ("written", "blocked", "conflict"):
            await sup.move(pool, env.cid, "confirmed", "confirm", actor="user", via="dashboard",
                           content_hash=row["content_hash"])
            await sup.move(pool, env.cid, "writing", "claim")
        kwargs = {"reason": "dirty_target"} if state == "blocked" else (
            {"reason": "base_changed"} if state == "conflict" else {"commit_sha": "a" * 40} if state == "written" else {})
        await sup.move(pool, env.cid, state, {"written": "write", "blocked": "block", "conflict": "conflict",
                       "rejected": "reject", "expired": "expire"}[state],
                       actor="user" if state == "rejected" else "system", **kwargs)
        await env.service.run_once()
        await env.service.run_once()
        assert len(env.channels.edits) == 1
        assert env.channels.edits[0][3] == push.final_text((await store.get(env.cid, db=pool))["candidate"])
        assert len(env.channels.posts) == (1 if edit_ok else 2)
        if not edit_ok:
            assert env.channels.posts[-1][3]["reply_to"] == "om_memory_1"
    run_db(check, monkeypatch)


def test_deferred_result_is_durable_and_sent_after_write(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        await env.service.run_once()
        row = (await store.get(env.cid, db=pool))["candidate"]
        await sup.move(pool, env.cid, "confirmed", "confirm", actor="user", via="dashboard", content_hash=row["content_hash"])
        await push.defer_result(env.cid, channel_id=CHANNEL, chat=DM)
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        assert await memory_write.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        await push.MemoryPushService(env.channels, clock=lambda: NOW).run_once()
        assert len(env.channels.posts) == 2 and env.channels.posts[-1][2].startswith("✅")
        assert await pool.fetchval("SELECT state FROM memory_posts WHERE kind='final'") == "sent"
    run_db(check, monkeypatch, timeout=40)


def test_alert_thresholds_are_combined_once_per_day(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        async def end(cid, *, blocked=True):
            row = (await store.get(cid, db=pool))["candidate"]
            await sup.move(pool, cid, "confirmed", "confirm", actor="user", via="dashboard", content_hash=row["content_hash"])
            await sup.move(pool, cid, "writing", "claim")
            await sup.move(pool, cid, "blocked" if blocked else "written", "block" if blocked else "write",
                           **({"reason": "dirty_target", "note": "dirty_target"} if blocked else {"commit_sha": "a" * 40}))
        await end(env.cid)
        for index in range(4):
            cid = await sup.candidate(pool, statement=f"Synthetic memory {index}")
            await sup.revise_current(pool, cid, env.settings, changes=[{**sup.INDEX, "line": f"- synthetic {index}"}])
            await end(cid)
            await env.service.run_once()
        assert len([p for p in env.channels.posts if p[2].startswith("告警")]) == 1
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_alert_sent:blocked'") == NOW.date().isoformat()
    run_db(check, monkeypatch)


def test_deferred_registration_does_not_wait_for_a_slow_channel(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        env.channels.wait = asyncio.Event()
        sending = asyncio.create_task(env.service.run_once())
        while not env.channels.posts:
            await asyncio.sleep(0)
        await asyncio.wait_for(push.defer_result(env.cid, channel_id=CHANNEL, chat=DM), .5)
        assert await pool.fetchval("SELECT count(*) FROM memory_posts WHERE kind='final'") == 1
        env.channels.wait.set()
        await sending
    run_db(check, monkeypatch, max_size=5)


def test_crashed_sending_post_is_recovered_after_dashboard_rejection(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        await env.service.run_once()
        await pool.execute("UPDATE memory_posts SET state='sending',external_id=NULL")
        await sup.move(pool, env.cid, 'rejected', 'reject', actor='user')
        await env.service.run_once()
        assert await pool.fetchval('SELECT state FROM memory_posts') == 'uncertain'
        assert len(env.channels.posts) == 1
    run_db(check, monkeypatch)


def test_repeated_reason_alert_counts_writes_between_blocks(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        # A block followed by a successful write never forms the consecutive-failure threshold.
        for index in range(5):
            async with store.transaction(pool) as conn:
                await store._event(conn, env.cid, sup.Action.BLOCK, sup.Actor.SYSTEM, sup.Via.SYSTEM,
                                   note='dirty_target')
                await store._event(conn, env.cid, sup.Action.WRITE, sup.Actor.SYSTEM, sup.Via.SYSTEM)
            await env.service.run_once()
            assert len([p for p in env.channels.posts if p[2].startswith('告警')]) == int(index == 4)
        await env.service.run_once()
        assert len([p for p in env.channels.posts if p[2].startswith('告警')]) == 1
        assert '目标笔记有未提交的修改' in env.channels.posts[-1][2]
    run_db(check, monkeypatch)


def test_another_process_cannot_send_an_in_flight_post(monkeypatch, tmp_path):
    import sys

    async def check(pool):
        env = await setup(pool, tmp_path)
        env.channels.wait = asyncio.Event()
        sending = asyncio.create_task(env.service.run_once())
        while not env.channels.posts:
            await asyncio.sleep(0)
        schema = await pool.fetchval('SELECT current_schema()')
        marker = tmp_path / 'duplicate-send'
        program = '''
import asyncio, sys
from pathlib import Path
import asyncpg
from looper.db import connection
from looper.agents.memory_push import MemoryPushService
from looper.agents.channels.base import PostReceipt
class StandIn:
    async def post(self, *args, **kwargs):
        Path(sys.argv[3]).write_text('unexpected send')
        return PostReceipt(True, ('om_competing_process',))
async def run():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=3,
                                    server_settings={'search_path': sys.argv[2]})
    connection._pool = pool
    try:
        await MemoryPushService(StandIn()).run_once()
    finally:
        await pool.close()
asyncio.run(run())
'''
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', program, DSN, schema, str(marker),
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
            assert child.returncode == 0, stderr.decode()
            assert not marker.exists()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            env.channels.wait.set()
            await sending
        assert len(env.channels.posts) == 1
    run_db(check, monkeypatch, max_size=5, timeout=30)


def test_reserved_defer_and_settle_do_not_wait_for_the_writer_row_lock(monkeypatch, tmp_path):
    async def check(pool):
        env = await setup(pool, tmp_path)
        async with store.transaction(pool) as conn:
            row = await conn.fetchrow('SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE', env.cid)
            defer_id = await push.reserve_result(conn, row, channel_id=CHANNEL, chat=DM)
            settle_id = await push.reserve_result(conn, row, channel_id=CHANNEL, chat=DM)
        assert defer_id is not None and settle_id is not None
        # This is the lock the real writer holds while its git work runs.
        async with pool.acquire() as writer_conn, writer_conn.transaction():
            await writer_conn.fetchrow('SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE', env.cid)
            await asyncio.wait_for(push.settle_result(settle_id), .5)
            await asyncio.wait_for(push.defer_result(env.cid, channel_id=CHANNEL, chat=DM), .5)
            assert await pool.fetchval('SELECT error FROM memory_posts WHERE id=$1', defer_id) is None
            assert await pool.fetchval('SELECT count(*) FROM memory_posts WHERE id=$1', settle_id) == 0
    run_db(check, monkeypatch, max_size=5, timeout=30)


@pytest.mark.parametrize('inline,edit_ok', [(True, True), (False, True), (True, False)])
def test_reserved_result_waits_for_reply_or_timeout_and_preserves_fallback(monkeypatch, tmp_path, inline, edit_ok):
    async def check(pool):
        env = await setup(pool, tmp_path)
        env.service._clock = lambda: datetime.now(timezone.utc)
        await env.service.run_once()
        async with store.transaction(pool) as conn:
            row = await conn.fetchrow('SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE', env.cid)
            await store.transition(conn, env.cid, 'confirmed', action='confirm', actor='user', via='dashboard',
                                   now=datetime.now(timezone.utc), content_hash=row['content_hash'])
            reservation = await push.reserve_result(conn, row, channel_id=CHANNEL, chat=DM)
        await sup.move(pool, env.cid, 'writing', 'claim')
        await sup.move(pool, env.cid, 'written', 'write', commit_sha='a' * 40)
        env.channels.edit_ok = edit_ok
        await env.service.run_once()
        assert len(env.channels.posts) == (1 if edit_ok else 2)
        if inline:
            await push.settle_result(reservation)
            await env.service.run_once()
            assert len(env.channels.posts) == (1 if edit_ok else 2)
        else:
            env.service._clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=21)
            await env.service.run_once()
            assert len(env.channels.posts) == 2
        if not edit_ok:
            assert env.channels.posts[-1][3]['reply_to'] == 'om_memory_1'
    run_db(check, monkeypatch)
