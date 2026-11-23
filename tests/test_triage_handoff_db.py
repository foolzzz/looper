"""Important triage events reach the Manager's queue: only the event that owns a message, one row each, never to a
group, and nothing at all while the switch is off. Synthetic messages; a scripted processor; nothing is sent."""

from __future__ import annotations

import json
import uuid

import pytest

from looper.agents import triage_handoff, triage_settings
from looper.db import manager_events as store, projects, tasks, triage_events
from tests.db_support import DSN, run_db
from tests.subscription_support import DM, add, confirm, fire, minutes_ago, scenario, source

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

# The claim as it was at origin/main 6613380fa, before it took a kind.
CLAIM_AT_6613380FA = (
    "UPDATE manager_events SET state = 'running', batch = $4, started_at = now() WHERE id IN ("
    "SELECT id FROM manager_events WHERE manager_id = $1 AND channel_id = $2 AND chat = $3 AND state = 'pending' "
    "ORDER BY id LIMIT $5 FOR UPDATE SKIP LOCKED) AND state = 'pending' RETURNING *")


async def triager(env):
    await source(env.pool)
    await triage_settings.update(True)
    fields = (await env.ok("POST", "/api/v1/schedule_admin/subscription_preset", {"output_kind": "triage"}))["fields"]
    fields.update(cron="*/5 * * * *", timezone="America/Los_Angeles")
    sid = (await env.enabled(**fields))["schedule_id"]
    await confirm(env, sid)
    return sid


async def operator_dm(pool, operator=DM):
    await pool.execute("UPDATE channel_configs SET enabled = true, config = config || $1::jsonb "
                       "WHERE channel_id = 'channel-lark'",
                       json.dumps({"operator_sender_id": operator}))


def event(*sources, category="important", title="Synthetic event", **fields):
    return {"category": category, "title": title, "summary": "A synthetic message", "reason": "It cannot wait",
            "confidence": "high", "sources": list(sources), **fields}


def reply(*events, ignored=()):
    return json.dumps({"events": list(events), "ignored": list(ignored), "instructions_seen": []})


async def triage_rows(pool):
    return [dict(row) for row in await pool.fetch("SELECT * FROM manager_events WHERE kind = 'triage' ORDER BY id")]


def test_only_an_important_event_that_owns_a_message_is_handed_off_once(monkeypatch, tmp_path):
    # E1 owns m1 and m2; E2 is fully covered by E1 (its only message is m2); E3 is a digest that cites m3.
    answer = reply(event("m1", "m2", title="Outage"), event("m2", title="Same outage"),
                   event("m3", category="digest", title="Later"))

    async def check(env, poster, fake):
        await operator_dm(env.pool)
        sid = await triager(env)
        seqs = [await add(env.pool, f"Synthetic update {n}", sender=DM, sent_at=minutes_ago(env, 10 - n))
                for n in range(3)]
        run = await fire(env, sid)
        events = await triage_events.for_run(run["id"], db=env.pool)
        first, covered, digest = events
        assert (first.category, covered.category, digest.category) == ("important", "important", "digest")
        owned = await triage_events.owned_counts(run["id"], db=env.pool)
        assert owned == {first.id: 2, digest.id: 1}
        assert json.loads(run["stream"]["extract_counts"])["important"] == owned[first.id]
        [row] = await triage_rows(env.pool)
        assert (row["state"], row["task_id"], row["target_kind"], row["channel_id"], row["chat"]) == (
            "pending", None, "dm", "channel-lark", DM)
        assert row["manager_id"] == "manager" and row["reason"] is None and row["due_at"] is not None
        assert row["dedupe_key"] == triage_handoff.dedupe_key(first.id)
        assert json.loads(row["payload"]) == {"version": "triage_v1", "event_id": first.id, "run_id": run["id"],
                                              "messages": 2}
        links = {e.id: e.manager_event_id for e in events}
        assert links == {first.id: row["id"], covered.id: None, digest.id: None}
        # A replay of the same run's handoff writes nothing more.
        async with env.pool.acquire() as conn:
            assert await triage_handoff.hand_off(conn, run["id"]) == []
        assert len(await triage_rows(env.pool)) == 1 and poster.sent == []
        assert seqs

    scenario(check, monkeypatch, tmp_path, reply=answer)


