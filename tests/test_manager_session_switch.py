"""Starting a new Manager session with a chosen Agent Profile, and the switches between profiles.

Claude Code and Codex are real subprocess stand-ins; the database is ``agent_db``'s in-memory one. The session's
choice reaches the Manager the way ``list_layers`` gives it: a ``session`` layer row with the profile.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.tiers import manager as manager_module
from looper.db import connection as db_connection
from looper.agents.tiers.manager import NEW_SESSION_REPLY, ManagerAgent, ManagerConfig
from tests.db_support import until
from tests.test_manager_new_session import _drain, _resets, _saved, _web
from tests.test_manager_reply import _CODEX, _codex_profile, _converse, _fake_claude, _flag, _forgetting, _resident


def _session(row: dict[str, Any]) -> dict[str, Any]:
    """The session layer ``list_layers`` returns when the user chose ``row`` for the Manager's session."""
    return {"scope": "session", "role": "manager", "profile": row}


def _logging(path: Path, body: str) -> str:
    """``body`` behind a prologue that appends everything read from stdin to ``path`` (and hands it on)."""
    return (
        "import io as _io\n"
        "_data = sys.stdin.read() if 'resident' not in sys.argv[0] and 'exec' in sys.argv else None\n"
        "if _data is not None:\n"
        f"    open({str(path)!r}, 'a').write(_data + '\\n=====\\n')\n"
        "    sys.stdin = _io.StringIO(_data)\n"
        + body
    )


_CLAUDE_LOG = (
    "class _Logged:\n"
    "    def __init__(self, stream, path):\n"
    "        self.stream, self.path = stream, path\n"
    "    def __iter__(self):\n"
    "        for line in self.stream:\n"
    "            open(self.path, 'a').write(json.loads(line)['message']['content'] + '\\n=====\\n')\n"
    "            yield line\n"
)


def _claude_logging(path: Path, body: str) -> str:
    return _CLAUDE_LOG + f"sys.stdin = _Logged(sys.stdin, {str(path)!r})\n" + body


def _entries(path: Path) -> list[str]:
    return path.read_text().split("\n=====\n")[:-1] if path.exists() else []


def _runs(agent_db: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in agent_db.start_record.await_args_list]


def _registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, key: bool = True) -> dict[str, dict[str, Any]]:
    """Two Agent Profiles in the registry: ``agent-codex`` and ``agent-sonnet`` (Claude Code)."""
    from looper.db import profile_registry as db_registry

    codex = fake_claude(tmp_path / "codex", _logging(tmp_path / "codex.log", _CODEX))
    rows = {row["id"]: row for row in (_codex_profile(codex), profile_json("sonnet"))}

    async def _view(**_: Any) -> dict[str, Any]:
        return {"profiles": [profile_registry.profile_view(row) for row in rows.values()]}

    monkeypatch.setattr(profile_registry, "registry_view", _view)
    monkeypatch.setattr(db_registry, "get_profile", AsyncMock(side_effect=lambda pid: rows.get(pid)))
    monkeypatch.setattr(db_registry, "get_api_key", AsyncMock(return_value="sk-test" if key else None))
    monkeypatch.setattr(db_registry, "list_models", AsyncMock(return_value=[
        {"id": "model-gpt-5.5", "provider_id": "prov-cpa", "name": "gpt-5.5", "protocol": "openai-responses",
         "context_window": None, "input_usd_per_mtok": None, "output_usd_per_mtok": None,
         "cache_read_usd_per_mtok": None, "cache_write_usd_per_mtok": None, "efforts": None,
         "background_model": None, "catalog_order": 0, "builtin": False}]))
    return rows


