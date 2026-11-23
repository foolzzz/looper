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