def test_the_owner_is_the_highest_category_then_the_earliest_event(monkeypatch):
    async def check(pool):
        await source(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, kind, requirement) "
                           "VALUES ('s', 'p', 'T', '0 * * * *', 'UTC', 'subscription', 'r')")
        run = "run-1"
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, status, trigger, scheduled_for) "
                           "VALUES ($1, 's', 'completed', 'manual', now())", run)
        await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq) VALUES ($1, 0, 0)", run)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        seqs = [await add(pool, f"m{n}", sent_at=now) for n in range(3)]
        await pool.execute("UPDATE subscription_runs SET window_seqs = $2 WHERE run_id = $1", run, seqs)
        ids = {}
        # Inserted in this order: a digest first (lowest id), then two important events.
        for name, category, cited in (("digest", "digest", seqs), ("early", "important", seqs[:2]),
                                      ("late", "important", seqs[1:])):
            ids[name] = await pool.fetchval(
                "INSERT INTO triage_events (source_id, schedule_id, run_id, category, title, reason, reason_kind, "
                "confidence, first_sent_at, last_sent_at) VALUES ('stream', 's', $1, $2, $3, 'r', 'model', 'high', "
                "$4, $4) RETURNING id", run, category, name, now)
            await pool.executemany("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)",
                                   [(ids[name], seq) for seq in cited])
        # An event of another run citing the same messages never counts here, and context outside the window owns nothing.
        owned = await triage_events.owned_counts(run, db=pool)
        assert owned == {ids["early"]: 2, ids["late"]: 1}
        assert await triage_events.owned_counts("no-such-run", db=pool) == {}

    run_db(check, monkeypatch)


def test_without_a_direct_chat_the_event_goes_to_the_dashboard_chat_else_falls_back(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        # No bot names its operator and no dashboard chat exists: nothing can be told, the event stays for the brief.
        await add(env.pool, "First synthetic update", sender=DM, sent_at=minutes_ago(env, 9))
        run = await fire(env, sid)
        [row] = await triage_rows(env.pool)
        assert (row["state"], row["reason"], row["delivery"], row["target_kind"]) == ("fallback", "no_chat", None, "web")
        assert row["finished_at"] is not None and row["due_at"] is None
        [stored] = await triage_events.for_run(run["id"], db=env.pool)
        assert stored.manager_event_id == row["id"]
        # An operator's dashboard chat takes it; an API client's chat does not.
        for session, client in (("web-client", "client-1"), ("web-operator", None)):
            if client:
                await env.pool.execute("INSERT INTO api_clients (id, name, token_sha256) VALUES ($1, 'c', 'h')", client)
                await env.pool.execute("INSERT INTO chat_threads (session_id, manager_id, client_id) "
                                       "VALUES ($1, 'manager', $2)", session, client)
            await env.pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel) "
                                   "VALUES ($1, 'm1', 'user', 'hello', 'web')", session)
        await add(env.pool, "Second synthetic update", sender=DM, sent_at=minutes_ago(env, 2))
        await env.pool.execute("UPDATE chat_messages SET created_at = now() + interval '1 minute' "
                               "WHERE session_id = 'web-client'")
        await fire(env, sid)
        second = (await triage_rows(env.pool))[-1]
        assert (second["state"], second["target_kind"], second["channel_id"], second["chat"],
                second["session_key"]) == ("pending", "web", "channel-web", "web-operator", "web-operator")
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply=reply(event("m1")))


