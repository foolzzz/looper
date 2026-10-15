"""What a task captures as it happens, and what a new session gets back (M7c).

The task journal records failures and retries with their learnings, the
Planner's decisions with their reasons, the supplements and new
sessions with the handoff before them; the Planner's turns and a Worker's
first turn carry it back, supplements first and whole. When the
Planner's or the Manager's agent can no longer resume its session, the old
session hands over while it runs, and the new session's first turn starts
with a resume brief; without a usable old session, the brief comes from the
saved state alone. A learning a Worker or Reviewer says holds for the whole
project becomes a draft of project knowledge when it arrives.

Claude is a stand-in throughout (``planner_claude`` and the Manager's
``_resident``); a forced agent change goes through ``session_switch.agent_switch``,
the one place that decides it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import session_switch
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.session_switch import SessionSwitch, SwitchReason
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.test_manager_reply import WEB, _fake_claude, _resident
from tests.test_planner_agent import (
    REVIEW_PASS,
    WORK_FAILED,
    WORK_OK,
    Harness,
    activity,
    always,
    chat,
    decide,
    drive,
    hold_first,
    plan,
    reply,
    step,
)
from tests.test_project_execution import _project, _spend_from_records

PITFALL = "the parser chokes on tabs"
INSTRUCTION = "Keep the public API unchanged.\nAnd use pnpm, not npm."


def _journal(agent_db: Any) -> list[tuple[str, str, str, list[str]]]:
    """The journal entries the Planner saved, in order: (kind, step label or '', summary, learnings)."""
    keys = {step_id: row["key"] for step_id, row in agent_db.store.steps.get("t1", {}).items()}
    return [(e["kind"], keys.get(e["step_id"], ""), e["summary"], e["learnings"])
            for e in agent_db.store.journal.get("t1", [])]


def _failed(**extra: Any) -> dict[str, Any]:
    return {**WORK_FAILED, "learnings": [PITFALL], **extra}


def _texts(claude: Any) -> list[str]:
    return [turn["text"] for turn in claude.turns()]


# --- Each kind is written when its event happens ---


def _by_attempt(*results: dict[str, Any]) -> Callable[[MessageEnvelope], dict[str, Any]]:
    """Step s1's attempts get ``results`` in turn; every other step succeeds."""

    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.payload["step_key"] != "s1":
            return WORK_OK
        return results[min(envelope.payload["attempt"], len(results)) - 1]

    return outcome


async def _chat_while_s1_runs(h: Harness) -> None:
    await h.start()
    await h.until(lambda: h.held)
    await h.say(INSTRUCTION)
    await h.release(WORK_OK)
    await h.finished()


