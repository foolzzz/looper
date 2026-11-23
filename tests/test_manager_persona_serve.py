"""Isolated serve/HTTP/MCP readback, with a scripted runtime and no real model."""

import json

import pytest

from evals.prompt_regression import runner
from evals.prompt_regression.dialogs import _ask
from fake_claude import fake_claude
from manager_mcp_stand_in import _BODY
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_serve_renders_saved_persona_on_the_next_turn_and_after_restart(tmp_path):
    runtime, bin_dir = tmp_path / "runtime", tmp_path / "bin"
    bin_dir.mkdir()
    discovery = "import sys\nif '--mcp-config' not in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
    fake_claude(bin_dir / "claude", discovery + "TURNS = [[], []]\n" + _BODY)
    with fresh_database_without_recording(DSN) as (database_url, _):
        for start in range(2):
            with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                                       log_path=tmp_path / f"serve-{start}.log", home=tmp_path / "home") as api:
                if not start:
                    assert _ask(api, "persona-chat", "Before persona", timeout=30) == "[]"
                    api.post("/api/v1/manager_persona/update", {"name": "管家", "tone": "简洁、直接"})
                assert _ask(api, "persona-chat", "After persona", timeout=30) == "[]"
        texts = [json.loads(line) for line in (runtime / "manager" / "turns.jsonl").read_text().splitlines()]
        assert len(texts) == 3
        assert "Persona" not in texts[0]
        assert all("  Name: 管家\n  Tone: 简洁、直接\n\nProjects:" in text for text in texts[1:])
