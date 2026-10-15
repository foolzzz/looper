"""Links to the dashboard in chat messages: ``LOOPER_PUBLIC_URL``, else the server's own address; a malformed one
keeps ``looper serve`` from starting."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

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
def test_without_a_public_url_links_go_to_the_server(monkeypatch: pytest.MonkeyPatch, host: str, port: int,
                                                     url: str) -> None:
    monkeypatch.delenv(links.PUBLIC_URL_ENV, raising=False)
    assert links.dashboard_url(host=host, port=port) == url


@pytest.mark.parametrize(("raw", "url"), [
    ("https://looper.example.com/", "https://looper.example.com"),
    ("  http://10.0.0.5:8767 ", "http://10.0.0.5:8767"),
    ("https://example.com/looper/", "https://example.com/looper"),  # behind a proxy, under a path
])
def test_a_public_url_is_used_as_given(monkeypatch: pytest.MonkeyPatch, raw: str, url: str) -> None:
    monkeypatch.setenv(links.PUBLIC_URL_ENV, raw)
    assert links.dashboard_url(host="127.0.0.1", port=8767) == url
    assert links.task_link(url, "task-1") == f"{url}/#tasks/task-1"


@pytest.mark.parametrize("raw", ["looper.example.com", "ftp://looper.example.com", "http://", "http://x:99999",
                                 "http://user:secret@x", "http://x/?a=1", "http://x/#tasks", "http://x:port"])
def test_a_malformed_public_url_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(links.PUBLIC_URL_ENV, raw)
    with pytest.raises(ValueError, match=links.PUBLIC_URL_ENV):
        links.dashboard_url(host="127.0.0.1", port=8767)


def test_serve_does_not_start_with_a_malformed_public_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import serve

    monkeypatch.setenv(links.PUBLIC_URL_ENV, "looper.example.com")
    with pytest.raises(serve.StartupCheckFailed, match=links.PUBLIC_URL_ENV):
        asyncio.run(serve._run_agent_system(port=8800, runtime_root=tmp_path, work_dir=tmp_path))


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
