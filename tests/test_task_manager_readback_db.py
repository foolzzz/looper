"""The readback in the task Manager's turn: before the user message, again before every turn, never when orchestrated."""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from looper.agents import chat_scope_alerts, operator_identity, task_manager_safety as safety
from looper.agents.tiers import task_manager as tm
from looper.db import agent_sessions
from tests.db_support import DSN, run_db, until
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
VERSION = safety.VERIFIED_CLI_VERSION


@pytest.fixture
def native(monkeypatch, tmp_path):
    return NativeTaskManager(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    sent: list[str] = []

    async def sender(channel_id, chat, text, **kwargs):
        sent.append(text)
        return True, None

    async def target():
        return SimpleNamespace(channel_id="synthetic-channel", chat="synthetic-chat")

    chat_scope_alerts.configure(sender)
    monkeypatch.setattr(operator_identity, "operator_dm_target", target)
    safety.refusal_counts.clear()
    yield sent
    chat_scope_alerts.reset()
    safety.refusal_counts.clear()


def override(native, content) -> None:
    native.transcript.with_name("readback-override.json").write_text(json.dumps(content))


def clear_override(native) -> None:
    native.transcript.with_name("readback-override.json").write_text("{}")


def order(native) -> list[str]:
    """Every control request and user input the stand-in saw, in order."""
    if not native.transcript.exists():
        return []
    rows = [json.loads(line) for line in native.transcript.read_text().splitlines()]
    return [row["subtype"] if row["kind"] == "control" else row["kind"] for row in rows
            if row["kind"] in ("control", "input", "launch")]


WIDER = {"get_settings": {"effective": {"sandbox": {"excludedCommands": ["git"]}}}}


def test_a_mismatch_at_launch_refuses_before_any_user_message(monkeypatch, tmp_path, native, alerts):
    async def check(db):
        task_id, _, _ = await native.create(db)
        override(native, WIDER)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="message-one", sink=sink)
            assert result.state == "refused" and result.reason.value == "launch_setting_mismatch"
            await sink.done()
            assert "sandbox.excludedCommands" in sink.finals[0][0]
            seen = order(native)
            assert seen[0] == "launch" and seen[1] == "initialize" and "input" not in seen
            assert not pool.agent(task_id).resident.started  # stopped with kill
            assert safety.refusal_counts[("launch_setting_mismatch", "launch", VERSION)] == 1
            assert len(alerts) == 1 and task_id in alerts[0] and "launch_setting_mismatch" in alerts[0]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_a_clean_launch_reads_back_before_the_first_turn_and_later_turns_do_not_initialize(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, "one", message_id="one")
            first = order(native)
            assert first.index("input") > max(i for i, kind in enumerate(first) if kind != "input" and kind != "launch")
            assert first.count("initialize") == 1 and first[1] == "initialize"
            await submit_and_wait(pool, task_id, "two", message_id="two")
            second = order(native)[len(first):]
            assert "initialize" not in second and second[-1] == "input"
            assert sorted(set(second) - {"input"}) == ["get_context_usage", "get_hooks_listing", "get_sandbox_dialog",
                                                       "get_settings", "list_permission_rules", "mcp_status"]
            assert not safety.refusal_counts
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_a_mismatch_that_appears_before_turn_two_refuses_it_and_the_next_turn_reads_back_again(monkeypatch, tmp_path, native, alerts):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, "one", message_id="one")
            override(native, WIDER)
            sink = Sink()
            result = await pool.submit(task_id, "two", message_id="two", sink=sink)
            assert result.state == "refused" and result.reason.value == "launch_setting_mismatch"
            await sink.done()
            assert [row["text"] for row in native.records("input")] == ["one"]  # turn two was never written
            assert safety.refusal_counts[("launch_setting_mismatch", "turn", VERSION)] == 1
            assert not pool.agent(task_id).resident.started and len(alerts) == 1
            clear_override(native)
            before = len(order(native))
            await submit_and_wait(pool, task_id, "three", message_id="three")
            assert len(native.records("launch")) == 2  # a new process
            third = order(native)[before:]
            assert third[0] == "launch" and third[1] == "initialize" and third[-1] == "input"
            assert [row["text"] for row in native.records("input")] == ["one", "three"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("answer,item", [("error", "get_settings"), ("hang", "get_settings")])
def test_a_cli_that_does_not_answer_is_readback_unavailable(monkeypatch, tmp_path, native, alerts, answer, item):
    async def check(db):
        task_id, _, _ = await native.create(db)
        override(native, {item: answer})
        monkeypatch.setattr("looper.agents.claude_cli.CONTROL_TIMEOUT_SEC", 0.5)
        monkeypatch.setattr("looper.agents.task_manager_readback.CONTROL_TIMEOUT_SEC", 0.5)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="message-one", sink=sink)
            assert result.state == "refused" and result.reason.value == "readback_unavailable"
            await sink.done()
            assert item in sink.finals[0][0]
            assert "input" not in order(native)
            assert safety.refusal_counts[("readback_unavailable", "launch", VERSION)] == 1
            assert len(alerts) == 1
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


