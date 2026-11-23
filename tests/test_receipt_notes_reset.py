"""A card-button note never outlives its Manager session: every real way a session is cleared or replaced (/new,
a switch of agent, the rebuild after a failed resume) drops the notes left in it. The Manager code runs for real;
only the notes sit in a ``ManagerOps`` of their own, which hears the reset through ``ManagerSession``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from looper.agents import receipts_settings
from looper.agents.ops.service import ManagerOps, Platform
from tests.test_manager_new_session import _chat
from tests.test_manager_reply import _converse, _forgetting, _resident
from tests.test_manager_startup_recovery import _web
from tests.test_manager_stop import _manager, _replies, _Session

CHAT = ("lark_bot", "channel-lark_bot", "oc_dm")


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> ManagerOps:
    """A ManagerOps holding one button note left in the main session."""
    monkeypatch.setattr(receipts_settings, "_enabled", True)
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="t")
    ops._note_button(CHAT, "ou_asker", "c-1", "it was cancelled; it did not run")
    assert ops._button_notes
    return ops


def test_a_new_command_drops_the_note(monkeypatch: pytest.MonkeyPatch, agent_db: Any, notes: ManagerOps) -> None:
    _chat(monkeypatch, agent_db, ["hello", "again"])
    assert notes._button_notes  # a chat without a reset leaves it
    _chat(monkeypatch, agent_db, ["hello", "/new", "again"])
    assert notes._button_notes == {}


def test_the_rebuild_after_a_failed_resume_drops_the_note(monkeypatch: pytest.MonkeyPatch, agent_db: Any,
                                                          notes: ManagerOps) -> None:
    _converse(monkeypatch, _resident(), ["hello", "again"], saved_session="sess-1", saved_totals={"cost_usd": 0.5})
    assert notes._button_notes  # a session that resumes is the same session
    chat = _converse(monkeypatch, _forgetting(), ["hello", "again"], saved_session="gone",
                     saved_totals={"cost_usd": 0.1})
    assert chat["replies"] == ["turn 1: hello", "turn 2: again"]
    assert notes._button_notes == {}


def test_a_switch_of_agent_drops_the_note(tmp_path, agent_db: Any, notes: ManagerOps) -> None:
    async def run() -> None:
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        manager._session_loaded = True
        main = manager._sessions["main"]
        main.session_id, main.session_runtime = "old-session", "omp"  # another runtime's session: it is replaced
        assert notes._button_notes
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 5)
            assert _replies(queue)[0]["outcome"] == "answered"
        finally:
            await bus.shutdown()

    asyncio.run(run())
    assert notes._button_notes == {}


# --- The input actually sent, with the real Ops attached to the Manager ---

NOTE = "[Looper] After your last reply"


def _attach_ops(monkeypatch: pytest.MonkeyPatch, plant: bool = True) -> list[ManagerOps]:
    """Every ManagerAgent built from now on has a real ManagerOps, and a card button's note is left for each turn
    as a press would leave it just before the turn begins."""
    from looper.agents.tiers.manager import ManagerAgent

    made: list[ManagerOps] = []
    init = ManagerAgent.__init__

    def patched(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="t")
        ops._working_at = ops._clock()
        begin = ops._begin_turn

        def planted(turn: Any, binding: Any = None, *, session_key: str = "main") -> str:
            if plant and not turn.autonomous:
                ops._chat_sessions[turn.chat_key] = session_key
                ops._note_button(turn.chat_key, turn.sender, "c-1", "it was cancelled; it did not run")
            return begin(turn, binding, session_key=session_key)

        monkeypatch.setattr(ops, "_begin_turn", planted)
        self.ops = ops
        made.append(ops)

    monkeypatch.setattr(ManagerAgent, "__init__", patched)
    return made


def _sent_by_switch(tmp_path: Any, switch: bool, text: str = "hello") -> str:
    async def run() -> str:
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        manager._session_loaded = True
        if switch:
            main = manager._sessions["main"]
            main.session_id, main.session_runtime = "old-session", "omp"
        try:
            await asyncio.wait_for(manager._handle(_web(text)), 5)
            assert _replies(queue)[0]["outcome"] == "answered"
        finally:
            await bus.shutdown()
        return session.turns[-1]

    return asyncio.run(run())


def test_the_input_after_a_switch_of_agent_carries_no_old_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
                                                              agent_db: Any) -> None:
    monkeypatch.setattr(receipts_settings, "_enabled", True)
    _attach_ops(monkeypatch)
    assert NOTE in _sent_by_switch(tmp_path / "a", switch=False)  # the note does reach a turn that keeps its session
    (tmp_path / "b").mkdir()
    assert NOTE not in _sent_by_switch(tmp_path / "b", switch=True)


def test_the_input_after_a_resume_rebuild_carries_no_old_note(monkeypatch: pytest.MonkeyPatch,
                                                              agent_db: Any) -> None:
    from tests.test_manager_new_session import _LOG_TURNS

    seen: list[str] = []
    managers: list[Any] = []
    monkeypatch.setattr(receipts_settings, "_enabled", True)
    _attach_ops(monkeypatch)
    from looper.agents.tiers.manager import ManagerAgent

    wrapped = ManagerAgent.__init__

    def keep(self: Any, *args: Any, **kwargs: Any) -> None:
        wrapped(self, *args, **kwargs)
        managers.append(self)

    monkeypatch.setattr(ManagerAgent, "__init__", keep)
    forgetting = _forgetting()
    resident = _resident()
    body = forgetting[:-len(resident)] + _LOG_TURNS + resident

    async def between(_spawned: Any) -> None:  # after message one: what the process that answered it read
        seen.append((managers[-1]._config.work_dir / "turns.jsonl").read_text())

    for saved, expected in (("sess-1", True), ("gone", False)):
        seen.clear()
        _converse(monkeypatch, _LOG_TURNS + resident if expected else body, ["hello", "again"], saved_session=saved,
                  saved_totals={"cost_usd": 0.1}, between=between)
        assert (NOTE in seen[0]) is expected, (saved, seen[0][:300])


def test_a_failing_listener_does_not_abort_the_reset_or_silence_the_others(monkeypatch: pytest.MonkeyPatch,
                                                                           tmp_path: Any, agent_db: Any,
                                                                           caplog: Any, notes: ManagerOps) -> None:
    from looper.agents.tiers import manager_session

    class Bomb:
        calls = 0

        def hear(self, key: str) -> None:
            Bomb.calls += 1
            raise RuntimeError("listener broke")

    class Recorder:
        keys: list[str] = []

        def hear(self, key: str) -> None:
            self.keys.append(key)

    bomb, recorder = Bomb(), Recorder()
    manager_session.on_session_replaced(bomb.hear)  # after the notes' ops, before the recorder
    manager_session.on_session_replaced(recorder.hear)

    async def run() -> None:
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        manager._session_loaded = True
        main = manager._sessions["main"]
        main.session_id, main.session_runtime = "old-session", "omp"
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 5)
            assert _replies(queue)[0]["outcome"] == "answered"  # the switch completed
            assert main.session_id != "old-session"
        finally:
            await bus.shutdown()

    asyncio.run(run())
    assert Bomb.calls >= 1 and recorder.keys[0] == "main"
    assert notes._button_notes == {}
    assert "listener broke" in caplog.text


NOTE_LINE = "[Looper] After your last reply, the operator answered confirmation c-1 with its card: it was cancelled; it did not run."


def test_a_user_line_equal_to_the_note_survives_when_the_note_is_taken_back(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any, agent_db: Any) -> None:
    """Nothing is removed by what it says: the note stands at a token only code can name."""
    monkeypatch.setattr(receipts_settings, "_enabled", True)
    _attach_ops(monkeypatch)
    for name, switch in (("a", False), ("b", True)):
        (tmp_path / name).mkdir()
        sent = _sent_by_switch(tmp_path / name, switch=switch, text=NOTE_LINE)
        assert sent.endswith(NOTE_LINE)  # the user's own message, untouched, last
        assert sent.count(NOTE_LINE) == (2 if not switch else 1), (name, sent)  # the real note only if it stands
        assert "looper-button-notes" not in sent


def test_a_note_for_a_switch_that_was_turned_off_before_the_send_is_not_sent(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any, agent_db: Any) -> None:
    from looper.agents.tiers.manager import ManagerAgent

    monkeypatch.setattr(receipts_settings, "_enabled", True)
    made = _attach_ops(monkeypatch)
    init = ManagerAgent.__init__

    def off_after_begin(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        begin = self.ops.begin_turn

        def begin_then_off(*a: Any, **k: Any) -> str:
            note = begin(*a, **k)
            receipts_settings.set_enabled(False)  # turned off between preparing the turn and sending it
            return note

        monkeypatch.setattr(self.ops, "begin_turn", begin_then_off)

    monkeypatch.setattr(ManagerAgent, "__init__", off_after_begin)
    sent = _sent_by_switch(tmp_path, switch=False, text=NOTE_LINE)
    assert sent.count(NOTE_LINE) == 1 and sent.endswith(NOTE_LINE) and "looper-button-notes" not in sent
    assert made
