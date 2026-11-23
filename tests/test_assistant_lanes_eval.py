"""The lanes eval flag applies only through the disposable run's operator API."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from evals import assistant_experience as e


@pytest.mark.parametrize("lanes", [False, True])
def test_session_sets_lanes_only_when_requested(tmp_path, monkeypatch, lanes):
    import asyncpg

    conn = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(return_value=[]),
                           fetchval=AsyncMock(return_value=0), close=AsyncMock())
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(e.runner, "select_agent", Mock())
    case = replace(next(case for case in e.CASES if case.id == "time_now"), says=(), checks=())
    args = SimpleNamespace(fake=True, model="sonnet", manager_model=None, timeout=1, lanes=lanes)
    session = e.Session(args, case, SimpleNamespace(), False, None)
    api = Mock()
    api.post.return_value = {"result": {"enabled": False}}
    session.run(api, "unused", tmp_path / "world.json", tmp_path / "calls.jsonl", {}, tmp_path)
    writes = [call.args for call in api.post.call_args_list if "/manager_lanes/" in call.args[0]]
    assert writes == ([("/api/v1/manager_lanes/update", {"enabled": True})] if lanes else [])


def test_cli_records_lanes_in_the_run_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["assistant_experience", "--fake", "--lanes", "--cases", "time_now",
                                     "--pg-url", "postgresql://localhost:54331/lanes_eval",
                                     "--out-dir", str(tmp_path)])

    def scripted_run(args, stand_in, root, conversation, *, session):
        assert args.lanes is True
        return {"passed": not session.failing, "cost_usd": 0,
                "checks": {**{check.__name__: "synthetic failure" if session.failing else None
                              for check in session.case.checks}, "reply_received": None}}

    monkeypatch.setattr(e.lark, "run_once", scripted_run)
    monkeypatch.setattr(e.runner, "_git", lambda: {"commit": "synthetic"})
    assert e.main() == 0
    run = json.loads(next(tmp_path.glob("*-assistant-fake.json")).read_text())
    assert run["lanes"] is True
    assert run["mode"] == "fake"
