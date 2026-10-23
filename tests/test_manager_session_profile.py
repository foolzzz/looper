"""/profile in the Manager: Looper answers it itself, records each switch and applies the choice on the next message.

Claude and Codex are real subprocess stand-ins; the database is ``agent_db``'s in-memory one, with an Agent Profile
registry of two profiles on top: ``agent-codex`` (an API key provider) and ``agent-haiku`` (Claude Code).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_fixtures import profile_json
from fake_claude import fake_claude
from looper.agents import profile_registry
from looper.agents.bus import MessageBus
from looper.agents.message import MessageType
from looper.agents.tiers.manager import (
    PROFILE_CURRENT_REPLY, PROFILE_EFFECT_REPLIES, ManagerAgent, ManagerConfig,
)
from tests.db_support import until
from tests.test_manager_new_session import _HOLD_FIRST, _drain, _saved, _web
from tests.test_manager_reply import _CODEX, _codex_profile, _fake_claude, _resident
from tests.test_manager_reply import WEB


def _runs(agent_db: Any) -> list[dict[str, Any]]:
    """The ``start_record`` keywords of the profile choices recorded, in order."""
    return [call.kwargs for call in agent_db.start_record.await_args_list
            if "session_profile" in (call.kwargs["metadata"] or {})]


def _chat(monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path, messages: list[str], *,
          key: bool = True, hold: bool = False, saved: bool = False,
          view: list[Any] | None = None) -> SimpleNamespace:
    """Send ``messages`` one at a time from the web chat to one Manager and return what happened.

    ``replies`` are the AGENT_REPLY envelopes, ``argvs`` every process started and ``turn_runs`` the metadata of the
    runs Claude or Codex answered. With ``hold`` the first message is held in Claude until the second has been sent.
    """
    from looper.db import agent_sessions, agent_setting_layers, profile_registry as db_registry

    codex = fake_claude(tmp_path / "codex", _CODEX)
    rows = {row["id"]: row for row in (_codex_profile(codex), profile_json("haiku"))}
    store = agent_db.store

    async def _view(**_: Any) -> dict[str, Any]:
        return {"profiles": [profile_registry.profile_view(row) for row in rows.values()]}

    async def _list_layers(*_: Any, manager_id: str | None = None, **__: Any) -> list[dict[str, Any]]:
        chosen = store.selected_profile_id.get(manager_id or "")
        if manager_id and chosen:
            return [{"scope": "session", "role": "manager", "profile_id": chosen, "profile": rows[chosen]}]
        return []

    monkeypatch.setattr(profile_registry, "registry_view", _view)
    monkeypatch.setattr(db_registry, "get_profile", AsyncMock(side_effect=lambda pid: rows.get(pid)))
    monkeypatch.setattr(db_registry, "get_api_key", AsyncMock(return_value="sk-test" if key else None))
    monkeypatch.setattr(db_registry, "list_models", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_setting_layers, "list_layers", _list_layers)
    assert agent_sessions.select_profile is agent_db.select_profile
    if saved:
        _saved(agent_db)

    spawned: list[list[str]] = []
    real_exec = asyncio.create_subprocess_exec

    async def _exec(*args: Any, **kwargs: Any) -> Any:
        spawned.append([str(a) for a in args])
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)

    async def _run() -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _fake_claude(root, (_HOLD_FIRST if hold else "") + _resident())
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(config=ManagerConfig(claude_bin=claude, work_dir=root), runtime_root=root)
            queue = bus.get_queue(WEB.agent_id)
            replies: list[str] = []
            if hold:
                await manager.start(bus)  # its loop serializes the messages, as the server's does
                await bus.publish(_web(messages[0], 0))
                await until((root / "busy").exists, timeout=10)
                for index, text in enumerate(messages[1:], 1):
                    await bus.publish(_web(text, index))
                await asyncio.sleep(0.3)
                assert not [e for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY], \
                    "the held turn's answer and /profile must wait for the turn"
                (root / "go").write_text("")
                while len(replies) < len(messages):
                    envelope = await asyncio.wait_for(queue.get(), 10)
                    if envelope.message_type == MessageType.AGENT_REPLY:
                        replies.append(envelope.payload["text"])
            else:
                manager._bus = bus
                for index, text in enumerate(messages):
                    await manager._handle(_web(text, index))
                    replies += [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]
            if view is not None:
                view.append(await manager.session_view())
            await manager.stop()
            await bus.shutdown()
            return replies

    replies = asyncio.run(_run())
    return SimpleNamespace(replies=replies, argvs=spawned, codex=codex, rows=rows)


@pytest.mark.usefixtures("agent_db")
class TestProfileCommand:
    def test_list_is_a_numbered_menu_and_spawns_nothing(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile"])
        (reply,) = chat.replies
        assert reply.splitlines()[0].startswith("0. 跟随设置（现在是")
        assert "1. " in reply and "2. " in reply and "codex/gpt-5.5" in reply and "claude-code/haiku" in reply
        assert chat.argvs == []
        agent_db.start_record.assert_not_awaited()
        agent_db.select_profile.assert_not_awaited()

    def test_a_number_selects_a_profile_and_the_switch_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        # Sorted by name: 1 is claude-code/haiku, 2 is codex/gpt-5.5.
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile 2"])
        agent_db.select_profile.assert_awaited_once_with("manager", "agent-codex")
        (run,) = _runs(agent_db)
        assert run["agent_id"] == "manager" and run["input_text"] == "/profile 2"
        assert run["metadata"]["session_profile"] == {
            "from": None, "to": "agent-codex", "effect": "starts", "channel": "web", "sender": None,
            "chat": "session-a", "group": False, "via": "command"}
        (finish,) = agent_db.finish_record.await_args_list
        assert (finish.kwargs["status"], finish.kwargs["cost_usd"]) == ("completed", 0.0)
        (reply,) = chat.replies
        assert "codex/gpt-5.5" in reply and reply == finish.kwargs["output_text"]
        assert reply.endswith("所有渠道共用这条会话。")
        assert chat.argvs == []

    def test_a_switch_that_cannot_resume_says_so(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile 2"], saved=True)
        assert _runs(agent_db)[0]["metadata"]["session_profile"]["effect"] == "new_session"
        assert chat.replies[0].endswith(PROFILE_EFFECT_REPLIES[
            next(e for e in PROFILE_EFFECT_REPLIES if e.value == "new_session")].format(name="codex/gpt-5.5"))

    @pytest.mark.parametrize("text", ["/profile 9", "/profile nope"])
    def test_an_unknown_profile_is_refused_with_the_list(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path, text: str,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, [text])
        agent_db.select_profile.assert_not_awaited()
        agent_db.start_record.assert_not_awaited()
        (reply,) = chat.replies
        assert "matches none" in reply and "1. " in reply and "2. " in reply

    def test_a_profile_without_its_api_key_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile 2"], key=False)
        agent_db.select_profile.assert_not_awaited()
        agent_db.start_record.assert_not_awaited()
        (reply,) = chat.replies
        assert "API key" in reply or "key" in reply.lower()
        assert "0. 跟随设置" in reply  # the list follows the reason

    def test_the_current_choice_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile 2", "/profile 2"])
        assert agent_db.select_profile.await_count == 1
        assert len(_runs(agent_db)) == 1
        assert chat.replies[1] == PROFILE_CURRENT_REPLY.format(name="codex/gpt-5.5")
        assert "没有变化" in chat.replies[1]

    def test_zero_follows_the_settings_again(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile 2", "/profile 0", "/profile 0"])
        assert [c.args for c in agent_db.select_profile.await_args_list] == [("manager", "agent-codex"),
                                                                              ("manager", None)]
        assert _runs(agent_db)[1]["metadata"]["session_profile"]["from"] == "agent-codex"
        assert "没有变化" in chat.replies[2]

    def test_the_next_message_runs_on_the_chosen_profile(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profile 2", "hello"])
        assert chat.replies[1] == "codex thread-1: hello"
        (argv,) = chat.argvs
        assert argv[argv.index("exec") - 1] == chat.codex
        turn = next(c.kwargs for c in agent_db.start_record.await_args_list
                    if "session_profile" not in (c.kwargs["metadata"] or {}))
        assert turn["metadata"]["profile"]["source"] == "session"

    def test_a_profile_sent_during_a_turn_is_answered_after_it(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["hello", "/profile 2"], hold=True)
        assert chat.replies[0] == "turn 1: hello"
        assert "codex/gpt-5.5" in chat.replies[1]
        turn = next(c.kwargs for c in agent_db.start_record.await_args_list
                    if "session_profile" not in (c.kwargs["metadata"] or {}))
        assert turn["metadata"]["profile"]["source"] != "session"
        assert len(chat.argvs) == 1  # the one Claude of the held turn

    def test_profiles_is_an_ordinary_message(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, tmp_path, ["/profiles"])
        assert chat.replies == ["turn 1: /profiles"]
        assert len(chat.argvs) == 1
        assert _runs(agent_db) == []

    def test_the_session_view_shows_the_choice_and_never_the_session_id(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        import json

        views: list[Any] = []
        _chat(monkeypatch, agent_db, tmp_path, ["/profile 2"], saved=True, view=views)
        (view,) = views
        assert view["selected"] == {"profile_id": "agent-codex", "name": "codex/gpt-5.5"}
        assert view["effective"]["source"] == "session"
        assert view["effective"]["profile"]["profile_id"] == "agent-codex"
        assert view["problem"] is None and len(view["choices"]) == 2
        assert "session_id" not in json.dumps(view)
