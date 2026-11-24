"""Operator chat proposes through a real CLI server, then confirmation writes the synthetic vault."""

import asyncio
import json
import subprocess
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression import dialogs, runner
from looper.agents.ops.leads import WITHHELD_MEMORY_REPLY
from tests import memory_support as sup
from tests.db_support import DSN, fresh_database_without_recording
from tests.fake_claude import fake_claude
from tests import lark_cli_stand_in as lark

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
STATEMENT = "Weekly reports put the conclusion first, followed by the supporting details."
GROUP_ASK = "What did we decide about weekly reports?"


def _wait(check, detail):
    deadline = time.monotonic() + 45
    while True:
        found = check()
        if found:
            return found
        assert time.monotonic() < deadline, detail()
        time.sleep(.1)


def test_operator_chat_proposal_drafting_confirmation_and_group_backstop_through_serve(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
                        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
                        '"--runtime-root", sys.argv[2]]))\n')
    repo = sup.synthetic_vault(tmp_path)
    runtime, home, binaries = (tmp_path / name for name in ("runtime", "home", "bin"))
    home.mkdir()
    binaries.mkdir()
    world_file, lark_log = tmp_path / "world.json", tmp_path / "lark.jsonl"
    world = {**lark.WORLD, "events": []}
    world_file.write_text(json.dumps(world))
    fake_claude(binaries / "lark-cli", lark._STAND_IN.format(
        log=str(lark_log), world=str(world_file), bot=lark.BOT_OPEN_ID))
    response = {"decision": "change", "reason": "The operator explicitly asked for this preference",
                "title": "Conclusion first", "hard_rule": False, "instructions_seen": [],
                "changes": [sup.NOTE, {"op": "append_line", "file": "X1", "under": "## Index",
                                       "line": sup.INDEX["line"]}]}
    arguments = {"kind": "preference", "statement": STATEMENT, "quote": STATEMENT,
                 "keywords": ["weekly", "reports"]}
    fake_claude(binaries / "claude", f"ROOT={str(runtime)!r}\nREPLY={json.dumps(response)!r}\n"
                f"ARGUMENTS={arguments!r}\nGROUP_ASK={GROUP_ASK!r}\nSTATEMENT={STATEMENT!r}\n" + r'''
import json, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("2.1.328")
    sys.exit(0)
from evals.prompt_regression.fake_claude import _mcp, _Stdin, _user_text, _emit, _answer
if "--mcp-config" not in sys.argv:
    sys.stdin.readline()
    while not (Path(ROOT) / "draft-ready").exists():
        time.sleep(.05)
    model = sys.argv[sys.argv.index("--model") + 1]
    _emit({"type":"system","subtype":"init","session_id":"synthetic-draft","tools":[],"mcp_servers":[]})
    _emit({"type":"result","subtype":"success","is_error":False,"result":REPLY,
           "total_cost_usd":.004,"modelUsage":{model:{"costUSD":.004}}})
    sys.exit(0)
rpc, stdin = _mcp(sys.argv[1:]), _Stdin()
while (line := stdin.readline()) is not None:
    if not line.strip():
        continue
    content = _user_text(line)
    tools = rpc("tools/list", {})["result"]["tools"]
    with (Path(ROOT) / "tools.jsonl").open("a") as output:
        output.write(json.dumps({"input":content,"tools":[t["name"] for t in tools]}) + "\n")
    if GROUP_ASK in content:
        _answer("synthetic-manager", STATEMENT, .002)
        continue
    _emit({"type":"assistant","message":{"role":"assistant","content":[{
        "type":"tool_use","id":"memory-proposal","name":"mcp__looper__looper_memory_propose",
        "input":ARGUMENTS}]}})
    result = rpc("tools/call", {"name":"looper_memory_propose","arguments":ARGUMENTS})
    answer = json.loads(result["result"]["content"][0]["text"])
    _answer("synthetic-manager", "提议待你确认。" if answer["status"] == "ok" else json.dumps(answer), .002)
''')
    for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "coco"):
        binary = binaries / name
        binary.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(76)\n")
        binary.chmod(0o755)

    async def seed(url):
        from looper.agents.serve import seed_channel_configs

        conn = await asyncpg.connect(url)
        try:
            await seed_channel_configs(conn)
            await conn.execute("UPDATE channel_configs SET enabled=true, config=$1::jsonb "
                               "WHERE channel_id='channel-lark'", json.dumps({
                "profile": "cli_main", "allowed_open_ids": [lark.OPEN_ID], "allowlist_app_id": "cli_main",
                "operator_sender_id": lark.OPEN_ID}))
        finally:
            await conn.close()

    with fresh_database_without_recording(str(DSN)) as (url, _):
        asyncio.run(seed(url))
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                                   notes_root=home / "notes", log_path=tmp_path / "serve.log") as api:
            assert not api.base_url.endswith(":8767")
            api.post("/api/v1/knowledge_vault/update", {"path": str(repo), "writable": [], "rules_file": None})
            asked = api.post("/api/v1/memory/settings_put", {
                "root": "mem", "enabled": True, "sources": {"manager": True},
                "rules_files": ["mem/AGENTS.md"], "required_frontmatter": ["kind"],
                "worktree_root": str(tmp_path / "worktrees"),
                "targets": {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}})
            assert asked["status"] == "needs_confirmation", asked
            api.post("/api/v1/confirm", {"confirmation_id": asked["confirmation_id"]})
            reply = dialogs._ask(api, "memory-operator", STATEMENT, timeout=45, database_url=url)
            rows = api.post("/api/v1/memory/list", {})["result"]["candidates"]
            assert len(rows) == 1 and rows[0]["state"] == "drafting", (reply, rows)
            cid = rows[0]["id"]
            assert f"（已提议记入记忆 {cid}：" in reply and "待你确认）" in reply

            def get():
                return api.post("/api/v1/memory/get", {"candidate": cid})["result"]["candidate"]

            row = get()
            assert row["source_kind"] == "manager_turn" and row["source_class"] == "operator"
            assert len(row["sources"]) == 1 and row["sources"][0]["quote"] == STATEMENT
            assert row["sources"][0]["channel"] == "web"
            assert not (repo / sup.NOTE["path"]).exists()
            (runtime / "draft-ready").touch()
            _wait(lambda: get()["state"] == "pending", get)
            row = get()
            assert row["current"]["changes"] == [sup.NOTE, sup.INDEX]
            api.post("/api/v1/memory/confirm", {"candidate": cid, "content_hash": row["content_hash"]})
            _wait(lambda: get()["state"] == "written", get)
            commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                    text=True, check=True).stdout.strip()
            assert get()["commit"] == commit and (repo / sup.NOTE["path"]).read_text() == sup.NOTE["content"]
            assert (repo / "mine/notes.md").read_text() == "Personal synthetic note\n"

            world_file.write_text(json.dumps({**world, "events": [{
                "type": "im.message.receive_v1", "chat_id": "oc_memory_group", "chat_type": "group",
                "message_id": "om_memory_group", "message_type": "text", "sender_type": "user",
                "sender_id": lark.OPEN_ID, "content": f"@HelperBot {GROUP_ASK}", "mentions": [lark.BOT_MENTION]}]}))

            def posts():
                return [json.loads(line) for line in lark_log.read_text().splitlines()] if lark_log.exists() else []

            _wait(lambda: any("+messages-reply" in call and WITHHELD_MEMORY_REPLY in call for call in posts()), posts)
            listings = [json.loads(line) for line in (runtime / "tools.jsonl").read_text().splitlines()]
            direct = next(item for item in listings if GROUP_ASK not in item["input"])
            group = next(item for item in listings if GROUP_ASK in item["input"])
            assert "looper_memory_propose" in direct["tools"]
            assert "looper_memory_propose" not in group["tools"]
            assert not any("+messages-reply" in call and STATEMENT in call for call in posts())