@pytest.mark.usefixtures("agent_db")
class TestJournalKinds:
    @pytest.mark.parametrize(("replies", "outcome", "scenario", "expected"), [
        pytest.param(
            (plan(step("s1")), decide("retry", reason="tests fail", feedback="fix the parser"), decide("accept")),
            _by_attempt(_failed(), WORK_OK), None,
            [("step_retry", "s1", ["attempt 1 failed: tests fail", "Retry: fix the parser"], [PITFALL]),
             ("step_accepted", "s1", ["did it"], [])],
            id="retry"),
        pytest.param(
            (plan(step("s1")), decide("fail", reason="the API cannot be reached")),
            always(_failed()), None,
            [("step_failed", "s1", ["failed: tests fail", "The Planner failed the task: the API cannot be reached"],
              [PITFALL])],
            id="fail"),
        pytest.param(
            (plan(step("s1")), "<error>"),
            always(_failed()), None,
            [("step_failed", "s1", ["failed: tests fail", "could not decide"], [PITFALL])],
            id="fail-without-a-decision"),
        pytest.param(
            (plan(step("s1"), step("s2", "s1")),
             decide("replan", reason="the parser approach is wrong", steps=[step("s3")]), decide("accept")),
            _by_attempt(_failed()), None,
            [("decision", "s1", ["replan: the parser approach is wrong", "s3", "dropped s2",
                                 "The step was dropped (failed: tests fail)"], [PITFALL]),
             ("step_accepted", "s3", ["did it"], [])],
            id="replan"),
        pytest.param(
            (plan(step("s1")), chat("Will do.", "add_steps", steps=[step("s2", "s1")]), decide("accept"),
             decide("accept")),
            hold_first(), _chat_while_s1_runs,
            [("supplement", "", [INSTRUCTION], []),
             ("decision", "", ["added steps s2 (after s1)", "Will do."], []),
             ("step_accepted", "s1", ["did it"], []), ("step_accepted", "s2", ["did it"], [])],
            id="supplement-and-added-steps"),
    ])
    def test_each_kind_is_written_when_its_event_happens(
        self, planner_claude: Any, agent_db: Any, replies: tuple[str, ...], outcome: Any, scenario: Any,
        expected: list[tuple[str, str, list[str], list[str]]],
    ) -> None:
        drive(planner_claude(*replies), outcome, scenario)
        saved = _journal(agent_db)
        assert [(kind, label) for kind, label, _, _ in saved] == [(kind, label) for kind, label, _, _ in expected]
        for (_, _, summary, learnings), (_, _, parts, expected_learnings) in zip(saved, expected):
            assert all(part in summary for part in parts), summary
            assert learnings == expected_learnings

    def test_a_task_stopped_at_its_spend_cap_journals_the_decision(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent_db.get_task_project.return_value = _project(task_budget_usd=0.4, task_budget_action="wrap_up")
        _spend_from_records(monkeypatch, agent_db)
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("accept"),
                                decide("fail", reason="s1 is done; s2 remains"))
        drive(claude, always(WORK_OK))
        (*_, (kind, label, summary, _)) = _journal(agent_db)
        assert (kind, label) == ("decision", "")
        assert summary.startswith("stopped at the spend cap: Task spend cap reached")
        assert summary.endswith("Planner's wrap-up: s1 is done; s2 remains")


# --- What the turns carry back ---


def _big(key: str) -> dict[str, Any]:
    """A successful result whose five 300-character learnings make its entry about 1,550 characters."""
    return {**WORK_OK, "summary": f"{key} done", "learnings": [f"{key}-tip{n} " + "x" * 290 for n in range(5)]}


@pytest.mark.usefixtures("agent_db")
class TestInjection:
    def test_supplements_come_first_and_whole_and_pitfalls_outlive_newer_entries(
        self, planner_claude: Any,
    ) -> None:
        """Three big accepted steps overflow the progress cap; a long supplement and an old retry stay."""
        instruction = "\n".join(f"line {n}: " + "y" * 240 for n in range(10))  # about 2,500 characters
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1"), step("s3", "s2"), step("s4", "s3")),
            chat("Noted."), decide("retry", reason="tests fail", feedback="fix the parser"),
            decide("accept"), decide("accept"), decide("accept"), decide("accept"),
        )

        def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
            key, attempt = envelope.payload["step_key"], envelope.payload["attempt"]
            return None if (key, attempt) == ("s1", 1) else _big(key)

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await h.say(instruction)
            await h.release(_failed())
            await h.finished()

        h = drive(claude, outcome, scenario)
        decision_on_s4 = _texts(claude)[-1]
        assert decision_on_s4.startswith("[DECISION] Step s4")
        # The supplement is whole, every line of it, before the journal, outside its cap.
        instructions = decision_on_s4[decision_on_s4.index("Supplements"):
                                      decision_on_s4.index("Progress and findings")]
        assert "- " + instruction.replace("\n", "\n  ") in instructions
        progress = decision_on_s4[decision_on_s4.index("Progress and findings"):
                                  decision_on_s4.index("Result of step s4")]
        assert len(progress) <= 4_000
        assert "(1 entry left out)" in progress.splitlines()[0]
        # The retry is the oldest entry yet stays, under its heading before the accepted steps; of these, the
        # oldest (s1) is the one left out.
        assert progress.index("Retried attempts:") < progress.index(PITFALL) < progress.index("Accepted steps:")
        assert progress.index("- s3: s3 done") < progress.index("- s2: s2 done")
        assert "s1-tip" not in progress
        # A later Worker has the supplement whole in its task section, and its dependency before the pitfall in
        # its first turn's findings.
        work_item = next(w for w in h.work_items if w.payload["step_key"] == "s4").payload
        assert work_item["supplements"] == [instruction]
        assert "line 0" not in work_item["findings"]
        assert work_item["findings"].index("- s3: s3 done") < work_item["findings"].index(PITFALL)