def test_an_orchestrated_session_never_sends_a_control_request(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db, status="running")
        override(native, WIDER)  # would refuse a direct session
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, "one", message_id="one")
            assert pool.agent(task_id).mode == "orchestrated"
            assert not [kind for kind in order(native) if kind not in ("launch", "input")]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_a_resume_reads_back_with_initialize_before_the_message(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        first = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        await submit_and_wait(first, task_id, "one", message_id="one")
        await first.shutdown()
        assert await agent_sessions.get_session(f"task-manager-{task_id}")
        before = len(order(native))
        second = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(second, task_id, "two", message_id="two")
            resumed = order(native)[before:]
            assert resumed[:2] == ["launch", "initialize"] and resumed[-1] == "input"
            assert native.records("launch")[1]["command"][-2] == "--resume"
        finally:
            await second.shutdown()
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("init,item", [
    ({"tools": ["Bash", "Read"]}, "tools"),
    ({"tools": ["Bash", "ToolSearch"]}, "tools"),
    ({"tools": []}, "tools"),
    ({"permissionMode": "default"}, "permissions.defaultMode"),
])
def test_an_init_that_lists_other_tools_or_mode_discards_the_turn(monkeypatch, tmp_path, native, alerts, init, item):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, "one", message_id="one")
            override(native, {"init": init})
            sink = Sink()
            result = await pool.submit(task_id, "write", message_id="two", sink=sink)
            await sink.done()
            assert result.state in ("refused", "started")
            assert sink.finals[-1][1] == "refused" and item in sink.finals[-1][0]
            assert sink.deltas == [] and sink.tools == []  # nothing of the turn was delivered
            assert safety.refusal_counts[("launch_setting_mismatch", "turn", VERSION)] == 1
            assert not pool.agent(task_id).resident.started and len(alerts) == 1
            clear_override(native)
            await submit_and_wait(pool, task_id, "three", message_id="three")
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


def test_the_fresh_process_after_a_failed_resume_is_read_back_too(monkeypatch, tmp_path, native, alerts):
    async def check(db):
        task_id, _, directory = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, "one", message_id="one")
            await pool.close(task_id)
            (directory / "reject-resume").touch()  # the resumed process exits before init
            override(native, WIDER)  # the fresh process that replaces it reports a wider setting
            sink = Sink()
            await pool.submit(task_id, "two", message_id="two", sink=sink)
            await sink.done()
            assert sink.finals[-1][1] == "refused" and "sandbox.excludedCommands" in sink.finals[-1][0]
            assert len(native.records("launch")) == 3  # first, the resume that exited, the fresh one
            assert [row["text"] for row in native.records("input")] == ["one"]  # nothing was sent to the fresh process
            assert safety.refusal_counts[("launch_setting_mismatch", "launch", VERSION)] == 1
            assert not pool.agent(task_id).resident.started and len(alerts) == 1
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("mode", ["skip", "late", "bare"])
@pytest.mark.parametrize("after_a_turn", [False, True])
def test_a_turn_without_a_passing_init_before_its_events_is_refused(monkeypatch, tmp_path, native, alerts, mode, after_a_turn):
    """Findings 3 and 4: no ``system/init`` at all, or one after the turn's first events, delivers nothing."""
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            if after_a_turn:
                await submit_and_wait(pool, task_id, "one", message_id="one")
            override(native, {"init_mode": mode})
            sink = Sink()
            await pool.submit(task_id, "two", message_id="two", sink=sink)
            await sink.done()
            assert sink.finals[-1][1] == "refused" and "init" in sink.finals[-1][0]
            assert sink.deltas == [] and sink.tools == []  # nothing of the turn reached the sink
            assert safety.refusal_counts[("launch_setting_mismatch", "turn", VERSION)] == 1
            assert not pool.agent(task_id).resident.started and len(alerts) == 1
            clear_override(native)
            await submit_and_wait(pool, task_id, "three", message_id="three")
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


