"""What the subscription tests share: a message source with synthetic events, a push target the channels know, a poster
that sends one message per idempotency key as Lark does, and a processor that answers as the test says."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from looper.agents import processor, subscription_runs
from looper.agents.agent_settings import AgentType
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.scheduler import Scheduler
from looper.db import chat_bindings
from tests.schedule_support import ScheduleEnv, run_scheduled

UTC = timezone.utc
UPDATE = "/api/v1/schedule_admin/update"
SOURCE = "stream"
DM = "ou_operatorsynthetic"  # the operator's direct chat: an allowlisted sender of the built-in Lark channel
GROUP = "oc_pushgroupsynthetic"  # a group the Lark channel is bound to
UNBOUND_GROUP = "oc_unboundsynthetic"
INSTRUCTIONS = "Pick the funny things people said. Reply SKIP when there is nothing."
SUBSCRIPTION = {"source_id": SOURCE, "chat_kinds": ["group", "topic"], "sender_kinds": ["user", "app"],
                "exclude_mentions_me": True, "max_age_minutes": 180, "max_events": 500, "text_max_chars": 200,
                "quiet_word": "SKIP", "output_header": "🍉 吃瓜速报 · 过去1小时"}


class Poster:
    """The channels' ``post``: one message per idempotency key (a repeated key sends nothing and answers as before),
    optionally failing or breaking for a test."""

    def __init__(self, *, dedupe: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.calls = 0
        self.dedupe = dedupe  # False: a channel whose key has expired, or that ignores it (Telegram)
        self.fail_with: str | None = None
        self.fail_segments: dict[int, int] = {}  # the nth call that fails with "refused", n -> how many more times
        self.break_after: int | None = None  # raise after this many messages were sent: a crash in the push

    async def __call__(self, channel_id: str, target: Any, text: str, *, reply_to: str | None = None,
                       idempotency_key: str | None = None, proactive: Any = None) -> PostReceipt:
        self.calls += 1
        if self.fail_with:
            return PostReceipt(False, error=self.fail_with, outcome=PostOutcome.REJECTED)
        if self.fail_segments.get(self.calls):
            return PostReceipt(False, error="refused", outcome=PostOutcome.REJECTED)
        if self.dedupe and idempotency_key is not None and any(m["key"] == idempotency_key for m in self.sent):
            return PostReceipt(True, external_ids=("om_again",))
        self.sent.append({"channel_id": channel_id, "target": target, "text": text, "key": idempotency_key})
        if self.break_after is not None and len(self.sent) >= self.break_after:
            raise RuntimeError("the process died after the push")
        return PostReceipt(True, external_ids=(f"om_{len(self.sent)}",))


class FakeProcessor:
    """``processor.run`` as the test scripts it: records what it was given, answers with ``reply`` or fails. A run of
    the leads check (``role``) answers with ``check_reply`` (or fails with ``check_failure``) instead, as record
    ``check_record_id``."""

    def __init__(self, reply: str = "- 《Ops room》: someone spilled coffee") -> None:
        self.reply: str | None = reply
        self.failure: processor.ProcessorFailure | None = None
        self.cost = 0.01
        self.calls: list[dict[str, Any]] = []
        self.during: Any = None  # an async function called while the processor "runs"
        self.check_reply: str | None = None
        self.check_failure: processor.ProcessorFailure | None = None
        self.check_cost = 0.0003
        self.check_record_id: int | None = None
        # Per call of the processor's own role, in order: (reply, failure) answered before ``reply``/``failure``.
        self.outcomes: list[tuple[str | None, processor.ProcessorFailure | None]] = []

    async def __call__(self, **kwargs: Any) -> processor.ProcessorRun:
        self.calls.append(kwargs)
        if kwargs.get("role", AgentType.PROCESSOR) is not AgentType.PROCESSOR:
            if self.check_failure is not None:
                return processor.ProcessorRun(None, self.check_record_id, self.check_cost, failure=self.check_failure,
                                              detail="synthetic failure")
            return processor.ProcessorRun(self.check_reply, self.check_record_id, self.check_cost)
        if self.during is not None:
            await self.during()
        if self.outcomes:
            reply, failure = self.outcomes.pop(0)
            if failure is not None:
                return processor.ProcessorRun(None, None, 0.0, failure=failure, detail="synthetic failure")
            return processor.ProcessorRun(reply, None, self.cost)
        if self.failure is not None:
            return processor.ProcessorRun(None, None, self.cost, failure=self.failure, detail="synthetic failure")
        return processor.ProcessorRun(self.reply, None, self.cost)


def processor_says(monkeypatch: pytest.MonkeyPatch, reply: str = "- 《Ops room》: someone spilled coffee") -> FakeProcessor:
    fake = FakeProcessor(reply)
    monkeypatch.setattr(processor, "run", fake)
    return fake


async def lark_targets(pool: Any) -> None:
    """The built-in Lark channel with the operator's direct chat on its allowlist and a bound group."""
    from looper.agents.serve import seed_channel_configs

    await seed_channel_configs(pool)
    await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-lark'",
                       json.dumps({"allowed_open_ids": [DM]}))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await chat_bindings.put_binding("channel-lark", GROUP, "manager", conn=conn)