# --- A new Planner session: the handoff and the resume brief ---


class _Switch:
    """``session_switch.profile_switch`` that also reports one profile change when ``armed``, as when a layer moves
    the profile to another runtime or provider."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.armed = False
        real = session_switch.profile_switch

        def profile_switch(session_id: str | None, runtime: str | None, profile: str | None, launch: Any,
                           **kwargs: Any) -> Any:
            if self.armed and session_id:
                self.armed = False
                return SessionSwitch(SwitchReason.PROFILE_CHANGED,
                                     "a codex process cannot resume a claude-code session",
                                     old_runtime="claude-code", old_profile=profile, new_runtime="codex",
                                     new_profile="agent-codex")
            return real(session_id, runtime, profile, launch, **kwargs)

        monkeypatch.setattr(session_switch, "profile_switch", profile_switch)


def _kill(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


@pytest.mark.usefixtures("agent_db")
class TestNewPlannerSession:
    @staticmethod
    def _scenario(claude: Any, before_s1_retry_result: Callable[[Harness], Any]) -> Any:
        """Plan s1 and s2 (after s1); the user sends an instruction while s1 runs; s1 fails with a pitfall and is
        retried; ``before_s1_retry_result`` runs while the retry is held; then the task completes."""

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: len(h.held) == 1)
            await h.say(INSTRUCTION)
            await h.release(_failed())
            await h.until(lambda: len(h.held) == 1)  # the retry runs
            await before_s1_retry_result(h)
            await h.release(WORK_OK)
            await h.finished()

        return scenario

    def test_a_changed_agent_gets_the_old_sessions_handoff_and_a_resume_brief(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        switch = _Switch(monkeypatch)
        handoff = "HANDOFF-NOTES: tests live in tests/ and run with pytest -q; the tokenizer is still suspect."
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")), chat("Noted."),
            decide("retry", reason="tests fail", feedback="fix the parser"),
            reply({"handoff": handoff}), decide("accept"), decide("accept"),
        )

        async def arm(h: Harness) -> None:
            switch.armed = True

        h = drive(claude, hold_first_two(), self._scenario(claude, arm))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        turns = claude.turns()
        handoff_turn = next(t for t in turns if t["text"].startswith("[HANDOFF]"))
        first_pid, second_pid = dict.fromkeys(t["pid"] for t in turns)
        assert handoff_turn["pid"] == first_pid  # the old session wrote it, while its Claude ran
        assert "agent changed from claude-code" in handoff_turn["text"]
        # The new session does not resume the old one; its first turn starts with the brief, once.
        assert [_resume_flag(argv) for argv in claude.argvs()] == [None, None]
        first_new, *later = [t["text"] for t in turns if t["pid"] == second_pid]
        _assert_brief(first_new, handoff=handoff)
        assert not any(text.startswith("[RESUME]") for text in later)
        assert [kind for kind, *_ in _journal(agent_db)] == [
            "supplement", "step_retry", "handoff", "session_switch", "step_accepted", "step_accepted"]
        assert any("The Planner starts a new session (agent changed" in line for line in activity(agent_db))

    @pytest.mark.parametrize("unusable", ["handoff-fails", "process-gone", "session-lost"])
    def test_without_a_usable_old_session_the_brief_comes_from_the_journal_alone(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch, unusable: str,
    ) -> None:
        switch = _Switch(monkeypatch)
        replies = [plan(step("s1"), step("s2", "s1")), chat("Noted."),
                   decide("retry", reason="tests fail", feedback="fix the parser")]
        if unusable == "handoff-fails":
            replies.append("<error>")  # the old provider is down: the handoff turn fails
        claude = planner_claude(*replies, decide("accept"), decide("accept"),
                                forget_sessions=unusable == "session-lost")

        async def before(h: Harness) -> None:
            if unusable != "handoff-fails":
                # The old session's Claude is gone before the next turn.
                _kill(h.planner._claude.pid)  # type: ignore[arg-type]
                await h.until(lambda: h.planner._claude.stale(h.planner._process.signature))  # type: ignore[union-attr]
            switch.armed = unusable != "session-lost"

        h = drive(claude, hold_first_two(), self._scenario(claude, before))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        texts = _texts(claude)
        briefs = [text for text in texts if text.startswith("[RESUME]")]
        assert len(briefs) == 1
        _assert_brief(briefs[0], handoff=None)
        assert ("session lost" in briefs[0]) is (unusable == "session-lost")
        kinds = [kind for kind, *_ in _journal(agent_db)]
        assert "handoff" not in kinds and kinds.count("session_switch") == 1


def hold_first_two() -> Callable[[MessageEnvelope], dict[str, Any] | None]:
    """Hold s1's first attempt and its retry, for the scenario to release; every other step succeeds."""

    def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
        return None if envelope.payload["step_key"] == "s1" else WORK_OK

    return outcome


