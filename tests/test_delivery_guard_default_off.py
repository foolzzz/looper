"""RFC off contract on the existing Manager path: one error reply and one record."""

from looper.agents import chat_settings
from tests.test_manager_reply import _ask, _result, _tool_use


def test_empty_success_keeps_legacy_reply_and_single_record(monkeypatch, agent_db):
    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", False)
    script = (
        "for line in sys.stdin:\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'synthetic-session'}), flush=True)\n"
        + "".join("    " + line + "\n" for line in (_tool_use() + _result(None)).splitlines())
    )
    answer = _ask(script)
    assert answer["text"] == "Manager 出错，没能完成这次回复：Claude Code 没有返回内容"
    assert agent_db.start_record.await_count == agent_db.finish_record.await_count == 1
    assert agent_db.finish_record.await_args.kwargs["error"] == "no reply"
    assert "delivery_guard" not in agent_db.start_record.await_args.kwargs["metadata"]