async def source(pool: Any, source_id: str = SOURCE) -> None:
    # Bound to the operator: the user whose own direct chat is DM.
    await pool.execute("INSERT INTO event_sources (id, kind, name, user_open_id) VALUES ($1, 'lark_user', 'Stream', $2) "
                       "ON CONFLICT DO NOTHING", source_id, DM)


async def chat(pool: Any, chat_id: str, *, kind: str = "group", name: str = "Ops room", source_id: str = SOURCE) -> None:
    await pool.execute("INSERT INTO source_chats (source_id, chat_id, kind, name) VALUES ($1, $2, $3, $4) "
                       "ON CONFLICT DO NOTHING", source_id, chat_id, kind, name)


async def person(pool: Any, open_id: str, name: str, source_id: str = SOURCE) -> None:
    await pool.execute("INSERT INTO source_people (source_id, open_id, name, name_source) VALUES ($1, $2, $3, 'contact') "
                       "ON CONFLICT DO NOTHING", source_id, open_id, name)


async def add(pool: Any, text: str, *, chat_id: str = "oc_ops", kind: str = "group", sent_at: datetime,
              sender: str = "ou_alice", sender_kind: str = "user", mentions_me: bool = False, origin: str = "live",
              source_id: str = SOURCE, commit: bool = True, ingested_at: datetime | None = None,
              chat_name: str = "Ops room") -> int:
    """A stored event; the source's committed sequence follows it unless ``commit`` is false."""
    await chat(pool, chat_id, kind=kind, name=chat_name, source_id=source_id)
    seq = await pool.fetchval(
        "INSERT INTO source_events (source_id, external_id, chat_id, sender_id, sender_kind, sent_at, msg_type, "
        "mentions_me, text, body_bytes, origin, ingested_at) VALUES ($1, $2, $3, $4, $5, $6, 'text', $7, $8, 1, $9, "
        "COALESCE($10, now())) RETURNING seq",
        source_id, f"om_{uuid.uuid4().hex[:10]}", chat_id, sender, sender_kind, sent_at, mentions_me, text, origin,
        ingested_at)
    if commit:
        await pool.execute("UPDATE event_sources SET committed_seq = GREATEST(committed_seq, $2) WHERE id = $1",
                           source_id, seq)
    return seq


def scheduler_with(env: Any, *, poster: Poster | None = None, sender: Any = None, own_apps: Any = None) -> Scheduler:
    """A scheduler on the test's clock that pushes through ``poster`` (the env's replaces it)."""
    scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=sender, day_zone="America/Los_Angeles",
                          runtime_root=env.runtime_root, poster=poster, own_apps=own_apps)
    scheduler._commands.prepare()
    env.scheduler = scheduler
    return scheduler


def minutes_ago(env: Any, minutes: float) -> datetime:
    return env.clock.now - timedelta(minutes=minutes)


async def started(env: ScheduleEnv, *, target: Any = {"channel": "lark", "chat": DM}, subscription: dict | None = None,
                  enable: bool = True, **fields: Any) -> str:
    """A subscription on the stream (its cursor at the source's end), enabled; its schedule id."""
    await source(env.pool)
    body = {"kind": "subscription", "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "notify": "failures",
            "subscription": {**SUBSCRIPTION, **(subscription or {})}, **({"notify_target": target} if target else {}),
            **fields}
    sid = (await env.schedule(**body))["schedule_id"]
    if enable:
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"enabled": True}})
    return sid


async def version_of(env: ScheduleEnv, sid: str) -> str:
    view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
    return view["subscription"]["config_version"]


async def confirm(env: ScheduleEnv, sid: str) -> None:
    """The operator confirms what the schedule's view shows."""
    await env.ok("POST", "/api/v1/schedule_admin/confirm_target",
                 {"schedule_id": sid, "config_version": await version_of(env, sid)})


async def fire(env: ScheduleEnv, sid: str) -> dict[str, Any]:
    """Make the schedule due, run one scheduler pass and the run it started; the run's row and its subscription's."""
    await env.due(sid)
    await env.scheduler.tick()
    await env.scheduler.idle()
    return await latest(env, sid)


async def latest(env: ScheduleEnv, sid: str) -> dict[str, Any]:
    row = dict(await env.pool.fetchrow("SELECT * FROM schedule_runs WHERE schedule_id = $1 "
                                       "ORDER BY created_at DESC, id DESC LIMIT 1", sid))
    stream = await env.pool.fetchrow("SELECT * FROM subscription_runs WHERE run_id = $1", row["id"])
    return {**row, "stream": dict(stream) if stream else None}


async def cursor(env: ScheduleEnv, sid: str) -> int:
    return await env.pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id = $1", sid)


async def confirmed_at(env: ScheduleEnv, sid: str) -> Any:
    return await env.pool.fetchval("SELECT target_confirmed_at FROM subscriptions WHERE schedule_id = $1", sid)


def scenario(test, monkeypatch, tmp_path, *, reply: str = "- 《Ops room》: someone spilled coffee"):
    """Run ``test(env, poster, fake)`` with the Lark targets, a scheduler that pushes through ``poster`` and a
    processor that answers ``reply``."""
    fake = processor_says(monkeypatch, reply)
    monkeypatch.setattr(subscription_runs, "RETRY_DELAY_SECONDS", 0)

    async def body(env: ScheduleEnv) -> None:
        await lark_targets(env.pool)
        poster = Poster()
        scheduler_with(env, poster=poster)
        await test(env, poster, fake)

    run_scheduled(body, monkeypatch, tmp_path)