def _resume_flag(argv: list[str]) -> str | None:
    return argv[argv.index("--resume") + 1] if "--resume" in argv else None


def _assert_brief(text: str, *, handoff: str | None) -> None:
    """The first turn of a new session: the brief, then the request with the instruction and the pitfall."""
    assert text.startswith("[RESUME] You continue this task in a new session")
    brief, request = text.split("\n\n[DECISION] ", 1)
    if handoff:
        assert handoff in brief
    else:
        assert "Your previous session wrote no handoff" in brief
    assert "Where the task is: 0 of 2 steps accepted, 1 under way, 0 failed." in brief
    assert request.index("Supplements") < request.index(INSTRUCTION.splitlines()[0]) \
        < request.index("Progress and findings") < request.index(PITFALL)


# --- The Manager's new sessions ---


CHATS = [
    {"session_id": "session-b", "messages": [
        {"role": "user", "content": "Is the staging deploy done?", "created_at": None},
        {"role": "assistant", "content": "Yes, it finished at 10:20.", "created_at": None}]},
    {"session_id": "session-a", "messages": [
        {"role": "user", "content": "Plan the calc refactor with me.", "created_at": None},
        {"role": "assistant", "content": "Sure: parser first, then the CLI.", "created_at": None},
        {"role": "user", "content": "hello", "created_at": None}]},  # the message the first turn answers
]


def _manager_turns(
    monkeypatch: pytest.MonkeyPatch, agent_db: Any, messages: list[str], *,
    before: Callable[[int], None] | None = None,
) -> list[str]:
    """Send ``messages`` (from the web chat ``session-a``) to one Manager backed by ``_resident``; returns what
    each of its runs was given (``input_text``), handoff turns included."""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(config=ManagerConfig(claude_bin=_fake_claude(root, _resident()), work_dir=root),
                                   runtime_root=root)
            manager._bus = bus
            for index, text in enumerate(messages):
                if before is not None:
                    before(index)
                await manager._handle(MessageEnvelope.create(
                    source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": text, "channel_context": {"session_id": "session-a"}},
                    correlation_id=f"corr-{index}",
                ))
            await manager.stop()
            await bus.shutdown()

    asyncio.run(_run())
    return [str(call.kwargs["input_text"]) for call in agent_db.start_record.await_args_list]


