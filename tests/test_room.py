"""The agent room's rules without a database: who may post what to whom, what a seat sees, who is online, when two
seats trade too long, which result a run's reminders never replace, and the prompts and contract with the room off."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from typing import Any

import pytest

from looper.agents.ops import ErrorCode, OpError
from looper.agents.room import (
    MemberKind,
    MessageKind,
    Presence,
    RoomDelivery,
    RoomService,
    Seat,
    Target,
    addressed,
    check_scope,
    members,
    ping_pong_rounds,
    room_section,
    run_seat,
    unread_counts,
    visible,
)
from looper.agents.tiers.worker import answers_only_reminders

WORKER = Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1")
FE = Seat("fe_rd@s2", "fe_rd", MemberKind.WORK, run="worker-2")
REVIEWER = Seat("reviewer@s1", "reviewer", MemberKind.REVIEW, run="reviewer-1")
PLANNER = Seat.planner()

TO_WORK = Target("fe_rd@s2", frozenset({MemberKind.WORK}), ("fe_rd@s2",))
TO_REVIEW = Target("reviewer@s1", frozenset({MemberKind.REVIEW}), ("reviewer@s1",))
TO_REVIEWERS = Target("reviewer", frozenset({MemberKind.REVIEW}), ("reviewer@s1", "reviewer@s2"))
TO_PLANNER = Target("planner", frozenset({MemberKind.PLANNER}), ("planner",))


def _message(sender: str, kind: str = "info", *, to: tuple[str, ...] = (), everyone: bool = False,
             id: int = 1) -> dict[str, Any]:
    return {"id": id, "sender_seat": sender, "kind": kind, "audience": list(to), "audience_all": everyone,
            "body": "x"}


QUESTION_FROM_REVIEWER = _message("reviewer@s1", "question", to=("worker@s1",))
QUESTION_FROM_PLANNER_TO_REVIEWER = _message("planner", "question", to=("reviewer@s1",))


@pytest.mark.parametrize(("sender", "kind", "everyone", "targets", "reply", "refused"), [
    # A work seat: to work seats, the Planner, everyone; to a Reviewer only to answer the Reviewer's own question.
    (WORKER, "interface", True, [], None, None),
    (WORKER, "question", False, [TO_WORK, TO_PLANNER], None, None),
    (WORKER, "info", False, [TO_REVIEW], None, ErrorCode.FORBIDDEN),
    (WORKER, "question", False, [TO_REVIEWERS], None, ErrorCode.FORBIDDEN),
    (WORKER, "answer", False, [TO_REVIEW], QUESTION_FROM_REVIEWER, None),
    (FE, "answer", False, [TO_REVIEW], QUESTION_FROM_REVIEWER, ErrorCode.FORBIDDEN),  # the question was not to it
    (WORKER, "answer", False, [TO_REVIEWERS], QUESTION_FROM_REVIEWER, ErrorCode.FORBIDDEN),  # one seat only
    # A review seat: only questions, never to everyone or to Reviewers; answers only what was put to it.
    (REVIEWER, "question", False, [Target("worker@s1", frozenset({MemberKind.WORK}), ("worker@s1",))], None, None),
    (REVIEWER, "question", False, [TO_PLANNER], None, None),
    (REVIEWER, "interface", False, [TO_PLANNER], None, ErrorCode.FORBIDDEN),
    (REVIEWER, "question", True, [], None, ErrorCode.FORBIDDEN),
    (REVIEWER, "question", False, [TO_REVIEWERS], None, ErrorCode.FORBIDDEN),
    (REVIEWER, "answer", False, [TO_PLANNER], QUESTION_FROM_PLANNER_TO_REVIEWER, None),
    (REVIEWER, "answer", False, [TO_PLANNER], _message("planner", "question", to=("fe_rd@s2",)), ErrorCode.FORBIDDEN),
    # The Planner says anything to anyone.
    (PLANNER, "announce", True, [], None, None),
    (PLANNER, "info", False, [TO_REVIEWERS], None, None),
    # An answer replies to a question; Looper's own kinds are not posted.
    (PLANNER, "answer", False, [TO_WORK], None, ErrorCode.INVALID),
    (WORKER, "answer", False, [TO_WORK], _message("fe_rd@s2", "info", to=("worker@s1",)), ErrorCode.INVALID),
    (WORKER, "notice", False, [TO_WORK], None, ErrorCode.INVALID),
])
def test_the_scope_matrix(sender: Seat, kind: str, everyone: bool, targets: list[Target],
                          reply: dict[str, Any] | None, refused: ErrorCode | None) -> None:
    def check() -> None:
        check_scope(sender, MessageKind(kind), everyone=everyone, targets=targets, reply=reply)

    if refused is None:
        check()
    else:
        with pytest.raises(OpError) as caught:
            check()
        assert caught.value.code is refused


@pytest.mark.parametrize(("message", "seat", "sees", "for_it"), [
    (_message("fe_rd@s2", everyone=True), WORKER, True, False),
    (_message("fe_rd@s2", to=("worker@s1",)), WORKER, True, True),
    (_message("fe_rd@s2", to=("worker",)), WORKER, True, True),  # by its role
    (_message("fe_rd@s2", to=("planner",)), WORKER, False, False),  # between others
    (_message("worker@s1", to=("planner",)), WORKER, True, False),  # its own
    (_message("fe_rd@s2", to=("fe_rd",)), PLANNER, True, False),  # the Planner sees everything
    (_message("fe_rd@s2", to=("planner",)), PLANNER, True, True),
    (_message("worker@s1", to=("worker",)), PLANNER, True, False),  # a role named 'planner' is no seat: by key only
])
def test_what_a_seat_sees_and_what_is_for_it(message: dict[str, Any], seat: Seat, sees: bool, for_it: bool) -> None:
    assert visible(message, seat) is sees
    assert addressed(message, seat) is for_it


def test_unread_counts_skip_what_was_read_its_own_and_what_it_does_not_see() -> None:
    rows = [_message("fe_rd@s2", "question", to=("worker@s1",), id=1),
            _message("fe_rd@s2", "interface", everyone=True, id=2),
            _message("fe_rd@s2", "info", everyone=True, id=3),
            _message("worker@s1", "info", everyone=True, id=4),
            _message("fe_rd@s2", "info", to=("planner",), id=5)]
    assert unread_counts(rows, WORKER, 0) == {"for_you": 1, "interface": 1, "room": 1}
    assert unread_counts(rows, WORKER, 2) == {"for_you": 0, "interface": 0, "room": 1}


def _step(key: str, status: str, **columns: Any) -> dict[str, Any]:
    return {"id": f"step-{key}", "step_key": key, "status": status, "kind": "work", **columns}


def test_presence_comes_from_the_pools_runs_and_the_steps() -> None:
    since = datetime(2026, 10, 17, tzinfo=timezone.utc)
    steps = [_step("s1", "dispatched", worker_id="worker-1", role_key="backend_rd"),
             _step("s2", "dispatched", worker_id="worker-2"),
             _step("s3", "verified", worker_id="worker-3", review=True, reviewer_id="reviewer-3"),
             _step("s4", "pending", review=True),
             _step("s5", "skipped"),
             _step("g1", "waiting_user", kind="gate")]
    seats = {m.seat: m for m in members(steps, {"worker-1": ("running", since), "worker-2": ("pending", since)})}
    assert set(seats) == {"backend_rd@s1", "worker@s2", "worker@s3", "reviewer@s3", "worker@s4", "reviewer@s4",
                          "worker@s5"}
    assert seats["backend_rd@s1"].presence is Presence.ONLINE and seats["backend_rd@s1"].run == "worker-1"
    assert seats["worker@s2"].presence is Presence.WAITING
    assert seats["worker@s3"].presence is Presence.OFFLINE and seats["reviewer@s3"].presence is Presence.OFFLINE
    assert seats["worker@s4"].presence is Presence.PLANNED and seats["reviewer@s4"].presence is Presence.PLANNED
    assert seats["worker@s5"].presence is Presence.DROPPED
    assert seats["reviewer@s3"].kind is MemberKind.REVIEW


def test_a_review_role_has_a_seat_of_its_own_online_in_its_stage() -> None:
    since = datetime(2026, 10, 17, tzinfo=timezone.utc)
    steps = [_step("s1", "completed", worker_id="worker-1", review=True, reviewer_id="reviewer-9",
                   review_role_key="security", review_stage="role")]
    seats = {m.seat: m for m in members(steps, {"reviewer-9": ("running", since)})}
    assert seats["security@s1"].presence is Presence.ONLINE
    assert seats["reviewer@s1"].presence is Presence.OFFLINE  # the built-in review came first


def test_a_runs_seat_is_its_role_at_its_step() -> None:
    work = run_seat({"task_id": "step-1", "step_key": "s1", "task_role": {"id": "fe_rd", "kind": "work"},
                     "context": {"work_dir": "/w"}}, is_verify=False, run="worker-1")
    assert (work.key, work.role, work.kind, work.step_id, work.work_dir) == ("fe_rd@s1", "fe_rd", MemberKind.WORK,
                                                                             "step-1", "/w")
    review = run_seat({"task_id": "step-1", "step_key": "s1"}, is_verify=True, run="reviewer-1")
    assert (review.key, review.kind) == ("reviewer@s1", MemberKind.REVIEW)


@pytest.mark.parametrize(("senders", "rounds"), [
    (["a", "b", "a", "b"], 4),
    (["a", "b", "b"], 1),  # the same seat twice ends the trade
    (["a", "c", "a"], 1),  # a third seat in between
    ([], 0),
])
def test_ping_pong_counts_the_newest_messages_traded_by_two_seats(senders: list[str], rounds: int) -> None:
    others = {"a": "b", "b": "a", "c": "a"}
    messages = [_message(s, to=(others[s],), id=i) for i, s in enumerate(senders, 1)]
    assert ping_pong_rounds(messages, "a", "b") == rounds


def test_an_answer_settles_the_trade_before_it() -> None:
    messages = [_message("a", "question", to=("b",), id=1), _message("b", "answer", to=("a",), id=2),
                _message("a", "info", to=("b",), id=3), _message("b", "info", to=("a",), id=4)]
    assert ping_pong_rounds(messages, "a", "b") == 2


@pytest.mark.parametrize(("answered", "reminders", "only"), [
    (["reminder-1"], {"reminder-1"}, True),
    (["looper-reminder-0123456789ab-7"], {"looper-reminder-0123456789ab-7"}, True),
    (["reminder-1", "reminder-2"], {"reminder-1", "reminder-2"}, True),
    (["prompt", "reminder-1"], {"reminder-1"}, False),  # folded into the step's own turn
    ([], {"reminder-1"}, False),  # a turn of Claude's own (background work)
    (["reminder-1"], set(), False),
])
def test_a_result_that_only_answers_reminders_never_replaces_the_outcome(answered: list[str], reminders: set[str],
                                                                         only: bool) -> None:
    assert answers_only_reminders({"type": "result", "user_message_uuids": answered}, reminders) is only


def test_the_room_section_names_the_seat_and_the_reviewers_own_rules() -> None:
    work, review = room_section(WORKER), room_section(REVIEWER)
    assert "You are worker@s1." in work and "Do not message Reviewers" in work
    assert "You are reviewer@s1." in review and "Do not message Reviewers" not in review


def test_the_reply_contract_and_the_prompts_are_unchanged_with_the_room_off() -> None:
    from looper.agents.agent_settings import DEFAULT_HARNESS_PROMPT, Harness, Layer
    from looper.agents.system_prompt import WORK_CONTRACT, build_system_prompt
    from looper.agents.tiers.planner_protocol import REPLY_CONTRACT, reply_contract

    for gates in (False, True):
        assert reply_contract(2, gates=gates) == reply_contract(2, gates=gates, room=False)
        assert "[ROOM]" not in reply_contract(2, gates=gates)
        assert "[ROOM]: messages for you" in reply_contract(2, gates=gates, room=True)
    assert reply_contract() == REPLY_CONTRACT
    arguments = dict(harness=Harness(DEFAULT_HARNESS_PROMPT, Layer.BUILTIN), role_id="worker", role="Do the step.", recommended=True,
                     contract_name="WORK_CONTRACT", contract=WORK_CONTRACT, task_kind="work", task="## Task\nx")
    off, on = build_system_prompt(**arguments), build_system_prompt(**arguments, room=room_section(WORKER))
    assert build_system_prompt(**arguments, room="").text == off.text and "room" not in off.layers
    assert on.text.index("## The task room") > on.text.index("## Task") and "room" in on.layers
    assert on.text.index("## The task room") < on.text.index("## Your step and its report")


def test_a_delivery_waits_for_tools_and_respects_addressed_and_interface_intervals(tmp_path, monkeypatch, agent_db):
    from looper.agents.runtimes import StreamChannel
    from looper.agents.tiers import worker

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        written = []
        clock = 1000.0
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock)

        class Runtime:
            async def write_turn(self, stdin, text, *, uuid=None):
                written.append((text, uuid))

        next_delivery = AsyncMock(return_value=RoomDelivery("one", 41, (41,), True, 0))
        confirm = AsyncMock()
        pool._inputs["w1"] = StreamChannel(Runtime(), SimpleNamespace(stdin=object()), model=None)
        pool._active["w1"] = SimpleNamespace(parent_task_id="task")
        pool._reminders["w1"] = state = worker._Reminders(next_delivery=next_delivery, confirm=confirm)
        assert not await pool.remind("w1")
        next_delivery.assert_not_awaited()
        state.tools_running = 1
        assert await pool._flush_reminder("w1")
        assert written[0][0] == "one" and re.fullmatch(r"looper-reminder-[0-9a-f]{12}-41", written[0][1])
        assert pool.room_in_flight("w1") == 41
        confirm.assert_not_awaited()
        activity = agent_db.insert_activity.await_args.kwargs
        assert activity["summary"] == "Room messages delivered: #41"
        assert activity["metadata"] == {"room_delivery": {"ids": [41], "through": 41, "chars": 3}}
        next_delivery.return_value = RoomDelivery("two", 42, (42,), True, 0)
        clock += 5
        assert not await pool.remind("w1") and state.retry is not None
        state.last_at -= 11
        assert await pool._flush_reminder("w1")
        next_delivery.assert_awaited_with(41)
        next_delivery.return_value = RoomDelivery("three", 43, (43,), False, 0)
        clock += 30
        assert not await pool.remind("w1")
        state.last_at -= 31
        assert await pool._flush_reminder("w1")
        assert [text for text, _ in written] == ["one", "two", "three"]
        assert not await pool.remind("w2")
        retry = state.retry
        pool._drop_reminders("w1")
        await asyncio.gather(retry, return_exceptions=True)
        assert retry.cancelled() and pool.room_in_flight("w1") == 0

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["rejected", "unknown", "empty", "error", "concurrent"])
def test_delivery_failures_and_concurrent_flushes(tmp_path, outcome):
    from looper.agents.runtimes.channel import DeliveryResult
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool, _Reminders

    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        async def next_delivery(after):
            await asyncio.sleep(0)
            return RoomDelivery("body", 42, (42,), True, 0) if after < 42 else None

        select = AsyncMock(side_effect=next_delivery)
        channel = SimpleNamespace(send=AsyncMock(return_value=DeliveryResult.ACCEPTED))
        pool._inputs["w1"] = channel
        pool._reminders["w1"] = state = _Reminders(next_delivery=select, tools_running=1)
        if outcome == "rejected":
            channel.send.side_effect = BrokenPipeError()
        elif outcome == "unknown":
            channel.send.return_value = DeliveryResult.UNKNOWN
        elif outcome == "empty":
            select.side_effect = None
            select.return_value = None
        elif outcome == "error":
            select.side_effect = OSError("database unavailable")
        if outcome == "concurrent":
            assert sum(await asyncio.gather(pool.remind("w1"), pool.remind("w1"))) == 1
            channel.send.assert_awaited_once()
        else:
            assert not await pool.remind("w1")
        if outcome in ("rejected", "error"):
            assert state.through == 0 and not state.uuids and state.pending
        elif outcome == "unknown":
            assert state.through == 42 and state.sent == 0
            assert not await pool.remind("w1")
            select.assert_awaited_with(42)
            channel.send.assert_awaited_once()
        elif outcome == "empty":
            assert not state.pending
            channel.send.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("paused_at", ["empty_read", "read", "send", "activity"])
@pytest.mark.parametrize("waiting", [False, True])
def test_a_post_during_delivery_is_not_lost(tmp_path, monkeypatch, agent_db, paused_at, waiting):
    from looper.agents.runtimes.channel import DeliveryResult
    from looper.agents.tiers import worker

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        entered, release, posted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(worker, "ADDRESSED_INTERVAL_SEC", 0)
        written, afters = [], []

        async def pause():
            entered.set()
            await release.wait()

        async def next_delivery(after):
            afters.append(after)
            if len(afters) == 1:
                if paused_at in ("empty_read", "read"):
                    await pause()
                return None if paused_at == "empty_read" else RoomDelivery("first", 41, (41,), True, 0)
            return RoomDelivery("second", 42, (42,), True, 0) if after < 42 else None

        async def send(text, **kwargs):
            written.append(text)
            if paused_at == "send" and len(written) == 1:
                await pause()
            return DeliveryResult.ACCEPTED

        if paused_at == "activity":
            pool._active["w1"] = SimpleNamespace(parent_task_id="task")

            async def activity(**kwargs):
                if len(written) == 1:
                    await pause()

            agent_db.insert_activity.side_effect = activity
        pool._inputs["w1"] = SimpleNamespace(send=send)
        pool._reminders["w1"] = state = worker._Reminders(
            next_delivery=next_delivery, tools_running=int(not waiting), waiting=waiting)
        first = asyncio.create_task(pool.remind("w1"))
        await entered.wait()

        async def post():
            posted.set()
            return await pool.remind("w1")

        second = asyncio.create_task(post())
        await posted.wait()
        release.set()
        await asyncio.gather(first, second)
        assert written == (["second"] if paused_at == "empty_read" else ["first", "second"])
        assert afters[:2] == [0, 0 if paused_at == "empty_read" else 41]
        assert state.through == 42 and not state.pending
        pool._drop_reminders("w1")

    asyncio.run(asyncio.wait_for(run(), 3))


def test_pending_delivery_retries_when_the_interval_ends(tmp_path, monkeypatch):
    from looper.agents.tiers import worker
    from looper.agents.runtimes.channel import DeliveryResult

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        released, sleeping = asyncio.Event(), asyncio.Event()
        delays = []
        async def sleep(delay):
            delays.append(delay)
            sleeping.set()
            await released.wait()
        monkeypatch.setattr(worker.asyncio, "sleep", sleep)
        delivery = RoomDelivery("body", 42, (42,), True, 0)
        pool._inputs["w1"] = channel = SimpleNamespace(send=AsyncMock(return_value=DeliveryResult.ACCEPTED))
        pool._reminders["w1"] = state = worker._Reminders(
            next_delivery=AsyncMock(return_value=delivery), waiting=True, last_at=worker.time.monotonic())
        assert not await pool.remind("w1")
        await sleeping.wait()
        assert 0 < delays[0] <= 10
        state.last_at -= 11
        retry = state.retry
        released.set()
        await retry
        channel.send.assert_awaited_once()
        assert not state.pending and state.retry is None

    asyncio.run(asyncio.wait_for(run(), 3))


def test_room_deliveries_include_visible_bodies_refs_and_continuations(monkeypatch):
    from looper.agents.room import db_room

    async def run():
        rows = [{**_message("fe_rd@s2", "question", to=(WORKER.key,), id=41), "body": "Which file?\nPlease answer."},
                {**_message("fe_rd@s2", "interface", everyone=True, id=42), "body": "Use port 8123",
                 "refs": [{"file": "api.md"}, {"commit": "abc1234"}], "reply_to": 40}]
        monkeypatch.setattr(RoomService, "enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(db_room, "last_read", AsyncMock(return_value=0))
        monkeypatch.setattr(db_room, "messages", AsyncMock(return_value=rows))
        delivery = await RoomService().delivery("task", WORKER)
        assert delivery is not None
        assert delivery.ids == (41, 42) and delivery.through == 42 and delivery.addressed and delivery.more == 0
        assert "question, for you): Which file?\n  Please answer." in delivery.text
        assert delivery.text.index("#41") < delivery.text.index("#42")
        assert "re #40; files: api.md; commits: abc1234): Use port 8123" in delivery.text

    asyncio.run(run())


@pytest.mark.parametrize("case", ["info", "clipped", "count", "size", "visibility", "after", "off"])
def test_room_delivery_limits_visibility_and_trigger(monkeypatch, case):
    from looper.agents.room import db_room

    async def run():
        rows = [{**_message("fe_rd@s2", to=(WORKER.key,), id=i), "body": "port 8123"} for i in range(1, 13)]
        last_read = 0
        if case == "info":
            rows = [_message("fe_rd@s2", everyone=True)]
        elif case == "clipped":
            rows = [{**rows[0], "body": "a" * 3000},
                    {**_message("fe_rd@s2", everyone=True, id=2), "body": "b" * 1000}]
        elif case == "size":
            rows = [{**r, "body": "x" * 1500} for r in rows]
        elif case == "visibility":
            rows = [rows[0], _message("fe_rd@s2", to=("worker@s3",), id=2), rows[2],
                    _message(WORKER.key, everyone=True, id=4), _message("fe_rd@s2", to=("worker@s3",), id=5)]
        elif case == "after":
            last_read = 2
        monkeypatch.setattr(RoomService, "enabled", AsyncMock(return_value=case != "off"))
        monkeypatch.setattr(db_room, "last_read", AsyncMock(return_value=last_read))
        async def select(task, *, after):
            return [r for r in rows if r["id"] > after]
        monkeypatch.setattr(db_room, "messages", select)
        delivery = await RoomService().delivery("task", WORKER, after=7 if case == "after" else 0)
        if case in ("off", "info"):
            assert delivery is None
        elif case == "clipped":
            assert "a" * 1500 + " … (clipped; read it whole with looper_room_read about=1)" in delivery.text
            assert "a" * 1501 not in delivery.text and "b" * 301 not in delivery.text
            assert "b" * 300 in delivery.text and "looper_room_read about=2" in delivery.text
        elif case == "count":
            assert delivery.ids == tuple(range(1, 11)) and delivery.through == 10 and delivery.more == 2
            assert "2 more unread" in delivery.text
        elif case == "size":
            assert len(delivery.text) <= 4000 and delivery.ids == (1, 2) and delivery.more == 10
        elif case == "visibility":
            assert delivery.ids == (1, 3) and delivery.through == 5
        elif case == "after":
            assert delivery.ids == tuple(range(8, 13)) and delivery.through == 12

    asyncio.run(run())


@pytest.mark.parametrize("runtime,input_enabled,expected", [("claude-code", True, "user_turn"),
                                                          ("claude-code", False, "none"), ("codex", False, "none")])
def test_room_push_requires_an_opted_in_runtime_with_input(monkeypatch, runtime, input_enabled, expected):
    from looper.agents.runtimes import ADAPTERS, room_push
    adapter = ADAPTERS[runtime]
    monkeypatch.setattr(adapter, "run_input", input_enabled)
    assert room_push(SimpleNamespace(runtime=adapter)).value == expected


def test_kept_run_sessions_go_with_their_task(tmp_path: Any) -> None:
    from looper.agents.room import remove_run_sessions, run_session_dir, sweep_run_sessions

    assert run_session_dir(tmp_path, "claude-code", "task-a", "worker-1") is None  # it keeps its own
    dirs = {(kind, task): run_session_dir(tmp_path, kind, task, "worker-1")
            for kind in ("codex", "omp", "pi") for task in ("task-a", "task-b", "task-c")}
    for directory in dirs.values():
        assert directory is not None
        directory.mkdir(parents=True)
    remove_run_sessions(tmp_path, "task-a")
    assert not any(dirs[(kind, "task-a")].exists() for kind in ("codex", "omp", "pi"))
    removed = sweep_run_sessions(tmp_path, keep=["task-b"])
    assert sorted(p.name for p in removed) == ["task-c", "task-c", "task-c"]
    assert all(dirs[(kind, "task-b")].exists() for kind in ("codex", "omp", "pi"))


def test_a_pi_room_run_persists_its_session(tmp_path: Any) -> None:
    import json
    import subprocess

    from fake_claude import fake_claude
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import LegacyProviderSpec as ProviderSpec, RuntimeSpec
    from tests.runtime_model_helpers import resolved_profile
    from looper.agents.room import run_session_dir
    from looper.agents.runtimes import ApiProtocol, AuthKind
    from looper.agents.runtimes.pi import PiRuntime

    runtime = PiRuntime()
    launch = Launch(AgentSettings(profile=resolved_profile(
        "pi-test", "pi-test", RuntimeSpec("pi", "pi", "pi"),
        ProviderSpec("test", "test", auth=AuthKind.API_KEY, api_key="test-key",
                     protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}), base_url="https://pi.example.invalid/v1"),
        "selected")))
    prompt = tmp_path / ".local" / "prompts" / "test.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("worker instructions")
    executable = fake_claude(tmp_path / "pi", '''
import json, os, pathlib, sys
args = sys.argv[1:]
assert '--no-session' not in args
directory = pathlib.Path(args[args.index('--session-dir') + 1])
assert os.environ['PI_CODING_AGENT_SESSION_DIR'] == str(directory)
text = sys.stdin.read()
(directory / 'timestamp_worker-session.jsonl').write_text(
    json.dumps({'type': 'session', 'id': 'worker-session'}) + '\\n' + json.dumps({'text': text}) + '\\n')
''')
    directory = run_session_dir(tmp_path, "pi", "task-1", "worker-1")
    assert directory is not None
    command = runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch, session_dir=directory)
    subprocess.run(command, cwd=tmp_path, env=launch.environment(), input="work item", text=True, check=True,
                   capture_output=True, timeout=10)
    saved = list(directory.glob("*.jsonl"))
    assert len(saved) == 1
    assert [json.loads(line) for line in saved[0].read_text().splitlines()] == [
        {"type": "session", "id": "worker-session"}, {"text": "work item"}]


def test_a_reviewer_answers_only_the_planner() -> None:
    for targets in ([TO_WORK], [TO_PLANNER, TO_WORK]):
        with pytest.raises(OpError, match="only the planner") as caught:
            check_scope(REVIEWER, MessageKind.ANSWER, everyone=False, targets=targets,
                        reply=QUESTION_FROM_PLANNER_TO_REVIEWER)
        assert caught.value.code is ErrorCode.FORBIDDEN
    check_scope(REVIEWER, MessageKind.ANSWER, everyone=False, targets=[TO_PLANNER],
                reply=QUESTION_FROM_PLANNER_TO_REVIEWER)


def test_inbox_subscribes_before_its_query(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from looper.agents import room as module
    from looper.agents.project_execution import RoomLimits

    async def run() -> None:
        room = module.RoomService()
        calls = 0
        message = _message("worker@s1", to=(FE.key,))

        async def context(*args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(limits=RoomLimits())

        async def last_read(*args: Any, **kwargs: Any) -> int:
            return 0

        async def messages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                # The SELECT's empty result is ready when a post commits and sends its wake.
                room._wake_inboxes("task")
                return []
            return [message]

        monkeypatch.setattr(room, "_context", context)
        monkeypatch.setattr(module.db_room, "last_read", last_read)
        monkeypatch.setattr(module.db_room, "messages", messages)
        result = await asyncio.wait_for(room.inbox("task", FE, wait_seconds=2, peek=True), timeout=0.3)
        assert [m["id"] for m in result["messages"]] == [1]

    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["addressed", "role", "interface", "confirmed", "during_query"])
def test_an_inbox_wait_returns_ids_for_messages_pushed_into_its_run(monkeypatch, delivery):
    from looper.agents import room as module
    from looper.agents.project_execution import RoomLimits

    async def run():
        cursor, read = 0, 0
        rows = []
        queried = asyncio.Event()
        room = RoomService(worker_pool=SimpleNamespace(room_in_flight=lambda run: cursor))
        monkeypatch.setattr(room, "_context", AsyncMock(return_value=SimpleNamespace(limits=RoomLimits())))
        monkeypatch.setattr(module.db_room, "last_read", AsyncMock(side_effect=lambda *args: read))
        mark = AsyncMock()
        monkeypatch.setattr(module.db_room, "mark_read", mark)

        async def messages(task, *, after=0):
            nonlocal cursor, read
            queried.set()
            if rows and delivery == "during_query":
                cursor = rows[-1]["id"]
            return [r for r in rows if r["id"] > after]

        monkeypatch.setattr(module.db_room, "messages", messages)
        waiting = asyncio.create_task(room.inbox("task", FE, wait_seconds=5))
        try:
            await queried.wait()
            # Neither an unrelated broadcast nor another seat's private message ends the wait.
            rows.extend([_message(WORKER.key, everyone=True, id=1),
                         _message(WORKER.key, to=(PLANNER.key,), id=2)])
            cursor = 2
            queried.clear()
            room._wake_inboxes("task")
            await queried.wait()
            assert not waiting.done()
            rows.append(_message(WORKER.key, "interface" if delivery == "interface" else "question",
                                 to=() if delivery == "interface" else (FE.role if delivery == "role" else FE.key,),
                                 everyone=delivery == "interface", id=3))
            if delivery != "during_query":
                cursor = 3
            if delivery == "confirmed":
                read = 3  # Claude's echo may mark it read before the wait resumes.
            room._wake_inboxes("task")
            result = await asyncio.wait_for(waiting, 0.5)
            assert result["messages"] == [] and result["more"] is False and result["waited"]
            assert result["delivered_message_ids"] == [3]
            assert "separate user messages" in result["note"]
            assert result["unread"] == {"for_you": 0, "interface": 0, "room": 0}
            mark.assert_not_awaited()  # Writing the delivery never marks it read.
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 2))


def test_reference_lengths_and_inbox_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from looper.agents import room as module
    from looper.agents.project_execution import RoomLimits

    async def run() -> None:
        room = module.RoomService()
        with pytest.raises(OpError, match=str(module.REF_MAX_CHARS)) as caught:
            await room._refs(WORKER, ["x" * (module.REF_MAX_CHARS + 1)], [])
        assert caught.value.code is ErrorCode.INVALID
        assert await room._refs(WORKER, ["x" * module.REF_MAX_CHARS], [])
        rows = [{**_message(WORKER.key, to=(FE.key,), id=i), "body": "x" * 20,
                 "refs": [{"file": "f" * 40}]} for i in (1, 2)]

        async def context(*args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(limits=RoomLimits())

        async def last_read(*args: Any, **kwargs: Any) -> int:
            return 0

        async def messages(*args: Any, **kwargs: Any) -> Any:
            return rows

        monkeypatch.setattr(room, "_context", context)
        monkeypatch.setattr(module.db_room, "last_read", last_read)
        monkeypatch.setattr(module.db_room, "messages", messages)
        monkeypatch.setattr(module, "INBOX_MAX_CHARS", 100)
        result = await room.inbox("task", FE, peek=True)
        assert [m["id"] for m in result["messages"]] == [1]
        assert result["more"]

    asyncio.run(run())


def test_delayed_planner_wakes_are_deduplicated() -> None:
    import asyncio

    from looper.agents.room import RoomService
    from looper.agents.message import MessageType, planner_address

    async def run() -> None:
        sent: list[Any] = []

        class Bus:
            async def publish(self, envelope: Any) -> None:
                sent.append(envelope)

        room = RoomService(bus=Bus())
        room.wake_planner_after("task", 0.01)
        wake = room._wakes["task"]
        room.wake_planner_after("task", 0.01)
        assert room._wakes["task"] is wake
        await asyncio.wait_for(wake, 0.3)
        assert len(sent) == 1 and sent[0].message_type is MessageType.ROOM_MESSAGE
        assert sent[0].target == planner_address("task")
        assert not room._wakes

    asyncio.run(run())


@pytest.mark.parametrize('value,expected', [
    ('looper-reminder-0123456789ab-42', 42), ('looper-reminder-0123456789ab-0', 0),
    ('looper-reminder-0123456789ab', None), ('looper-reminder-0123456789AB-42', None),
    ('looper-reminder-0123456789ab-42-extra', None), (None, None),
])
def test_delivery_cursor_requires_a_complete_delivery_uuid(value, expected):
    from looper.agents.tiers.worker import uuid_through
    assert uuid_through(value) == expected


def test_confirmation_failure_does_not_interrupt_the_stream(tmp_path, caplog):
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool, _Reminders

    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        state = _Reminders(confirm=AsyncMock(side_effect=OSError('database unavailable')))
        await pool._confirm_delivery('w1', state, 42)
        await pool._confirm_delivery('w1', state, 42)
        state.confirm.assert_awaited_once_with(42)
        assert 'failed to confirm room delivery through 42' in caplog.text

    asyncio.run(run())
