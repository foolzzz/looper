"""Real serve/HTTP/MCP path with scripted Claude; no model or external channel calls."""

import json
from pathlib import Path

import pytest

from evals.prompt_regression import runner
from evals.prompt_regression.dialogs import _ask
from fake_claude import fake_claude
from manager_mcp_stand_in import _BODY
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_serve_applies_context_settings_to_the_next_dashboard_turn(tmp_path):
    runtime, bin_dir = tmp_path / "runtime", tmp_path / "bin"
    bin_dir.mkdir()
    # Runtime discovery is scripted too; a new process never invokes an installed CLI.
    body = "import sys\nif '--mcp-config' not in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
    fake_claude(bin_dir / "claude", body + "TURNS = [[], []]\n" + _BODY)
    with fresh_database_without_recording(DSN) as (database_url, _):
        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                                   log_path=tmp_path / "serve.log", home=tmp_path / "home") as api:
            assert _ask(api, "synthetic-context", "Before context", timeout=30) == "[]"
            result = api.post("/api/v1/turn_context_settings/update", {
                "turn_context_enabled": True, "timezone": "Asia/Shanghai", "reply_language": "en"})["result"]
            assert result["turn_context_enabled"]
            assert _ask(api, "synthetic-context", "After context", timeout=30) == "[]"
            texts = [json.loads(line) for line in (runtime / "manager" / "turns.jsonl").read_text().splitlines()]
            assert "[Turn context]" not in texts[0] and "Channel: the Looper dashboard chat" in texts[0]
            assert "[Turn context]" in texts[1] and "Speaker: the operator" in texts[1]
            assert "Chat: the Looper dashboard\nReply in: English" in texts[1]
            assert "(Asia/Shanghai, UTC+08:00)" in texts[1]
            assert "Channel:" not in texts[1]
            args = json.loads((runtime / "manager" / "argv.json").read_text())
            flag = next(flag for flag in ("--append-system-prompt-file", "--system-prompt-file") if flag in args)
            prompt_file = Path(args[args.index(flag) + 1])
            assert "## Turn context" in prompt_file.read_text()