class TestManagerContinuity:
    @pytest.mark.parametrize("saved", [None, "sess-1"])
    def test_a_new_session_starts_with_the_recent_chats_and_a_resumed_one_without(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, saved: str | None,
    ) -> None:
        agent_db.recent_conversations.return_value = CHATS
        if saved:
            agent_db.store.sessions["manager"] = {"session_id": saved, "session_totals": {},
                                                  "runtime_type": "claude-code", "profile_id": None}
        first, second = _manager_turns(monkeypatch, agent_db, ["hello", "again"])
        assert second == "again"
        if saved:
            assert first == "hello"
            return
        brief, message = first.rsplit("\n\n", 1)
        assert message == "hello"
        assert brief.startswith("[RESUME] This is a new session")
        # The most recent chat first, each oldest message first, without the message being answered.
        assert brief.index("Chat session-b") < brief.index("Is the staging deploy done?") \
            < brief.index("Yes, it finished") < brief.index("Chat session-a") \
            < brief.index("Plan the calc refactor") < brief.index("parser first, then the CLI")
        assert "- user: hello" not in brief
        assert agent_db.recent_conversations.await_args.kwargs == {"sessions": 3, "messages": 9}

    def test_the_recent_chats_are_capped(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        long = [{"role": "user", "content": f"m{n} " + "z" * 400, "created_at": None} for n in range(9)]
        agent_db.recent_conversations.return_value = [{"session_id": f"s{c}", "messages": long} for c in range(3)]
        (first,) = _manager_turns(monkeypatch, agent_db, ["hello"])
        assert len(first) < 4_000 + 200
        assert "older messages left out" in first
        assert "- user: m8 " in first and "Chat s0" in first and "Chat s2" not in first

    def test_a_changed_agent_hands_over_before_the_new_session(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        agent_db.recent_conversations.return_value = CHATS
        switch = _Switch(monkeypatch)

        def before(index: int) -> None:
            switch.armed = index == 1

        first, handoff, second = _manager_turns(monkeypatch, agent_db, ["hello", "again"], before=before)
        assert first.startswith("[RESUME]")  # the Manager's first session ever, with the recent chats
        assert handoff.startswith("[HANDOFF] This session ends here (agent changed from claude-code")
        assert second.startswith("[RESUME] This is a new session (agent changed from claude-code")
        # The old session's reply to the handoff turn (the stand-in echoes it) is in the brief.
        assert "Handoff from your previous session" in second and "turn 2: [HANDOFF]" in second
        assert "Chat session-b" in second and second.endswith("\n\nagain")


# --- Project-wide learnings become drafts when they arrive ---


@pytest.mark.usefixtures("agent_db")
class TestProjectLearnings:
    @pytest.mark.parametrize("mode", ["off", "draft", "write"])
    def test_project_learnings_are_drafted_when_the_project_distills_lessons(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch, mode: str,
    ) -> None:
        from looper.agents import knowledge

        add_draft = AsyncMock(return_value={})
        monkeypatch.setattr(knowledge, "add_draft", add_draft)
        monkeypatch.setattr(knowledge, "add_document", AsyncMock(return_value={}))
        agent_db.get_task_project.return_value = _project(distill_lessons=mode)
        work = {**WORK_OK, "learnings": ["a task-only tip", {"text": "run make gen before building",
                                                             "scope": "project"}]}
        review = {**REVIEW_PASS, "learnings": [{"text": "the API needs a token", "scope": "project"}]}
        claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"),
                                reply({"lessons": "# Lessons"}))
        drive(claude, always(work, review))

        drafted = [call.kwargs for call in add_draft.await_args_list
                   if call.kwargs["title"].startswith("Learnings from step")]
        if mode == "off":
            assert drafted == []  # they stay the task's
        else:  # write drafts them too: a single learning is unreviewed
            assert [d["content"].splitlines()[2] for d in drafted] == [
                "- run make gen before building", "- the API needs a token"]
            assert [d["summary"].split(" of ")[0] for d in drafted] == ["What the Worker", "What the Reviewer"]
            assert all(d["task_id"] == "t1" for d in drafted)
        # Either way the task journal keeps every learning, and the decision turn marks the project-wide one.
        ((kind, _, _, learnings),) = _journal(agent_db)
        assert kind == "step_accepted"
        assert learnings == ["a task-only tip", "run make gen before building", "the API needs a token"]
        assert "- learning (holds for the whole project): run make gen before building" in _texts(claude)[1]


class TestLearningsContract:
    @pytest.mark.parametrize(("reported", "kept"), [
        (["the build needs make gen"], ["the build needs make gen"]),
        ([{"text": "use pnpm", "scope": "project"}, "plain", {"text": "no scope"}, {"text": "odd", "scope": "x"},
          {"scope": "project"}, 5, " "],
         [{"text": "use pnpm", "scope": "project"}, "plain", "no scope", "odd"]),
        ("not a list", []),
    ], ids=["plain-strings", "mixed", "malformed"])
    @pytest.mark.parametrize("is_verify", [False, True], ids=["report", "verdict"])
    def test_learnings_are_plain_strings_or_text_with_a_scope(
        self, tmp_path: Path, reported: Any, kept: list[Any], is_verify: bool,
    ) -> None:
        from looper.agents.tiers.worker import judge_result

        body = ({"passed": True, "findings": [], "summary": "ok", "learnings": reported} if is_verify else
                {"status": "completed", "changed_files": {}, "deleted_files": [], "summary": "ok",
                 "learnings": reported})
        event = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(body)}
        assert judge_result(event, work_dir=tmp_path, is_verify=is_verify)["learnings"] == kept
