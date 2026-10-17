"""The agent room's rules without a database: who may post what to whom, what a seat sees, who is online, when two
seats trade too long, which result a run's reminders never replace, and the prompts and contract with the room off."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from looper.agents.ops import ErrorCode, OpError
from looper.agents.room import (
    MemberKind,
    MessageKind,
    Presence,
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


def test_a_reminder_waits_for_a_tool_call_and_comes_at_most_once_a_minute(tmp_path: Any) -> None:
    import asyncio

    from looper.agents.tiers.worker import WorkerConfig, WorkerPool, _Reminders

    async def run() -> None:
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        written: list[tuple[str, str | None]] = []

        class Runtime:
            async def write_turn(self, stdin: Any, text: str, *, uuid: str | None = None) -> None:
                written.append((text, uuid))

        pool._inputs["w1"] = (object(), Runtime())  # type: ignore[assignment]
        pool._reminders["w1"] = state = _Reminders()
        assert not await pool.remind("w1", "one")  # no tool call runs: it waits
        state.tools_running = 1
        assert await pool._flush_reminder("w1")
        assert written[0][0] == "one" and str(written[0][1]).startswith("looper-reminder-")
        assert not await pool.remind("w1", "two")  # within the minute: it waits
        state.last_at -= 61
        assert await pool.remind("w1", "three")  # the newest replaced the one waiting
        assert [text for text, _ in written] == ["one", "three"] and state.uuids == {u for _, u in written}
        assert not await pool.remind("w2", "a run without a room")

    asyncio.run(run())


def test_kept_run_sessions_go_with_their_task(tmp_path: Any) -> None:
    from looper.agents.room import remove_run_sessions, run_session_dir, sweep_run_sessions

    assert run_session_dir(tmp_path, "claude-code", "task-a", "worker-1") is None  # it keeps its own
    dirs = {(kind, task): run_session_dir(tmp_path, kind, task, "worker-1")
            for kind in ("codex", "omp") for task in ("task-a", "task-b", "task-c")}
    for directory in dirs.values():
        assert directory is not None
        directory.mkdir(parents=True)
    remove_run_sessions(tmp_path, "task-a")
    assert not any(dirs[(kind, "task-a")].exists() for kind in ("codex", "omp"))
    removed = sweep_run_sessions(tmp_path, keep=["task-b"])
    assert sorted(p.name for p in removed) == ["task-c", "task-c"]
    assert all(dirs[(kind, "task-b")].exists() for kind in ("codex", "omp"))
