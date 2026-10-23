"""The scheduler tests' environment on a real PostgreSQL: a project, a scheduler on a clock the test sets, a bus that
records what it would send the TaskRouter, the API as the dashboard calls it, and a stand-in for the channels."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.db_support import run_db

UTC = timezone.utc
# Noon in Los Angeles, where the scheduler's day (``run_scheduled``, ``with_sender``) starts at midnight: far from a
# day's end.
NOON = datetime(2030, 1, 15, 20, 0, tzinfo=UTC)
TELEGRAM_CHAT = "1000001"  # a stand-in id in the test database's allowlist; nothing is sent anywhere


class Bus:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.sent.append(envelope)

    def of(self, kind: str) -> list[str]:
        return [e.payload["task_id"] for e in self.sent if e.message_type.value == kind]


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class ScheduleEnv(SimpleNamespace):
    pool: Any
    bus: Bus
    clock: Clock
    scheduler: Any
    project_id: str
    work_dir: str

    async def call(self, method: str, path: str, body: Any = None, query: dict[str, str] | None = None,
                   ) -> tuple[int, Any]:
        from looper.agents.http_server import AgentHTTPServer
        from looper.agents.tiers.task import TaskRouter

        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())),
                                 scheduler=self.scheduler)
        writer = _Writer()
        raw = b"" if body is None else json.dumps(body).encode()
        await server._route_api(method, path, query or {}, raw, writer)
        head, _, payload = writer.data.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    async def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, payload = await self.call(method, path, body)
        assert status in (200, 201), (status, payload)
        return payload

    async def schedule(self, **fields: Any) -> dict[str, Any]:
        body = {"name": f"s-{uuid.uuid4().hex[:6]}", "cron": "*/5 * * * *", "timezone": "America/Los_Angeles",
                "requirement": "Look at the new alerts.", **fields}
        return (await self.ok("POST", f"/api/projects/{self.project_id}/schedules", body))["schedule"]

    async def enabled(self, **fields: Any) -> dict[str, Any]:
        schedule = await self.schedule(**fields)
        return (await self.ok("PUT", f"/api/schedules/{schedule['schedule_id']}", {"enabled": True}))["schedule"]

    async def due(self, schedule_id: str, *, fires_ago: int = 0, step: timedelta = timedelta(minutes=5)) -> None:
        """Make the schedule's next fire ``fires_ago`` fire times in the past, and the clock 10 s after the latest."""
        latest = (self.clock.now + step).replace(second=0, microsecond=0)
        latest -= timedelta(minutes=latest.minute % 5)
        await self.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", schedule_id,
                                latest - fires_ago * step)
        self.clock.now = latest + timedelta(seconds=10)

    async def runs(self, schedule_id: str) -> list[dict[str, Any]]:
        return (await self.ok("GET", f"/api/schedules/{schedule_id}/runs"))["runs"]

    async def end_task(self, task_id: str, status: str, *, summary: str = "", cost: float = 0.0) -> None:
        await self.pool.execute(
            "UPDATE tasks SET status = $2, result_summary = CASE WHEN $2 = 'completed' THEN $3 END, "
            "error = CASE WHEN $2 = 'failed' THEN $3 END WHERE id = $1", task_id, status, summary)
        if cost:
            await self.pool.execute(
                "INSERT INTO llm_records (agent_id, task_id, input_text, output_text, status, cost_usd) "
                "VALUES ($1, $2, '', '', 'completed', $3)", f"planner-{task_id}", task_id, cost)
        await self.scheduler.task_ended(task_id, status)


class Sender:
    """Stands in for the server's channels: records each message instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail_with: str | None = None

    async def __call__(self, channel: str, chat: str, text: str) -> tuple[bool, str | None]:
        if self.fail_with:
            return False, self.fail_with
        self.sent.append((channel, chat, text))
        return True, None


def run_scheduled(test: Callable[[ScheduleEnv], Awaitable[None]], monkeypatch: pytest.MonkeyPatch,
                  tmp_path: Path) -> None:
    """Run ``test(env)`` on a new, migrated schema that is also the server's, with a project in ``tmp_path``."""
    from looper.agents import scheduler_settings
    from looper.agents.scheduler import Scheduler
    from looper.db import projects

    async def with_env(pool: Any) -> None:
        monkeypatch.setattr(scheduler_settings, "_min_interval", scheduler_settings.DEFAULT_MIN_INTERVAL_SECONDS)
        project = await projects.create_project(name="alerts", work_dir=str(tmp_path), pool=pool)
        bus, clock = Bus(), Clock()
        await test(ScheduleEnv(pool=pool, bus=bus, clock=clock,
                               scheduler=Scheduler(bus=bus, clock=clock, day_zone="America/Los_Angeles"),
                               project_id=project["id"], work_dir=str(tmp_path)))

    run_db(with_env, monkeypatch, max_size=4)


def with_sender(env: ScheduleEnv, *, day_zone: str = "America/Los_Angeles", dashboard_url: str = "") -> Sender:
    from looper.agents.scheduler import Scheduler

    sender = Sender()
    env.scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=sender, day_zone=day_zone,
                              dashboard_url=dashboard_url)
    return sender


async def allow_telegram(env: ScheduleEnv) -> None:
    from looper.agents.serve import seed_channel_configs

    await seed_channel_configs(env.pool)
    await env.pool.execute(
        "UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-telegram'",
        f'{{"allowed_user_ids": ["{TELEGRAM_CHAT}"]}}')
