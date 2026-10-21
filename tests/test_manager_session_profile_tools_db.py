"""Telling the Manager that /profile switches its session, and looper_settings showing that choice.

The contract tests need no database; the tool tests run only when LOOPER_TEST_DATABASE_URL points at a disposable
database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents.agent_settings import DEFAULT_HARNESS_PROMPT, Harness, Layer
from looper.agents.system_prompt import build_system_prompt
from looper.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT, TOOLS_CONTRACT
from tests.test_manager_session_profile_db import _profile_id
from tests.test_ops_tasks_db import _Chat
from tests.test_usage_api_db import DSN, _run

BULLET = """\
- The user switches the Agent Profile of this conversation with /profile
  and starts a new one with /new; Looper answers both itself. You cannot
  switch it: when asked to, tell the user to send /profile (it lists the
  choices). Setting the manager role's profile with looper_settings applies
  to later sessions only when this one has its own choice.
"""
NEXT = '- Never say an operation ran unless its answer was "ok".\n'
NOTE = ("the Manager's current session keeps its own profile (chosen with /profile); "
        "this setting applies from its next session")

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_contract_tells_the_manager_about_profile_once_before_the_last_bullet() -> None:
    assert TOOLS_CONTRACT.count(BULLET) == 1
    assert BULLET + NEXT in TOOLS_CONTRACT


def test_manager_prompt_includes_it() -> None:
    prompt = build_system_prompt(
        harness=Harness(DEFAULT_HARNESS_PROMPT, Layer.BUILTIN), role_id="manager", role=DEFAULT_SYSTEM_PROMPT,
        recommended=True, contract_name="TOOLS_CONTRACT", contract=TOOLS_CONTRACT)
    assert BULLET in prompt.text


async def _settings(chat: _Chat, **arguments: Any) -> dict[str, Any]:
    (answer,) = await chat.say("配置", ("looper_settings", arguments))
    assert answer["status"] == "ok", answer
    return answer["result"]


@needs_db
def test_view_and_set_show_the_selected_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import agent_sessions

    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        pid = await _profile_id(pool)
        await agent_sessions.select_profile("manager", pid)
        viewed = await _settings(chat, action="view", role="manager")
        selected = viewed["manager_session"]["selected"]
        assert selected["profile_id"] == pid and selected["name"]
        changed = await _settings(chat, action="set", role="manager", values={"profile": pid})
        assert changed["manager_session"]["selected"]["profile_id"] == pid
        assert changed["note"] == NOTE

    _run(test, monkeypatch)


@needs_db
def test_without_a_selection_there_is_no_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        pid = await _profile_id(pool)
        assert (await _settings(chat, action="view", role="manager"))["manager_session"] == {"selected": None}
        changed = await _settings(chat, action="set", role="manager", values={"profile": pid})
        assert changed["manager_session"] == {"selected": None} and "note" not in changed

    _run(test, monkeypatch)


@needs_db
def test_other_roles_are_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        pid = await _profile_id(pool)
        assert "manager_session" not in await _settings(chat, action="view", role="worker")
        assert "manager_session" not in await _settings(chat, action="view")
        changed = await _settings(chat, action="set", role="worker", values={"profile": pid})
        assert "manager_session" not in changed and "note" not in changed

    _run(test, monkeypatch)
