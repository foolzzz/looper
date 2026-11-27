"""An isolated serve process authorizes high-risk calls and confirmations by sender identity.

Real serve, HTTP and MCP path with a scripted Claude and the lark-cli stand-in (native CliTransport): the inbound
group messages arrive through the Lark channel's event consumer. The turn-context switch is off (its default)."""

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

from evals.prompt_regression import runner
from fake_claude import fake_claude
from looper.agents.serve import seed_channel_configs
from manager_mcp_stand_in import _BODY
from tests.db_support import DSN, fresh_database_without_recording
from tests.lark_cli_stand_in import BOT_MENTION, OPEN_ID, OTHER

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

DELETE = {"action": "delete", "project": "sample"}
CONFIRM = {"confirmation_id": "<confirmation>"}
# The group's messages in order: who says it, and what the scripted model then calls.
SCRIPT = [
    (OPEN_ID, [("looper_projects", DELETE)]),          # the operator asks: pending
    (OTHER, [("looper_confirm", CONFIRM)]),            # another allowed sender's next message: refused
    (OTHER, [("looper_projects", DELETE)]),            # they cannot ask for it themselves: refused
    (OPEN_ID, [("looper_projects", DELETE)]),          # the operator asks again: pending
    (OPEN_ID, [("looper_confirm", CONFIRM)]),          # and confirms it: runs
]


def _event(index, sender):
    return {"type": "im.message.receive_v1", "chat_id": "oc_synthetic_group", "chat_type": "group",
            "message_id": f"om_synthetic_{index}", "message_type": "text", "sender_id": sender,
            "sender_type": "user", "content": f"@HelperBot step {index}", "mentions": [BOT_MENTION]}


@pytest.mark.parametrize("routed", [False, True], ids=["group-main-session", "group-routed-session"])
def test_serve_authorizes_by_sender_with_the_switch_off(monkeypatch, tmp_path, lark_cli_stand_in, routed):
    world_path = tmp_path / "world.json"
    world = json.loads(world_path.read_text())
    world["events"] = [_event(index, sender) for index, (sender, _) in enumerate(SCRIPT)]
    world_path.write_text(json.dumps(world))
    bin_dir, runtime, home = tmp_path / "bin", tmp_path / "runtime", tmp_path / "home"
    turns = [calls for _, calls in SCRIPT]
    head = "import sys\nif '--mcp-config' not in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
    fake_claude(bin_dir / "claude", head + f"TURNS = {turns!r}\n" + _BODY)
    monkeypatch.setattr(runner, "_SERVE", """import sys
from looper.cli import main
sys.exit(main(['serve', '--host', '127.0.0.1', '--port', sys.argv[1], '--runtime-root', sys.argv[2]]))
""")
    with fresh_database_without_recording(str(DSN)) as (url, _):
        def sql(statement, *args):
            async def run():
                conn = await asyncpg.connect(url)
                try:
                    return await conn.fetch(statement, *args)
                finally:
                    await conn.close()
            return asyncio.run(run())

        async def seed():
            from looper.db import projects

            conn = await asyncpg.connect(url)
            try:
                await seed_channel_configs(conn)
                await conn.execute("UPDATE channel_configs SET enabled=false")
                await conn.execute(
                    "UPDATE channel_configs SET enabled=true,config=$1::jsonb WHERE channel_id='channel-lark'",
                    json.dumps({"profile": "cli_main", "allowlist_app_id": "cli_main",
                                "allowed_open_ids": [OPEN_ID, OTHER], "operator_sender_id": OPEN_ID}))
                await conn.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                                   "ON CONFLICT (key) DO UPDATE SET value = $2",
                                   "manager.session_routing.enabled", "true" if routed else "false")
                assert not await conn.fetchval("SELECT value FROM system_settings WHERE key='manager.turn_context.enabled'") == "true"
            finally:
                await conn.close()
            pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
            try:
                await projects.create_project(name="sample", work_dir="/srv/sample", pool=pool)
            finally:
                await pool.close()
        asyncio.run(seed())
        port = runner.free_port()
        assert port != 8767
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home, port=port,
                                   log_path=tmp_path / "serve.log", notes_root=home / "notes") as api:
            assert urlsplit(api.base_url).port == port != 8767
            deadline = time.monotonic() + 90
            while True:
                rows = sql("SELECT tool, status, error_code, result FROM manager_actions "
                           "WHERE tool IN ('looper_projects', 'looper_confirm') ORDER BY id")
                if len(rows) >= len(SCRIPT) or (routed and time.monotonic() > deadline - 70) or time.monotonic() > deadline:
                    break
                time.sleep(.2)
            kept = bool(sql("SELECT 1 FROM projects WHERE name='sample'"))
            summary = [(row["tool"], row["status"], row["error_code"]) for row in rows]
            print("RESULT", "routed" if routed else "main", summary, "project_kept", kept)
            if routed:
                # The default Manager's routed non-main sessions list none of these tools: no sender reaches them,
                # and the project is kept. Every message was delivered to the scripted model.
                assert not any(item[1] in ("needs_confirmation", "ok") for item in summary), summary
                assert kept
                (work,) = (runtime / ".local" / "manager-sessions").glob("*/work")
                assert len((work / "turns.jsonl").read_text().splitlines()) == len(SCRIPT)
                assert "looper_projects" not in json.loads((work / "tools.json").read_text())
                return
            assert summary == [
                ("looper_projects", "needs_confirmation", None),
                ("looper_confirm", "error", "forbidden"),
                ("looper_projects", "error", "forbidden"),
                ("looper_projects", "needs_confirmation", None),
                ("looper_projects", "ok", None),  # the confirmation runs the call and is audited as it
            ], (summary, (tmp_path / "serve.log").read_text()[-4000:])
            assert not kept  # only the operator's own confirmation ran the delete
            assert json.loads(rows[1]["result"]) == {"reason": "operator_only"}