def test_a_group_message_is_told_to_the_operator_chat_never_to_a_group(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await operator_dm(env.pool)
        sid = await triager(env)
        await add(env.pool, "Group chatter that mentions the operator", chat_id="oc_group", kind="group",
                  mentions_me=True, sent_at=minutes_ago(env, 5))
        await fire(env, sid)
        [row] = await triage_rows(env.pool)
        assert (row["target_kind"], row["chat"]) == ("dm", DM) and row["chat"] != "oc_group"
        # The table refuses a waiting triage event aimed at a group.
        with pytest.raises(Exception, match="manager_events_turn_target"):
            await env.pool.execute(
                "INSERT INTO manager_events (manager_id, kind, origin, target_kind, channel_id, chat, dedupe_key, "
                "payload, fallback, state, due_at) VALUES ('manager', 'triage', 'chat', 'group', 'channel-lark', "
                "'oc_group', 'x', '{}', '[]', 'pending', now())")

    scenario(check, monkeypatch, tmp_path, reply=reply(event("m1")))


def test_with_the_switch_off_or_nothing_important_no_triage_row_is_written(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await operator_dm(env.pool)
        sid = await triager(env)
        await add(env.pool, "A digest only", sender=DM, sent_at=minutes_ago(env, 5))
        await fire(env, sid)
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 1
        assert await env.pool.fetchval("SELECT count(*) FROM manager_events") == 0
        await triage_settings.update(False)
        await add(env.pool, "Ignored: the switch is off", sender=DM, sent_at=minutes_ago(env, 1))
        run = await fire(env, sid)
        assert run["status"] == "skipped_gate"
        assert await env.pool.fetchval("SELECT count(*) FROM manager_events") == 0

    scenario(check, monkeypatch, tmp_path, reply=reply(event("m1", category="digest")))


def test_claim_takes_one_kind_and_without_a_kind_is_what_it_was(monkeypatch, tmp_path):
    async def record(pool, key, *, kind, task=None):
        if kind == "task_end":
            project = await projects.create_project(name=f"p-{task}", work_dir=str(tmp_path), pool=pool)
            await tasks.create_task(task_id=task, requirement="r", project_id=project["id"], work_dir=str(tmp_path),
                                    status="completed", pool=pool)
        return (await store.record(
            manager_id="manager", task_id=task, origin="chat", target_kind="dm", channel_id="channel-lark",
            chat=DM, dedupe_key=key, payload={}, fallback=[], state="pending", reason=None, kind=kind,
            pool=pool)).row

    async def check(pool):
        end = await record(pool, "end-1", kind="task_end", task="t1")
        first = await record(pool, "triage-1", kind="triage")
        second = await record(pool, "triage-2", kind="triage")
        batch_end, batch_triage = uuid.uuid4(), uuid.uuid4()
        taken = await store.claim("manager", "channel-lark", DM, batch_end, pool=pool)
        assert [row["id"] for row in taken] == [end["id"]]
        triaged = await store.claim("manager", "channel-lark", DM, batch_triage, kind="triage", pool=pool)
        assert [row["id"] for row in triaged] == [first["id"], second["id"]]
        assert {row["batch"] for row in triaged} == {batch_triage} and taken[0]["batch"] == batch_end
        assert await store.claim("manager", "channel-lark", DM, uuid.uuid4(), kind="triage", pool=pool) == []
        with pytest.raises(ValueError):
            await store.claim("manager", "channel-lark", DM, uuid.uuid4(), kind="bogus", pool=pool)

        # Without triage rows the claim returns exactly what the claim at 6613380fa returned.
        await pool.execute("DELETE FROM manager_events WHERE kind = 'triage'")
        await pool.execute("UPDATE manager_events SET state = 'pending', batch = NULL, started_at = NULL")
        await record(pool, "end-2", kind="task_end", task="t2")
        await pool.execute("UPDATE manager_events SET created_at = '2026-01-01T00:00:00Z'")
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            old = await conn.fetch(CLAIM_AT_6613380FA, "manager", "channel-lark", DM, batch_end, 10)
            await tx.rollback()
        new = await store.claim("manager", "channel-lark", DM, batch_end, pool=pool)
        def stable(rows):
            return [{key: value for key, value in dict(row).items() if key != "started_at"} for row in rows]

        assert stable(old) == stable(new) and len(new) == 2

    run_db(check, monkeypatch)


def test_an_old_dashboard_chat_is_found_though_other_channels_have_many_newer_sessions(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        await env.pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at) "
                               "VALUES ('web-old', 'm1', 'user', 'hello', 'web', now() - interval '3 days')")
        for number in range(30):  # newer chats on a bot, an API client's thread and a Planner thread
            session = f"lark-session-{number}"
            await env.pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel) "
                                   "VALUES ($1, 'm1', 'user', 'hi', 'lark')", session)
        await env.pool.execute("INSERT INTO chat_threads (session_id, manager_id, client_id) VALUES ('web-api', "
                               "'manager', 'client-x')")
        await env.pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel) VALUES "
                               "('web-api', 'm1', 'user', 'hi', 'web'), ('planner-1', 'm1', 'user', 'hi', 'web')")
        await add(env.pool, "An urgent synthetic update", sender=DM, sent_at=minutes_ago(env, 5))
        await fire(env, sid)
        [row] = await triage_rows(env.pool)
        assert (row["state"], row["target_kind"], row["chat"]) == ("pending", "web", "web-old")

    scenario(check, monkeypatch, tmp_path, reply=reply(event("m1")))
