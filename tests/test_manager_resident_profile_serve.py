"""Real serve, PostgreSQL and CLI readback against a scripted resident; no model calls."""

import json
import subprocess
import sys

import pytest

from evals.prompt_regression import runner
from evals.prompt_regression.dialogs import _ask
from fake_claude import fake_claude
from manager_mcp_stand_in import _BODY
from looper.agents.operator_profile import CAP, HEADING
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("stable", [False, True])
def test_manager_turns_and_local_command_share_exact_profile_bytes(tmp_path, stable):
    runtime, bin_dir, vault = (tmp_path / name for name in ("runtime", "bin", "vault"))
    bin_dir.mkdir()
    (vault / "记忆").mkdir(parents=True)
    note = vault / "记忆" / "画像.md"
    discovery = "import sys\nif '--mcp-config' not in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
    fake_claude(bin_dir / "claude", discovery + "TURNS = [[], [], [], []]\n" + _BODY)
    with fresh_database_without_recording(DSN) as (database_url, _):
        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                                   log_path=tmp_path / "serve.log", home=tmp_path / "home") as api:
            expected = []
            for index, name in enumerate(("Ada", "Bee")):
                if index and stable:
                    # A fresh resident must get the whole stable block, with exactly the current note.
                    _ask(api, "profile-chat", "/new", timeout=30)
                # Each new resident state sees edits, skips frontmatter and keeps the compiler's output cap.
                value = name if index == 0 else name + "合成" * CAP
                note.write_text(f"---\nsummary: Hidden metadata\n---\n## Name\n{value}\n"
                                "- Working on a synthetic project [kind:: fact]\n"
                                "- No proactive messages outside working hours [kind:: preference]\n",
                                encoding="utf-8")
                if index == 0:
                    api.post("/api/v1/knowledge_vault/update", {"path": str(vault),
                        "profile_source": "记忆/画像.md"})
                    if stable:
                        # Stable-context has no dedicated UI control.
                        import asyncio
                        import asyncpg
                        async def enable_stable():
                            conn = await asyncpg.connect(database_url)
                            try:
                                await conn.execute("INSERT INTO system_settings (key,value) "
                                    "VALUES ('manager.stable_context','true')")
                            finally:
                                await conn.close()
                        asyncio.run(enable_stable())
                    api.post("/api/v1/profile_settings/update", {"enabled": True})
                result = subprocess.run([sys.executable, "-m", "looper.cli", "profile", "show",
                                         "--runtime-root", str(runtime)], capture_output=True, timeout=2)
                assert result.returncode == 0 and result.stderr == b""
                assert result.stdout.startswith(f"Name: {name}".encode())
                assert len(result.stdout.decode()) <= CAP and b"Hidden metadata" not in result.stdout
                if index:
                    assert len(result.stdout.decode()) == CAP
                expected.append(result.stdout)
                assert _ask(api, "profile-chat", f"Profile turn {index}", timeout=30) == "[]"
            # Removing a previously valid file must never serve an old cached profile.
            note.unlink()
            if stable:
                _ask(api, "profile-chat", "/new", timeout=30)
            missing = subprocess.run([sys.executable, "-m", "looper.cli", "profile", "show",
                                      "--runtime-root", str(runtime)], capture_output=True, timeout=2)
            assert (missing.returncode, missing.stdout, missing.stderr) == (0, b"", b"")
            assert _ask(api, "profile-chat", "Profile missing", timeout=30) == "[]"
        turns = [json.loads(line) for line in (runtime / "manager" / "turns.jsonl").read_text().splitlines()]
        assert len(turns) == 3
        for turn, output in zip(turns, expected):
            body = turn.split(HEADING + "\n", 1)[1].split("\n\n", 1)[0]
            assert body.encode() == output
            assert turn.startswith("Looper state (rev ") is stable
        assert HEADING not in turns[-1]
