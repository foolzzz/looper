"""The actual CLI server drafts privately, exposes the review and writes only after confirmation."""

import asyncio
import json
import subprocess
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.agents import memory_model as model
from tests import memory_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("enabled", [False, True], ids=["off-unchanged", "draft-confirm-write"])
def test_drafting_through_cli_serve(monkeypatch, tmp_path, enabled):
    from tests.fake_claude import fake_claude

    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
                        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
                        '"--runtime-root", sys.argv[2]]))\n')
    repo = sup.synthetic_vault(tmp_path)
    runtime, home, binaries = (tmp_path / name for name in ("runtime", "home", "bin"))
    home.mkdir()
    binaries.mkdir()
    response = {"decision": "change", "reason": "Explicit operator preference", "title": "Conclusion first",
                "hard_rule": False, "instructions_seen": [], "changes": [sup.NOTE,
                {"op": "append_line", "file": "X1", "under": "## Index", "line": sup.INDEX["line"]}]}
    fake_claude(binaries / "claude", f"REPLY = {json.dumps(response)!r}\n" + r'''
import json, sys
if "--version" in sys.argv:
    print("2.1.328")
    sys.exit(0)
sys.stdin.readline()
model = sys.argv[sys.argv.index("--model") + 1]
print(json.dumps({"type":"system","subtype":"init","session_id":"synthetic-draft","tools":[],"mcp_servers":[]}),flush=True)
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":REPLY,
                  "total_cost_usd":.004,"modelUsage":{model:{"costUSD":.004}}}),flush=True)
''')
    for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "coco", "lark-cli"):
        script = binaries / name
        script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(76)\n")
        script.chmod(0o755)

    async def seed(url):
        conn = await asyncpg.connect(url)
        try:
            settings = model.MemorySettings(root="mem", enabled=enabled, branch="main", commit_prefix="mem",
                rules_files=("mem/AGENTS.md",), required_frontmatter=("kind",),
                targets={model.Kind.PREFERENCE: model.Target(model.Shape.NOTE, folders=("prefs",), index="prefs/_index.md")})
            for key, value in (("knowledge_vault", {"path": str(repo)}), ("memory_write", settings.to_json())):
                await conn.execute("INSERT INTO system_settings(key,value) VALUES($1,$2)", key, json.dumps(value))
            return model.format_id(await sup.candidate(conn))
        finally:
            await conn.close()

    with fresh_database_without_recording(str(DSN)) as (url, _):
        cid = asyncio.run(seed(url))
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                                   notes_root=home / "notes", log_path=tmp_path / "serve.log") as api:
            assert int(api.base_url.rsplit(":", 1)[1]) >= 8890
            def get():
                return api.post("/api/v1/memory/get", {"candidate": cid})["result"]["candidate"]
            def wait(state):
                deadline = time.monotonic() + 30
                while True:
                    found = get()
                    if found["state"] == state:
                        return found
                    assert time.monotonic() < deadline, found
                    time.sleep(.1)
            if not enabled:
                time.sleep(.4)
                row = get()
                assert row["state"] == "drafting"
                assert [e["action"] for e in row["events"]] == ["propose"]
                async def records():
                    conn = await asyncpg.connect(url)
                    try:
                        return await conn.fetchval("SELECT count(*) FROM llm_records WHERE run_kind='memory_draft'")
                    finally:
                        await conn.close()
                assert asyncio.run(records()) == 0
                assert not (repo / sup.NOTE["path"]).exists()
                return
            row = wait("pending")
            assert row["current"]["changes"] == [sup.NOTE, sup.INDEX]
            assert not (repo / sup.NOTE["path"]).exists()
            api.post("/api/v1/memory/confirm", {"candidate": cid, "content_hash": row["content_hash"]})
            row = wait("written")
            commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            assert row["commit"] == commit
            assert (repo / sup.NOTE["path"]).read_text() == sup.NOTE["content"]
            assert (repo / "mine/notes.md").read_text() == "Personal synthetic note\n"
