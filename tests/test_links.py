"""Links to the dashboard use its configured public URL, else the server's own address."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import links

SCHEDULE = {"id": "sched-1", "project_id": "proj-1", "name": "alerts", "timezone": "UTC", "consecutive_failures": 3,
            "last_ok_at": None}
RUN = {"status": "failed", "scheduled_for": datetime(2026, 10, 15, 9, tzinfo=timezone.utc), "fired_at": None,
       "finished_at": None, "summary": "boom", "error": None, "cost_usd": 0.05, "task_id": "task-1"}


@pytest.mark.parametrize(("host", "port", "url"), [
    ("127.0.0.1", 8767, "http://127.0.0.1:8767"),
    ("0.0.0.0", 8800, "http://127.0.0.1:8800"),  # bound to every address: the machine's own
    ("::1", 8800, "http://[::1]:8800"),
    ("looper.lan", 80, "http://looper.lan:80"),
])
def test_without_a_public_url_links_go_to_the_server(host: str, port: int, url: str) -> None:
    assert links.dashboard_url(host=host, port=port) == url


@pytest.mark.parametrize(("raw", "url"), [
    ("https://looper.example.com/", "https://looper.example.com"),
    ("http://10.0.0.5:8767", "http://10.0.0.5:8767"),
    ("https://example.com/looper/", "https://example.com/looper"),  # behind a proxy, under a path
])
def test_a_public_url_is_used_as_given(raw: str, url: str) -> None:
    assert links.dashboard_url(host="127.0.0.1", port=8767, public_url=raw) == url
    assert links.task_link(url, "task-1") == f"{url}/#tasks/task-1"


def test_serve_does_not_start_with_a_malformed_public_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncpg

    from looper.agents import serve

    connect = AsyncMock(side_effect=AssertionError("the database must not be contacted"))
    create_pool = AsyncMock(side_effect=AssertionError("the database pool must not be opened"))
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(serve, "_configure_logging", lambda level: None)  # leaves the process's logging alone
    monkeypatch.setenv("LOOPER_PUBLIC_URL", "looper.example.com")
    monkeypatch.setenv("LOOPER_DATABASE_URL", "postgresql://u:Sekr1t-pw@127.0.0.1:1/db")
    with pytest.raises(SystemExit) as stopped:
        serve.serve_agent(runtime_root=tmp_path, work_dir=tmp_path)
    assert stopped.value.code == 1
    assert "LOOPER_PUBLIC_URL" in caplog.text
    connect.assert_not_called()
    create_pool.assert_not_called()


def test_a_schedules_messages_link_to_the_dashboard() -> None:
    from looper.agents.schedule_notify import Kind, failures_alert_text, recovered_text, run_text, stale_alert_text

    base = "https://looper.example.com"
    page = f"{base}/#projects/proj-1/schedules/sched-1"
    assert run_text(SCHEDULE, "p", RUN, base=base).endswith(f"花费 $0.05 · {base}/#tasks/task-1")
    assert run_text(SCHEDULE, "p", {**RUN, "task_id": None}, base=base).endswith(page)
    assert failures_alert_text(SCHEDULE, "p", RUN, base=base).endswith(page)
    assert stale_alert_text(SCHEDULE, "p", RUN["scheduled_for"], base=base).endswith(page)
    assert recovered_text(SCHEDULE, "p", RUN, Kind.STALE_RECOVERED, base=base).endswith(page)
    assert run_text(SCHEDULE, "p", RUN).endswith("· /#tasks/task-1")  # no address: relative, as before
