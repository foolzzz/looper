"""Delivery recovery is measured through the Manager dialog harness."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import dialogs
from looper.agents.delivery_guard import FOLLOWUP_MESSAGE
from tests.test_eval_manager_agent import DialogApi


def test_delivery_guard_is_an_explicit_dialog_option():
    parser = cli._parser()
    assert parser.parse_args(["dialogs"]).delivery_guard is False
    assert parser.parse_args(["dialogs", "--delivery-guard"]).delivery_guard is True


def test_tools_only_dialog_enables_guard_and_scripts_a_read_without_text():
    (dialog,) = dialogs.select(["tools_only_no_text"])
    posts = []
    dialog.seed(SimpleNamespace(post=lambda path, body: posts.append((path, body))), "project-synthetic")
    assert posts == [("/api/v1/delivery_guard/update", {"enabled": True})]
    assert dialog.says[0].fake == (("looper_projects", {"action": "list"}), ("__reply__", {"text": ""}))
    assert {"tools", "replies", "delivery_guard"} <= set(dialog.tags)


@pytest.mark.parametrize("fault", [None, "empty", "one_record", "no_annotation", "wrong_followup", "new_session"])
def test_tools_only_check_requires_reply_and_a_recorded_followup(monkeypatch, fault):
    (dialog,) = dialogs.select(["tools_only_no_text"])
    records = [
        {"id": 1, "input_summary": "read the projects", "output_chars": 0,
         "session_id": "session-synthetic", "metadata": {}},
        {"id": 2, "input_summary": FOLLOWUP_MESSAGE, "output_chars": 8,
         "session_id": "session-synthetic", "metadata": {"delivery_guard": {"followup": 1, "for": "m-synthetic"}}},
    ]
    if fault == "one_record":
        records.pop()
    elif fault == "no_annotation":
        records[1]["metadata"] = {}
    elif fault == "wrong_followup":
        records[1]["input_summary"] = "a fresh user message"
    elif fault == "new_session":
        records[1]["session_id"] = "another-session"
    monkeypatch.setattr(dialogs, "_manager_runs", lambda _: records)
    look = SimpleNamespace(reply="" if fault == "empty" else "已查到项目。", fake=True,
                           actions=lambda: [{"tool": "looper_projects", "status": "ok", "risk": "read"}])
    problem = dialog.says[0].check(look)
    assert (problem is None) is (fault is None), problem


def test_guard_option_enables_each_disposable_server_and_reports_followups(monkeypatch, tmp_path):
    apis = {}

    class GuardApi(DialogApi):
        def post(self, path, body):
            if path == "/api/v1/delivery_guard/update":
                self.posts.append((path, deepcopy(body)))
                return {"status": "ok", "result": body}
            if path == "/api/v1/runs/list":
                return {"result": {"records": [{"id": 1, "metadata": {"delivery_guard": {"followup": 1}}}]}}
            return super().post(path, body)

    @contextmanager
    def database(_):
        api = GuardApi()
        apis[str(id(api))] = api
        yield str(id(api)), None

    @contextmanager
    def server(*, database_url, **_):
        yield apis[database_url]

    def answer(api, dialog, **_):
        assert ("/api/v1/delivery_guard/update", {"enabled": True}) in api.posts
        return {"id": dialog.id, "outcome": "pass", "success": True, "turns": [], "problems": []}

    monkeypatch.setattr(dialogs, "fresh_database", database)
    monkeypatch.setattr(dialogs, "running_server", server)
    monkeypatch.setattr(dialogs, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(dialogs, "_usage", lambda _: (0.0, {}))
    monkeypatch.setattr(dialogs, "_git", lambda: {})
    monkeypatch.setattr(dialogs, "run_dialog", answer)
    result, _ = dialogs.run(mode="fake", dialogs=dialogs.select(["status", "ask_project"]),
                            admin_url="unused", out_dir=tmp_path, delivery_guard=True, model="sonnet")
    assert result["aborted"] is None
    assert result["delivery_guard"] is True
    assert len(apis) == 2
    assert [row["followups"] for row in result["dialogs"]] == [1, 1]
