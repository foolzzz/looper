"""The wait loop against disposable PostgreSQL, a recording bus and a local gh stand-in."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from looper.agents import wait_service as service, waits
from looper.agents.message import MessageType, channel_address, planner_address
from looper.db import activity, projects, waits as db_waits
from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import DSN, _run
from tests.test_waits_db import NOW, _plan_with_wait

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class Bus:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[Any] = []
        self.fail = fail

    async def publish(self, message: Any) -> None:
        if self.fail and message.message_type is MessageType.CHAT_NOTICE:
            raise RuntimeError("PRIVATE_NOTICE_TEXT")
        if message.message_type is MessageType.WAIT_EVENT:
            row = await db_waits.get(message.payload["task_id"], "w1")
            assert row["state"] in ("ready", "timed_out")
            if row["state"] == "timed_out":
                assert row["notice"] in ("sent", "dashboard", "failed")
        self.messages.append(message)


def _loop(bus: Bus | None = None, **kwargs: Any) -> Any:
    return service.WaitService(bus=bus or Bus(), has_planner=lambda _: True, **kwargs)


async def _open(pool: Any, *, task_id: str = "task-a", check: Any = None, earliest: int = 0,
                maximum: int = 3600, every: int | None = 300, status: str = "waiting",
                directory: Path | None = None) -> dict[str, Any]:
    project = await _project(pool, task_id)
    await _task(pool, task_id, status, project)
    if directory is not None:
        await pool.execute("UPDATE tasks SET work_dir = $2 WHERE id = $1", task_id, str(directory))
    spec = waits.WaitSpec("CI finishes", maximum, not_before_seconds=earliest or None,
                          check=check, every_seconds=every if check else None)
    due, deadline, next_check = waits.open_times(spec, NOW)
    row = waits.Wait(spec, state=waits.WaitState.WAITING, opened_at=NOW, due_at=due,
                     deadline_at=deadline, next_check_at=next_check).row(f"step-{task_id}")
    await _plan_with_wait(pool, task_id, row.pop("step_id"), **row)
    return project


async def _row(task_id: str = "task-a") -> dict[str, Any]:
    row = await db_waits.get(task_id, "w1")
    assert row is not None
    return row


def _gh(tmp_path: Path) -> tuple[str, Path]:
    payload = tmp_path / "pr.json"
    executable = tmp_path / "fake-gh"
    executable.write_text(f"#!{sys.executable}\nimport pathlib, sys\n"
                          f"p = pathlib.Path({str(payload)!r})\n"
                          "p.with_suffix('.args').write_text(' '.join(sys.argv[1:]))\n"
                          "data = p.read_text()\n"
                          "if data == 'exit': sys.exit(2)\n"
                          "print(data)\n")
    executable.chmod(0o755)
    return str(executable), payload


def _view(path: Path, **changes: Any) -> None:
    path.write_text(json.dumps({"headRefOid": "abcdef012345", "state": "OPEN", "comments": [],
                                "reviews": [], "statusCheckRollup": [], **changes}))


def _check(when: str = "head_changed") -> waits.GithubPrCheck:
    return waits.GithubPrCheck("owner/name", 12, waits.ReadyWhen(when))


def test_time_only_wait_before_and_after_due(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, earliest=60)
        bus = Bus()
        loop = _loop(bus)
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=59)) == 0
        assert (await _row())["state"] == "waiting" and not bus.messages
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=60)) == 1
        row = await _row()
        assert (row["state"], row["outcome"], row["checks"]) == ("ready", "ready", 0)
        assert row["resolution"] == "Ready: the time it waited for came (2026-10-19 12:01 UTC)."
        (message,) = bus.messages
        assert message.message_type is MessageType.WAIT_EVENT
        assert message.target == planner_address("task-a")
        assert message.payload == {"task_id": "task-a", "wait": "w1"}
        assert await loop.run_once(clock=lambda: NOW + timedelta(hours=2)) == 0
        assert len(bus.messages) == 1

    _run(test, monkeypatch)


def test_first_github_check_only_baselines_then_unchanged_then_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    gh, payload = _gh(tmp_path)

    async def test(pool: Any) -> None:
        await _open(pool, check=_check())
        bus = Bus()
        loop = _loop(bus, gh=gh)
        _view(payload)
        for tick in (0, 300):
            assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=tick)) == 0
            row = await _row()
            assert row["state"] == "waiting" and row["last_result"] == "not_ready"
        assert row["baseline"] == {"head": "abcdef012345", "comments": 0}
        assert row["checks"] == 2 and row["next_check_at"] == NOW + timedelta(seconds=600)
        _view(payload, headRefOid="new-head")
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=600)) == 1
        row = await _row()
        assert (row["state"], row["outcome"], row["checks"], row["last_result"]) == ("ready", "ready", 3, "ready")
        assert "changed" in row["resolution"]
        assert payload.with_suffix(".args").read_text() == (
            "pr view 12 --repo owner/name --json headRefOid,state,comments,reviews,statusCheckRollup")
        assert [m.message_type for m in bus.messages] == [MessageType.WAIT_EVENT]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("when", "changes"), [
    ("checks_finished", {"statusCheckRollup": [{"status": "COMPLETED"}, {"state": "SUCCESS"}]}),
    ("new_comments", {"comments": [{}], "reviews": [{}]}),
    ("head_changed", {"state": "MERGED"}),
    ("head_changed", {"state": "CLOSED"}),
])
def test_github_first_success_is_only_baseline_even_if_already_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, when: str, changes: dict[str, Any],
) -> None:
    gh, payload = _gh(tmp_path)

    async def test(pool: Any) -> None:
        await _open(pool, check=_check(when))
        _view(payload, **({} if when == "new_comments" else changes))
        loop = _loop(gh=gh)
        assert await loop.run_once(clock=lambda: NOW) == 0
        row = await _row()
        assert (row["checks"], row["last_result"], row["last_output"]) == (
            1, "not_ready", "baseline: head abcdef0, 0 comments")
        _view(payload, **changes)
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=300)) == 1
        assert (await _row())["last_result"] == "ready"

    _run(test, monkeypatch)


@pytest.mark.parametrize("output", ["exit", "invalid JSON"])
def test_github_errors_keep_waiting_and_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str) -> None:
    gh, payload = _gh(tmp_path)

    async def test(pool: Any) -> None:
        await _open(pool, check=_check())
        payload.write_text(output)
        assert await _loop(gh=gh).run_once(clock=lambda: NOW) == 0
        row = await _row()
        assert (row["state"], row["last_result"], row["checks"], row["baseline"]) == ("waiting", "error", 1, None)
        assert row["next_check_at"] == NOW + timedelta(seconds=300)
        _view(payload)
        assert await _loop(gh=gh).run_once(clock=lambda: NOW + timedelta(seconds=300)) == 0
        assert (await _row())["baseline"] == {"head": "abcdef012345", "comments": 0}

    _run(test, monkeypatch)


def test_ready_check_waits_for_earliest_time_and_records_before_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _open(pool, check=waits.CommandCheck("true"), earliest=600, directory=tmp_path)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        loop = _loop()
        assert await loop.run_once(clock=lambda: NOW) == 0
        assert (await _row())["last_result"] == "ready"
        move = db_waits.move

        async def observed(step_id: str, **kwargs: Any) -> Any:
            row = await _row()
            assert row["last_checked_at"] == NOW + timedelta(seconds=600)
            assert row["checks"] == 2 and row["last_result"] == "ready"
            assert kwargs["expect"] == ("waiting",)
            return await move(step_id, **kwargs)

        monkeypatch.setattr(db_waits, "move", observed)
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=600)) == 1
        assert (await _row())["state"] == "ready"

    _run(test, monkeypatch)


def test_github_change_before_earliest_time_keeps_original_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    gh, payload = _gh(tmp_path)

    async def test(pool: Any) -> None:
        await _open(pool, check=_check(), earliest=600)
        loop = _loop(gh=gh)
        _view(payload)
        assert await loop.run_once(clock=lambda: NOW) == 0
        _view(payload, headRefOid="new-head")
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=300)) == 0
        row = await _row()
        assert row["last_result"] == "ready" and row["state"] == "waiting"
        assert row["baseline"] == {"head": "abcdef012345", "comments": 0}
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=600)) == 1
        assert "changed" in (await _row())["resolution"]

    _run(test, monkeypatch)


@pytest.mark.parametrize("race", ["record", "move"])
def test_concurrent_user_move_wins_and_does_not_wake_or_notify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, race: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await _open(pool, check=waits.CommandCheck("true"), directory=tmp_path, maximum=60)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        original = db_waits.record_check if race == "record" else db_waits.move
        move = db_waits.move

        async def competing(step_id: str, **kwargs: Any) -> Any:
            assert await move(step_id, to="cancel_requested") is not None
            return await original(step_id, **kwargs)

        monkeypatch.setattr(db_waits, "record_check" if race == "record" else "move", competing)
        bus = Bus()
        assert await _loop(bus).run_once(clock=lambda: NOW + timedelta(seconds=60)) == 0
        row = await _row()
        assert row["state"] == "cancel_requested" and row["notice"] is None
        assert row["checks"] == (0 if race == "record" else 1)
        assert not bus.messages and not await activity.list_activity("task-a")

    _run(test, monkeypatch)


@pytest.mark.parametrize("code", [0, 1])
def test_command_result_output_environment_and_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, code: int,
) -> None:
    monkeypatch.setenv("TZ", "inherited-marker")
    monkeypatch.setenv("SOME_SECRET", "private-secret")
    caplog.set_level(logging.INFO)
    command = (f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "import os, pathlib; pathlib.Path('ran').write_text(os.getcwd()); "
        "print('PRIVATE_OUTPUT' + 'x' * 3000); "
        "print(os.environ.get('TZ')); "
        "print(os.environ.get('SOME_SECRET', 'filtered')); "
        f"raise SystemExit({code})"))

    async def test(pool: Any) -> None:
        project = await _open(pool, check=waits.CommandCheck(command), directory=tmp_path)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        assert await _loop().run_once(clock=lambda: NOW) == (1 if code == 0 else 0)
        row = await _row()
        assert row["last_result"] == ("ready" if code == 0 else "not_ready")
        assert len(row["last_output"].encode()) == 2048
        assert row["last_output"].endswith("inherited-marker\nfiltered\n")
        assert "inherited-marker" not in row["resolution"]
        assert row["resolution"] == ("Ready: the command exited 0." if code == 0 else "")
        assert (tmp_path / "ran").read_text() == str(tmp_path)
        assert "PRIVATE_OUTPUT" not in caplog.text and command not in caplog.text
        assert "private-secret" not in caplog.text and "inherited-marker" not in caplog.text

    _run(test, monkeypatch)


def test_command_policy_is_read_again_at_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        project = await _open(pool, check=waits.CommandCheck("touch forbidden"), directory=tmp_path)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        loop = _loop()
        await projects.update_project(project["id"], execution={"wait_commands": "off"})
        assert await loop.run_once(clock=lambda: NOW) == 0
        row = await _row()
        assert (row["state"], row["last_result"], row["last_error"]) == ("waiting", "error", "wait_commands is off")
        assert not (tmp_path / "forbidden").exists()

    _run(test, monkeypatch)


def test_command_timeout_stays_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service, "CHECK_TIMEOUT_SEC", 0.05)

    async def test(pool: Any) -> None:
        project = await _open(pool, check=waits.CommandCheck("sleep 30"), directory=tmp_path)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        assert await _loop().run_once(clock=lambda: NOW) == 0
        row = await _row()
        assert (row["state"], row["last_result"], row["checks"]) == ("waiting", "error", 1)
        assert "timed out" in row["last_error"]

    _run(test, monkeypatch)


def test_stopped_tasks_are_untouched_with_active_positive_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        for name, status in (("stopped", "stopped"), ("active", "waiting")):
            project = await _open(pool, task_id=name, status=status, directory=tmp_path,
                                  check=waits.CommandCheck(f"touch {name}"), maximum=60)
            await projects.update_project(project["id"], execution={"wait_commands": "on"})
        bus = Bus()
        assert await _loop(bus).run_once(clock=lambda: NOW + timedelta(seconds=60)) == 1
        row = await _row("stopped")
        assert (row["state"], row["checks"], row["notice"]) == ("waiting", 0, None)
        assert not (tmp_path / "stopped").exists() and (tmp_path / "active").exists()
        assert (await _row("active"))["state"] == "ready"
        assert [m.payload["task_id"] for m in bus.messages] == ["active"]

    _run(test, monkeypatch)


@pytest.mark.parametrize("result", [waits.CheckResult.READY, waits.CheckResult.NOT_READY])
def test_task_stopped_during_check_cannot_move_or_notify(
    monkeypatch: pytest.MonkeyPatch, result: waits.CheckResult,
) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, check=_check(), maximum=60)
        await pool.execute("UPDATE tasks SET origin_channel_id = 'channel-telegram', "
                           "chat_kind = 'chat', chat_id = 'chat-1' WHERE id = 'task-a'")
        await pool.execute("UPDATE task_waits SET baseline = $1::jsonb WHERE task_id = 'task-a'",
                           json.dumps({"head": "old-head", "comments": 0}))
        checked = []

        async def check(*args: Any, **kwargs: Any) -> Any:
            checked.append(True)
            await pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = 'task-a'")
            return result, "checked", "", {"resolution": "Ready.", "exit_code": 0}

        monkeypatch.setattr(service, "run_github_pr", check)
        bus = Bus()
        assert await _loop(bus).run_once(clock=lambda: NOW + timedelta(seconds=60)) == 0
        row = await _row()
        assert checked == [True] and row["checks"] == 1
        assert (row["state"], row["outcome"], row["notice"], row["resolution"]) == ("waiting", None, None, "")
        assert not bus.messages and not await activity.list_activity("task-a")

    _run(test, monkeypatch)


def test_task_stopped_after_timeout_move_is_not_notified(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        for task_id in ("task-a", "task-control"):
            await _open(pool, task_id=task_id, earliest=7200, maximum=3600)
            await pool.execute("UPDATE tasks SET origin_channel_id = 'channel-telegram', "
                               "chat_kind = 'chat', chat_id = $1 WHERE id = $1", task_id)
        insert = activity.insert_activity

        async def stop_before_notice(**kwargs: Any) -> Any:
            inserted = await insert(**kwargs)
            if kwargs["task_id"] == "task-a":
                assert (await _row())["state"] == "timed_out"
                await pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = 'task-a'")
            return inserted

        monkeypatch.setattr(activity, "insert_activity", stop_before_notice)
        bus = Bus()
        loop = service.WaitService(bus=bus, has_planner=lambda _: False)
        assert await loop.run_once(clock=lambda: NOW + timedelta(hours=1)) == 2
        assert (await _row())["notice"] is None
        assert (await _row("task-control"))["notice"] == "sent"
        (notice,) = bus.messages
        assert notice.message_type is MessageType.CHAT_NOTICE and notice.payload["chat"] == "task-control"

    _run(test, monkeypatch)


def test_no_planner_leaves_ready_in_database(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, earliest=60)
        bus = Bus()
        loop = service.WaitService(bus=bus, has_planner=lambda _: False)
        assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=60)) == 1
        assert (await _row())["state"] == "ready" and not bus.messages

    _run(test, monkeypatch)


@pytest.mark.parametrize("kind", ["time", "command"])
def test_ready_wins_over_deadline_in_same_tick(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str) -> None:
    async def test(pool: Any) -> None:
        project = await _open(pool, earliest=60, maximum=60, directory=tmp_path,
                              check=waits.CommandCheck("true") if kind == "command" else None)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        bus = Bus()
        assert await _loop(bus).run_once(clock=lambda: NOW + timedelta(seconds=60)) == 1
        row = await _row()
        assert (row["state"], row["notice"]) == ("ready", None)
        assert [m.message_type for m in bus.messages] == [MessageType.WAIT_EVENT]
        assert not await activity.list_activity("task-a")

    _run(test, monkeypatch)


@pytest.mark.parametrize(("channel", "fail", "notice"), [
    ("telegram", False, "sent"), ("lark", False, "sent"), ("lark_bot", False, "sent"),
    (None, False, "dashboard"), ("telegram", True, "failed"),
])
def test_timeout_notice_and_activity_precede_planner_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    channel: str | None, fail: bool, notice: str,
) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, earliest=7200, maximum=3600)
        if channel:
            await pool.execute("UPDATE tasks SET origin_channel_id = $2, chat_kind = 'chat', chat_id = 'chat-1', "
                               "chat_mention = 'requester', title = $3 WHERE id = $1", "task-a",
                               channel_address(channel).agent_id, "长" * 50)
        bus = Bus(fail=fail)
        loop = _loop(bus, dashboard_url="https://looper.example.com")
        assert await loop.run_once(clock=lambda: NOW + timedelta(hours=1)) == 1
        row = await _row()
        assert (row["state"], row["notice"], row["outcome"], row["resolution"]) == (
            "timed_out", notice, None, "Timed out after 1 h.")
        (entry,) = await activity.list_activity("task-a")
        assert entry["summary"] == "Wait w1 timed out after 1 h: CI finishes; the Planner decides next"
        assert bus.messages[-1].message_type is MessageType.WAIT_EVENT
        notices = [m for m in bus.messages if m.message_type is MessageType.CHAT_NOTICE]
        if notice == "sent":
            (message,) = notices
            assert message.target == channel_address(channel)
            assert message.payload["chat"] == "chat-1" and message.payload["mention"] == "requester"
            lines = message.payload["text"].splitlines()
            assert lines[:3] == ["⏳ 等待超时 · " + "长" * 39 + "…", "CI finishes",
                                  "等了 1 小时，条件一直没有满足；规划者接下来决定怎么做。"]
            assert "https://looper.example.com/#tasks/task-a" in lines[3] and len(lines) == 4
        else:
            assert not notices
        assert await loop.run_once(clock=lambda: NOW + timedelta(hours=2)) == 0
        assert len(await activity.list_activity("task-a")) == 1
        assert "PRIVATE_NOTICE_TEXT" not in caplog.text
        if fail:
            assert any(r.levelno == logging.WARNING for r in caplog.records)

    _run(test, monkeypatch)


@pytest.mark.parametrize(("result", "maximum", "duration", "waited"), [
    (waits.CheckResult.NOT_READY, 60, "1 min", "1 分钟"),
    (waits.CheckResult.ERROR, 86400, "1 d", "1 天"),
])
def test_unsatisfied_check_is_recorded_before_timeout(
    monkeypatch: pytest.MonkeyPatch, result: waits.CheckResult, maximum: int, duration: str, waited: str,
) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, check=_check(), maximum=maximum)
        await pool.execute("UPDATE tasks SET origin_channel_id = 'channel-telegram', "
                           "chat_kind = 'chat', chat_id = 'chat-1' WHERE id = 'task-a'")

        async def check(*args: Any, **kwargs: Any) -> Any:
            return result, "pending", "failed" if result is waits.CheckResult.ERROR else "", {
                "baseline": {"head": "abcdef0", "comments": 0}, "resolution": "", "exit_code": 2}

        monkeypatch.setattr(service, "run_github_pr", check)
        bus = Bus()
        assert await _loop(bus).run_once(clock=lambda: NOW + timedelta(seconds=maximum)) == 1
        row = await _row()
        assert (row["checks"], row["last_result"], row["state"], row["notice"]) == (
            1, result.value, "timed_out", "sent")
        assert row["resolution"] == f"Timed out after {duration}."
        assert f"等了 {waited}，" in bus.messages[0].payload["text"]
        assert bus.messages[-1].message_type is MessageType.WAIT_EVENT

    _run(test, monkeypatch)


def test_error_activity_only_on_transition_and_error_is_clipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, check=_check())
        results = iter([waits.CheckResult.ERROR, waits.CheckResult.ERROR, waits.CheckResult.NOT_READY,
                        waits.CheckResult.ERROR])

        async def check(*args: Any, **kwargs: Any) -> Any:
            result = next(results)
            return result, "", "e" * 250 if result is waits.CheckResult.ERROR else "", {
                "baseline": {"head": "abcdef0", "comments": 0}, "resolution": "", "exit_code": 0}

        monkeypatch.setattr(service, "run_github_pr", check)
        loop = _loop()
        for tick in range(4):
            assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=tick * 300)) == 0
            assert len(await activity.list_activity("task-a")) == (1 if tick < 3 else 2)
        assert [a["summary"] for a in await activity.list_activity("task-a")] == [
            "Wait w1 check failed: " + "e" * 200] * 2

    _run(test, monkeypatch)


def test_one_row_failure_does_not_block_others_and_checks_are_bounded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def test(pool: Any) -> None:
        for number in range(9):
            await _open(pool, task_id=f"task-{number}", check=_check())
        running = peak = calls = 0

        async def check(*args: Any, **kwargs: Any) -> Any:
            nonlocal running, peak, calls
            running += 1
            calls += 1
            number = calls
            peak = max(peak, running)
            try:
                await asyncio.sleep(0.02)
                if number == 9:
                    raise RuntimeError("PRIVATE_ROW_ERROR")
                return waits.CheckResult.NOT_READY, "", "", {
                    "baseline": {"head": "abcdef0", "comments": 0}, "resolution": "", "exit_code": 0}
            finally:
                running -= 1

        monkeypatch.setattr(service, "run_github_pr", check)
        assert await _loop().run_once(clock=lambda: NOW) == 0
        assert calls == 9 and peak == 4 and running == 0
        counts = [int((await _row(f"task-{n}"))["checks"]) for n in range(9)]
        assert sum(counts) == 8 and counts.count(0) == 1
        assert "step-task-" in caplog.text and "PRIVATE_ROW_ERROR" not in caplog.text

    _run(test, monkeypatch)


@pytest.mark.parametrize(("wait_state", "task_status"), [
    ("ready", "waiting"), ("cancel_requested", "waiting"), ("timed_out", "waiting"),
    ("waiting", "stopped"), ("waiting", "failed"), ("waiting", "completed"),
    ("waiting", "cancelled"),
])
def test_queued_check_rereads_wait_and_task_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wait_state: str, task_status: str,
) -> None:
    async def test(pool: Any) -> None:
        for task_id in ("task-a", "task-control"):
            project = await _open(pool, task_id=task_id, directory=tmp_path,
                                  check=waits.CommandCheck(f"touch {task_id}"))
            await projects.update_project(project["id"], execution={"wait_commands": "on"})
        loop = _loop()
        loop._semaphore = asyncio.Semaphore(1)
        await loop._semaphore.acquire()
        listed = asyncio.Event()
        due = db_waits.due

        async def selected(*args: Any, **kwargs: Any) -> Any:
            rows = await due(*args, **kwargs)
            listed.set()
            return rows

        monkeypatch.setattr(db_waits, "due", selected)
        running = asyncio.create_task(loop.run_once(clock=lambda: NOW))
        try:
            await asyncio.wait_for(listed.wait(), 2)
            await pool.execute("UPDATE tasks SET status = $2 WHERE id = $1", "task-a", task_status)
            if wait_state != "waiting":
                await db_waits.move("step-task-a", to=wait_state,
                                    outcome="resumed" if wait_state == "ready" else None)
        finally:
            loop._semaphore.release()
        await asyncio.wait_for(running, 5)
        assert not (tmp_path / "task-a").exists(), "the queued check ran after its wait or task ended"
        assert (await _row())["checks"] == 0
        assert (tmp_path / "task-control").is_file()
        assert (await _row("task-control"))["checks"] == 1

    _run(test, monkeypatch)


def test_default_interval_is_opened_from_real_settings_and_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        old_limits, old_sources = waits._limits, waits._sources.copy()
        try:
            view = await waits.update({"min_check_seconds": 900})
            assert view["min_check_seconds"]["value"] == 900
            await _open(pool, check=_check(), every=None)
            assert (await _row())["spec"]["every_seconds"] == 900

            async def check(*args: Any, **kwargs: Any) -> Any:
                return waits.CheckResult.NOT_READY, "", "", {
                    "baseline": {"head": "abcdef0", "comments": 0}, "resolution": "", "exit_code": 0}

            monkeypatch.setattr(service, "run_github_pr", check)
            loop = _loop()
            assert await loop.run_once(clock=lambda: NOW) == 0
            assert (await _row())["next_check_at"] == NOW + timedelta(seconds=900)
            assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=899)) == 0
            assert (await _row())["checks"] == 1
            assert await loop.run_once(clock=lambda: NOW + timedelta(seconds=900)) == 0
            assert (await _row())["checks"] == 2
        finally:
            await waits.update({"min_check_seconds": None, "max_wait_seconds": None})
            waits._limits, waits._sources = old_limits, old_sources

    _run(test, monkeypatch)


def test_start_stop_reclaims_loop_and_inflight_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _open(pool, check=_check())
        await pool.execute("UPDATE task_waits SET next_check_at = now() - interval '1 minute'")
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def check(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(service, "run_github_pr", check)
        loop = _loop(interval=0.01)
        await loop.start()
        task = loop._task
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await loop.start()
            assert loop._task is task
        finally:
            await asyncio.wait_for(loop.stop(), 2)
        assert task.done() and loop._task is None and cancelled.is_set()
        await loop.stop()
        assert (await _row())["checks"] == 0

    _run(test, monkeypatch)


def test_stop_reaps_a_running_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    command = "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(
        "import os, pathlib, time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(30)")

    async def test(pool: Any) -> None:
        project = await _open(pool, check=waits.CommandCheck(command), directory=tmp_path)
        await projects.update_project(project["id"], execution={"wait_commands": "on"})
        await pool.execute("UPDATE task_waits SET next_check_at = now() - interval '1 minute'")
        loop = _loop(interval=0.01)
        await loop.start()
        pid_file = tmp_path / "pid"

        async def started() -> int:
            while not pid_file.exists() or not pid_file.read_text():
                await asyncio.sleep(0.01)
            return int(pid_file.read_text())

        try:
            pid = await asyncio.wait_for(started(), 3)
        finally:
            await asyncio.wait_for(loop.stop(), 8)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert (await _row())["checks"] == 0

    _run(test, monkeypatch)


def test_serve_loads_wait_settings_and_owns_wait_service_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Run the composition function with inert services: no listener, child process or user runtime."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import looper.db as db
    from looper.agents import (
        consult, operator_auth, operator_notes, proc_tree, profile_registry, read_only_copies, room,
        scheduler_settings, serve, state_import, step_workspaces, task_workspaces,
    )
    from looper.agents.ops import planner as planner_ops, room as room_ops, service as manager_ops
    from looper.agents.runtimes import codex_login, grok_login
    from looper.db import channel_configs, llm_records, room as db_room, system_settings

    monkeypatch.setattr(waits, "_limits", waits.WaitLimits())
    monkeypatch.setattr(waits, "_sources", {})

    async def test(pool: Any) -> None:
        events: list[str] = []
        await system_settings.put_settings({waits.MAX_WAIT_KEY: "7200", waits.MIN_CHECK_KEY: "900"})

        def fake(name: str) -> Any:
            async def start() -> None:
                events.append(name + ".start")
                assert waits.limits() == waits.WaitLimits(7200, 900)

            async def stop() -> None:
                events.append(name + ".stop")

            return SimpleNamespace(start=start, stop=stop, start_all=start, stop_all=stop,
                                   register=MagicMock(), resume_all=AsyncMock(), notify=AsyncMock(),
                                   post=AsyncMock(), task_ended=AsyncMock(), list_channels=lambda: [])

        bus = SimpleNamespace(shutdown=AsyncMock())
        router = SimpleNamespace(has_planner=lambda task_id: task_id == "task-active")
        for name in ("Broadcaster", "ManagerConfig", "WorkerConfig"):
            monkeypatch.setattr(serve, name, MagicMock())
        for name, value in (("MessageBus", bus), ("AgentSupervisor", fake("supervisor")),
                            ("ChannelRegistry", fake("registry")), ("WebChannelAdapter", fake("web")),
                            ("ManagerAgent", fake("manager")), ("Scheduler", fake("scheduler")),
                            ("GateReminders", fake("gates")), ("GateCourier", fake("courier")),
                            ("TaskRouter", router), ("WorkerPool", fake("workers")),
                            ("AgentHTTPServer", fake("http"))):
            constructor = MagicMock(return_value=value)
            if name == "ChannelRegistry":
                value.register = AsyncMock()
            monkeypatch.setattr(serve, name, constructor)

        class WaitService:
            def __init__(self, **kwargs: Any) -> None:
                assert kwargs["bus"] is bus
                assert kwargs["has_planner"] is router.has_planner
                assert kwargs["has_planner"]("task-active") and not kwargs["has_planner"]("task-stopped")
                assert kwargs["dashboard_url"] == "https://looper.example.com"
                events.append("waits.created")

            async def start(self) -> None:
                events.append("waits.start")
                assert waits.limits() == waits.WaitLimits(7200, 900)

            async def stop(self) -> None:
                events.append("waits.stop")

        # The service may be imported at module scope or locally; both references remain inert.
        monkeypatch.setattr(service, "WaitService", WaitService)
        monkeypatch.setattr(serve, "WaitService", WaitService, raising=False)
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "ensure_schema", AsyncMock())
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(operator_auth, "ensure_token", lambda _: "unused-test-token")
        monkeypatch.setattr(operator_notes, "configure_root", lambda _: None)
        for module, name, result in (
            (serve, "_back_up_before_schema_change", None), (serve, "seed_role_configs", None),
            (serve, "seed_channel_configs", None), (profile_registry, "register_found_runtimes", []),
            (state_import, "import_file_state", None), (proc_tree, "sweep", None),
            (serve, "tasks_left_running", SimpleNamespace(resumable=[], starting=[])),
            (llm_records, "end_unfinished_records", 0), (step_workspaces, "sweep", 0),
            (task_workspaces, "sweep", 0), (codex_login, "inspect_at_start", None),
            (grok_login, "inspect_at_start", None), (read_only_copies, "reclaim_unused", None),
            (db_room, "tasks_keeping_sessions", []), (channel_configs, "get_config", None),
            (serve, "carry_on_tasks", None),
        ):
            monkeypatch.setattr(module, name, AsyncMock(return_value=result))
        monkeypatch.setattr(room, "sweep_run_sessions", lambda *args: None)
        for module, name in ((manager_ops, "Platform"), (manager_ops, "ManagerOps"),
                             (planner_ops, "PlannerOps"), (room_ops, "RoomOps"), (room, "RoomService")):
            monkeypatch.setattr(module, name, MagicMock())
        monkeypatch.setattr(consult, "ConsultService", MagicMock(return_value=fake("consults")))
        check = SimpleNamespace(status=serve.self_check.OK, to_dict=lambda: {})
        for name in ("check_database", "codex_login_check", "grok_login_check"):
            monkeypatch.setattr(serve.self_check, name, AsyncMock(return_value=check))
        monkeypatch.setattr(serve.self_check, "check_claude", lambda _: [])
        for name in ("check_lark_cli", "check_git"):
            monkeypatch.setattr(serve.self_check, name, lambda *args: check)
        monkeypatch.setattr(serve.self_check, "log_problems", lambda _: None)
        load = waits.load

        async def load_waits() -> Any:
            events.append("waits.load")
            return await load()

        async def load_scheduler() -> None:
            events.append("scheduler.load")

        monkeypatch.setattr(waits, "load", load_waits)
        monkeypatch.setattr(scheduler_settings, "load", load_scheduler)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: (
            loop.call_soon(callback) if sig == serve.signal.SIGINT else None))
        ops = SimpleNamespace(host="127.0.0.1", port=9876, public_url="https://looper.example.com",
                              operator_notes_root=tmp_path, database_url=DSN)
        await asyncio.wait_for(serve._run_agent_system(ops=ops, runtime_root=tmp_path,
                                                      work_dir=tmp_path), 3)
        assert events[:3] == ["scheduler.load", "waits.load", "waits.created"]
        assert events[events.index("gates.start") + 1] == "waits.start"
        assert events[events.index("gates.stop") + 1] == "waits.stop"
        assert events.count("waits.start") == events.count("waits.stop") == 1
        assert events.index("http.start") < events.index("waits.start")
        assert events.index("waits.stop") < events.index("supervisor.stop")

    _run(test, monkeypatch)
