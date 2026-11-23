"""RFC off contract on the existing Manager path: one error reply and one record."""

import hashlib
import json
from unittest.mock import AsyncMock

from looper.agents import chat_settings
from looper.agents import claude_cli
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


def test_disabled_guard_keeps_prompt_wire_input_and_command_bytes(monkeypatch, agent_db):
    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", False)
    # Pin the machine-dependent isolation inputs; keep every settings field in the command hash.
    monkeypatch.setattr(claude_cli, "_outside_memory", lambda *_, **__: [
        "/outside/CLAUDE.md", "/outside/.claude/rules/**", "/user/.claude/CLAUDE.md"])
    write = AsyncMock(wraps=claude_cli.write_user_turn)
    reminder = AsyncMock(side_effect=AssertionError("a disabled guard must never write a reminder"))
    monkeypatch.setattr(claude_cli, "write_user_turn", write)
    monkeypatch.setattr(claude_cli.ResidentClaude, "write_mid_turn", reminder)
    script = (
        "for line in sys.stdin:\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'synthetic-session'}), flush=True)\n"
        + "".join("    " + line + "\n" for line in (_tool_use() * 18 + _result(None)).splitlines())
    )
    answer = _ask(script)
    assert write.await_count == 1
    reminder.assert_not_awaited()
    root = agent_db.start_record.await_args.kwargs["metadata"]["cwd"]
    captured = {
        "prompt": answer["prompts"],
        "wire": [call.args[1] for call in write.await_args_list],
        "command": [argument.replace(root, "<runtime-root>") for argument in answer["argv"]],
    }
    digests = {name: hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
               for name, value in captured.items()}
    # Measured against the pre-wiring Manager on main, with the same synthetic fixture.
    assert digests == {
        "prompt": "ba7610d5ac6b4b2de4051caa85deaf7d3151aaeb3acad2701cf3d4143dc0c868",
        "wire": "8145e064476d655926851bdca3fc9fa871af6f177b9265b0e6ffd460299d3b48",
        "command": "7edb2cf2ddfca62297eed3bb2831b8800c19478ef4c63ead868a3c480b1e8206",
    }
    assert answer["text"] == "Manager 出错，没能完成这次回复：Claude Code 没有返回内容"
    assert agent_db.start_record.await_count == agent_db.finish_record.await_count == 1
    assert "delivery_guard" not in agent_db.start_record.await_args.kwargs["metadata"]
