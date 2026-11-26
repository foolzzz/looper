"""The dialog runner keeps the inputs a paid comparison would diff, without a paid call."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression import dialogs
from looper.agents.tiers.manager import _capture_eval_inputs
from tests.db_support import DSN


def test_capture_records_the_state_block_and_turn_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "turns.jsonl"
    monkeypatch.setenv("LOOPER_EVAL_TURN_CAPTURE", str(path))
    _capture_eval_inputs(
        ["Looper state (rev abcd1234):", "Persona:", ""],
        "Projects:\n\n[Turn context]\nLocal time: 2026-11-25 Wed 12:00 (UTC, +0000)\n"
        "Speaker: the operator\nChat: web\nReply in: English\n\n请说出我的称呼。",
    )
    _capture_eval_inputs(["Looper state: unchanged since rev abcd1234", ""], "Channel: dashboard\n\n继续。")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["state_block"].startswith("Looper state (rev abcd1234):")
    assert rows[0]["turn_context"].startswith("[Turn context]\n")
    assert rows[0]["turn_context"].endswith("\n")
    assert "请说出" not in rows[0]["turn_context"]
    assert "Projects:" not in rows[0]["turn_context"]
    assert rows[1]["turn_context"] is None
    assert rows[1]["state_block"].startswith("Looper state: unchanged")


def test_capture_writes_nothing_unless_the_eval_asks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOPER_EVAL_TURN_CAPTURE", raising=False)
    _capture_eval_inputs(["Looper state: unchanged since rev ab", ""], "Channel: dashboard\n\nhi")
    assert list(tmp_path.iterdir()) == []


def test_a_stub_dialog_without_a_database_url_completes_and_marks_inputs_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stub API and an empty database URL still finish. The row says the inputs were not captured, and nothing
    opens a connection. Missing turn fields then mean "not captured", not an empty read."""
    import asyncpg

    calls: list[object] = []

    def connect(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("a stub dialog must not open a database")

    monkeypatch.setattr(asyncpg, "connect", connect)

    class Api:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
            if path.startswith("/api/v1/runs/") or path == "/api/v1/tasks/page":
                return {"status": "ok", "result": self.get(path, **data)}
            if path == "/api/v1/operator_chat/messages":
                return {"status": "ok", "result": {"messages": self.messages}}
            if path == "/api/v1/project_admin/create":
                return {"status": "ok", "result": {"project": {"project_id": "project-test"}}}
            assert path == "/api/v1/operator_chat/send", path
            self.messages.append({"message_id": f"{data['message_id']}:reply", "content": "ok"})
            return {}

        def get(self, path: str, **_: Any) -> dict[str, Any]:
            if path == "/api/v1/tasks/page":
                return {"tasks": [], "counts": {}, "next_before": None}
            if path == "/api/v1/runs/list":
                return {"records": [{"id": index + 1, "status": "completed", "error": None}
                                    for index in reversed(range(len(self.messages)))]}
            if path == "/api/v1/runs/events":
                return {"events": [{"seq": 1, "event": {"type": "result", "subtype": "success"}}]}
            raise AssertionError(path)

    @contextmanager
    def database(_: str) -> Any:
        yield "", 0.0

    @contextmanager
    def server(**_: Any) -> Any:
        yield Api()

    monkeypatch.setattr(dialogs, "fresh_database", database)
    monkeypatch.setattr(dialogs, "running_server", server)
    monkeypatch.setattr(dialogs, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(dialogs, "select_agent", lambda *_, **__: {"name": "fake"})
    monkeypatch.setattr(dialogs, "_git", lambda: {})
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
    monkeypatch.setattr(dialogs, "_usage", lambda *_: (0.0, {}))
    result, path = dialogs.run(
        mode="fake", dialogs=(dialogs.Dialog("stub", (dialogs.Say("Status?", lambda _look: None),)),),
        admin_url="", out_dir=tmp_path, timeout=10)
    assert calls == [], result.get("aborted")
    assert result["aborted"] is None
    row, = result["dialogs"]
    assert row["outcome"] == "pass", row
    assert row["inputs_captured"] is False
    turn, = row["turns"]
    assert "system_prompt_sha256" not in turn
    assert "prompt_layers" not in turn
    assert "looper_state" not in turn
    assert "state_block" not in turn
    assert "turn_context" not in turn
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["dialogs"][0]["inputs_captured"] is False


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_stable_dialog_records_the_recovered_whole_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    case = next(dialog for dialog in dialogs.FAKE_DIALOGS if dialog.id == "stable_whole_after_client")
    result, path = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                               stable_context=True)
    assert result["aborted"] is None
    row, = result["dialogs"]
    assert row["outcome"] == "pass", row
    assert row["state_views"] == ["whole", "whole", "whole", "unchanged"]
    turns = row["turns"]
    assert len(turns) == 4
    hashes = [turn["system_prompt_sha256"] for turn in turns]
    assert len(hashes) == 4 and len(set(hashes)) == 1 and len(hashes[0]) == 64
    assert all(isinstance(turn["prompt_layers"], dict) and turn["prompt_layers"].get("contract") for turn in turns)
    assert [turn["looper_state"]["view"] for turn in turns] == row["state_views"]
    assert [turn["looper_state"]["reason"] for turn in turns] == [
        "new session", "operator profile omitted for client turn",
        "another audience's block came after the last shown state", "unchanged"]
    assert turns[2]["state_block"].startswith("Looper state (rev ")
    assert "Eval Operator" in turns[2]["state_block"]
    assert turns[3]["state_block"].startswith("Looper state: unchanged since rev ")
    assert all(turn["turn_context"] is None for turn in turns)
    assert row["inputs_captured"] is True
    written = path.read_text(encoding="utf-8")
    assert json.loads(written) == result
    assert str(Path.home()) not in written