@pytest.mark.usefixtures("agent_db")
class TestSwitchPath:
    def test_claude_code_to_codex_and_back(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        rows = _registry(monkeypatch, tmp_path)
        log = tmp_path / "claude.log"
        on_codex = [_session(rows["agent-codex"])]
        chat = _converse(monkeypatch, _claude_logging(log, _resident()), ["hello", "again", "third"],
                         layers=[[], on_codex, []])
        assert chat["replies"][0] == "turn 1: hello"
        assert chat["replies"][1].startswith("codex thread-1:")
        spawned = [argv for argv, _ in chat["spawned"]]
        claude1, claude2 = spawned[0], spawned[-1]
        assert "--resume" not in claude1 and "--resume" not in claude2
        claude_inputs = _entries(log)
        # The handoff runs on the process that served turn 1; the new Claude session starts from a resume brief.
        assert claude_inputs[1].rpartition("\n\n")[2].startswith("[HANDOFF]")
        assert claude_inputs[-1].startswith("[RESUME]") or "[RESUME]" in claude_inputs[-1].split("\n\n")[0]
        codex_inputs = _entries(tmp_path / "codex.log")
        assert any(entry.startswith("[RESUME]") or "[RESUME]" in entry for entry in codex_inputs)
        assert any(entry.rpartition("\n\n")[2].startswith("[HANDOFF]") for entry in codex_inputs)

        by_input = [(r["input_text"], r["metadata"]) for r in _runs(agent_db)]
        claude_profile = by_input[0][1]["profile"]["id"]
        claude_cost = by_input[0][1]["pricing"]["cost_source"]
        handoffs = [m for text, m in by_input if text.startswith("[HANDOFF]")]
        turns = [m for text, m in by_input if not text.startswith("[HANDOFF]")]
        assert len(handoffs) == 2 and len(turns) == 3
        assert [m["profile"]["id"] for m in turns] == [claude_profile, "agent-codex", claude_profile]
        assert [m["profile"]["id"] for m in handoffs] == [claude_profile, "agent-codex"]
        assert turns[1]["pricing"]["cost_source"] != claude_cost
        assert [m["pricing"]["cost_source"] for m in handoffs] == [claude_cost, turns[1]["pricing"]["cost_source"]]
        assert turns[2]["pricing"]["cost_source"] == claude_cost

    def test_another_model_on_the_same_runtime_resumes_the_session(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        rows = _registry(monkeypatch, tmp_path)
        chat = _converse(monkeypatch, _resident(), ["hello", "again"],
                         layers=[[_session(profile_json("opus"))], [_session(rows["agent-sonnet"])]])
        first, second = [argv for argv, _ in chat["spawned"]]
        assert "--resume" not in first and _flag(second, "--resume") == "sess-1"
        assert _flag(second, "--model") == "sonnet"
        assert not [r for r in _runs(agent_db) if r["input_text"].startswith("[HANDOFF]")]
        assert chat["replies"] == ["turn 1: hello", "turn 1: again"]  # one process per profile

    def test_a_session_that_cannot_be_resumed_is_retried_and_keeps_the_choice(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        rows = _registry(monkeypatch, tmp_path)
        chat = _converse(monkeypatch, _forgetting(), ["hello"], saved_session="gone", saved_runtime="claude-code",
                         layers=[[_session(rows["agent-sonnet"])]])
        assert chat["replies"] == ["turn 1: hello"]
        agent_db.select_profile.assert_not_awaited()

    def test_a_broken_session_profile_is_answered_with_the_hint_and_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        rows = _registry(monkeypatch, tmp_path, key=False)
        chat = _converse(monkeypatch, _resident(), ["hello"], layers=[[_session(rows["agent-codex"])]])
        (reply,) = chat["replies"]
        assert reply.startswith("Manager 出错，没能完成这次回复：")
        assert reply.endswith("发送 /profile 换一个 Agent Profile，或 /profile 0 跟随设置。")
        assert chat["spawned"] == []

    def test_a_broken_role_profile_gets_the_other_hint(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        rows = _registry(monkeypatch, tmp_path, key=False)
        chat = _converse(monkeypatch, _resident(), ["hello"],
                         layers=[[{"scope": "role", "role": "manager", "profile": rows["agent-codex"]}]])
        (reply,) = chat["replies"]
        assert reply.endswith("发送 /profile 选另一个 Agent Profile。")
        assert chat["spawned"] == []


def _new_chat(monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path, messages: list[tuple[str, dict[str, Any]]],
              *, key: bool = True, saved: bool = True) -> SimpleNamespace:
    """Send ``messages`` (text, extra payload) one at a time to a Manager backed by the logging Claude stand-in.

    ``transaction`` is a stand-in that records the connection every write inside it was given.
    """
    from looper.db import agent_setting_layers

    rows = _registry(monkeypatch, tmp_path, key=key)
    store = agent_db.store
    if saved:
        _saved(agent_db)
    events: list[tuple[str, Any, Any]] = []
    conn = object()
    real_save, real_select = store.save_session, store.select_profile

    async def _save(agent_id: str, *args: Any, pool: Any = None, **kwargs: Any) -> None:
        events.append(("save", pool, kwargs.get("clean_start")))
        await real_save(agent_id, *args, **kwargs)

    async def _select(agent_id: str, profile_id: str | None, *, pool: Any = None) -> None:
        events.append(("select", pool, profile_id))
        await real_select(agent_id, profile_id)

    agent_db.save_session.side_effect = _save
    agent_db.select_profile.side_effect = _select

    @contextlib.asynccontextmanager
    async def _transaction() -> Any:
        events.append(("begin", conn, None))
        yield conn
        events.append(("commit", conn, None))

    monkeypatch.setattr(db_connection, "transaction", _transaction)

    async def _list_layers(*_: Any, manager_id: str | None = None, **__: Any) -> list[dict[str, Any]]:
        chosen = store.selected_profile_id.get(manager_id or "")
        if manager_id and chosen:
            return [{"scope": "session", "role": "manager", "profile_id": chosen, "profile": rows[chosen]}]
        return []

    monkeypatch.setattr(agent_setting_layers, "list_layers", _list_layers)

    spawned: list[list[str]] = []
    stopped: list[bool] = []
    stopped_by_first: list[bool] = []
    real_exec = asyncio.create_subprocess_exec

    async def _exec(*args: Any, **kwargs: Any) -> Any:
        spawned.append([str(a) for a in args])
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    log = tmp_path / "claude.log"

    async def _run() -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _fake_claude(root, _claude_logging(log, _resident()))
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(config=ManagerConfig(claude_bin=claude, work_dir=root), runtime_root=root)
            manager._bus = bus
            queue = bus.get_queue(manager_module.WEB.agent_id) if hasattr(manager_module, "WEB") else None
            replies: list[str] = []
            real_stop = manager._claude.stop

            async def _stop() -> None:
                stopped.append(True)
                await real_stop()

            manager._claude.stop = _stop  # type: ignore[method-assign]
            for index, (text, payload) in enumerate(messages):
                envelope = _web(text, index)
                envelope.payload.update(payload)
                queue = queue or bus.get_queue(envelope.source.agent_id)
                await manager._handle(envelope)
                replies += [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]
                if index == 0:
                    stopped_by_first.extend(stopped)
            await manager.stop()
            await bus.shutdown()
            return replies

    replies = asyncio.run(_run())
    return SimpleNamespace(replies=replies, argvs=spawned, events=events, conn=conn, stopped=stopped_by_first,
                           inputs=_entries(log), rows=rows)


@pytest.mark.usefixtures("agent_db")
class TestNewSessionWithProfile:
    def test_the_dropped_session_and_the_choice_are_written_together(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _new_chat(monkeypatch, agent_db, tmp_path,
                         [("/new", {"new_session_profile_id": "agent-sonnet"}), ("hello", {})])
        assert chat.events[:4] == [("begin", chat.conn, None), ("save", chat.conn, True),
                                   ("select", chat.conn, "agent-sonnet"), ("commit", chat.conn, None)]
        assert chat.replies[0] == NEW_SESSION_REPLY + "这次会话用「claude-code/sonnet」。"
        (reset,) = _resets(agent_db)
        assert reset["metadata"]["session_reset"]["selected_profile_id"] == "agent-sonnet"
        # The next message: no resume, no resume brief, on the chosen profile.
        (argv,) = chat.argvs
        assert "--resume" not in argv and _flag(argv[1:], "--model") == "sonnet"
        assert not chat.inputs[0].startswith("[RESUME]") and "[RESUME]" not in chat.inputs[0]
        assert chat.replies[1] == "turn 1: hello"
        turn = next(r for r in _runs(agent_db) if "session_reset" not in r["metadata"])
        assert turn["metadata"]["profile"]["id"] == "agent-sonnet"

    def test_an_invalid_profile_resets_nothing(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _new_chat(monkeypatch, agent_db, tmp_path, [("/new", {"new_session_profile_id": "agent-nope"})])
        (reply,) = chat.replies
        assert reply.startswith("Manager 出错，没能开始新会话：")
        assert chat.events == [] and chat.stopped == []
        assert agent_db.store.sessions["manager"]["session_id"] == "sess-0"
        assert not agent_db.store.sessions["manager"].get("clean_start")
        assert _resets(agent_db) == []

    def test_a_profile_that_cannot_launch_resets_nothing(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        chat = _new_chat(monkeypatch, agent_db, tmp_path, [("/new", {"new_session_profile_id": "agent-codex"})],
                         key=False)
        assert chat.replies[0].startswith("Manager 出错，没能开始新会话：")
        assert chat.events == [] and chat.stopped == []
        assert agent_db.store.sessions["manager"]["session_id"] == "sess-0"

    def test_plain_new_clears_the_choice(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, tmp_path: Path,
    ) -> None:
        agent_db.store.selected_profile_id["manager"] = "agent-sonnet"
        chat = _new_chat(monkeypatch, agent_db, tmp_path, [("/new", {})])
        assert ("select", chat.conn, None) in chat.events
        assert chat.replies == [NEW_SESSION_REPLY]
        assert "manager" not in agent_db.store.selected_profile_id
        (reset,) = _resets(agent_db)
        assert reset["metadata"]["session_reset"]["selected_profile_id"] is None


def test_a_stop_during_the_handoff_keeps_the_old_session_and_the_next_message_hands_off_again(
        agent_db: Any, tmp_path: Path) -> None:
    from looper.agents.tiers.manager import StopResult, _Process
    from tests.test_manager_stop import _manager, _replies, _Session

    async def run() -> None:
        session = _Session()
        manager, bus, queue = _manager(tmp_path, session)
        manager._session_loaded = True
        manager._session_id, manager._session_runtime = "old-session", "omp"
        session.started = True
        manager._process = _Process(["claude"], None, None, None)  # type: ignore[arg-type]
        stops: list[str] = []
        session.stop = AsyncMock(side_effect=lambda **kw: stops.append("stop"))  # type: ignore[method-assign]
        first = _web("你好", 0)
        handling = asyncio.create_task(manager._handle(first))
        await until(session.running.is_set, timeout=10)
        assert session.turns[0].startswith("[HANDOFF]")
        assert await manager.stop_turn(first.message_id, via="web") is StopResult.STOPPING
        await asyncio.wait_for(handling, 5)
        (reply,) = _replies(queue)
        assert reply["outcome"] == "stopped"
        assert stops == [] and session.started
        assert manager._session_id == "old-session" and manager._pending_switch is None
        session.hold = False
        session.running.clear()
        session._stopped.clear()
        await asyncio.wait_for(manager._handle(_web("再来", 1)), 5)
        assert len([t for t in session.turns if t.startswith("[HANDOFF]")]) == 2
        await bus.shutdown()

    asyncio.run(run())