def test_a_tool_the_turns_init_did_not_list_is_refused_before_it_is_delivered(monkeypatch, tmp_path, native, alerts):
    """Finding 5: a tool call outside the set the passing init listed (a tool a server registered mid-turn)."""
    async def check(db):
        task_id, _, _ = await native.create(db)
        override(native, {"tool": "mcp__looper__registered_later"})
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = Sink()
            await pool.submit(task_id, "two", message_id="two", sink=sink)
            await sink.done()
            assert sink.finals[-1][1] == "refused" and "mcp__looper__registered_later" in sink.finals[-1][0]
            assert sink.tools == [] and safety.refusal_counts[("launch_setting_mismatch", "turn", VERSION)] == 1
            assert not pool.agent(task_id).resident.started and len(alerts) == 1
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


def test_a_partial_tool_start_the_init_did_not_list_is_refused_before_anything_is_delivered(monkeypatch, tmp_path, native, alerts):
    """A tool that starts as a ``content_block_start`` of the stream, with no assistant message after it."""
    async def check(db):
        task_id, _, _ = await native.create(db)
        override(native, {"partial_tool": "Write"})
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = Sink()
            await pool.submit(task_id, "two", message_id="two", sink=sink)
            await sink.done()
            assert sink.finals[-1][1] == "refused" and "Write" in sink.finals[-1][0]
            assert sink.deltas == [] and sink.tools == []
            assert not pool.agent(task_id).resident.started and len(alerts) == 1
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("init_mode", ["skip", "normal"])
def test_a_stop_before_the_init_does_not_deliver_the_cli_result(monkeypatch, tmp_path, native, alerts, init_mode):
    async def check(db):
        task_id, _, _ = await native.create(db)
        override(native, {"init_mode": init_mode})
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = Sink()
            assert (await pool.submit(task_id, "wait", message_id="one", sink=sink)).state == "started"
            await until(lambda: len(native.records("input")) == 1)
            result = pool.stop_turn(task_id, "one")
            assert (await result if inspect.isawaitable(result) else result) is True
            await sink.done()
            if init_mode == "skip":  # the turn never showed an init: the CLI's own result text is not delivered
                assert sink.finals[-1][1] == "refused" and sink.finals[-1][0] != "stopped"
                assert not pool.agent(task_id).resident.started and not alerts  # a user stop is not an alarm
                assert safety.refusal_counts[("launch_setting_mismatch", "turn", VERSION)] == 1
            else:  # after a passing init the stop keeps its protocol
                assert sink.finals[-1][1] == "stopped"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)


def test_a_stop_while_the_init_is_still_to_come_ends_the_process_without_an_alert(monkeypatch, tmp_path, native, alerts):
    """Pins the timing the idle-reclaim test used to depend on: the input is written, the init is not yet there."""
    async def check(db):
        task_id, _, _ = await native.create(db)
        override(native, {"init_delay": 1.5})
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            sink = Sink()
            await pool.submit(task_id, "wait", message_id="one", sink=sink)
            await until(lambda: len(native.records("input")) == 1)
            result = pool.stop_turn(task_id, "one")
            assert (await result if inspect.isawaitable(result) else result) is True
            await sink.done()
            assert sink.finals[-1][1] == "refused"
            assert not pool.agent(task_id).resident.started and not alerts
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=40)
