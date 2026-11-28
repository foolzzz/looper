"""An isolated serve process keeps main's autonomous output off group chats over native CLI transport."""

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
from tests.db_support import DSN, fresh_database_without_recording
from tests.lark_cli_stand_in import BOT_MENTION, OPEN_ID

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("group", [True, False], ids=["shared-group", "operator-dm"])
def test_serve_autonomous_main_recipient_boundary(monkeypatch, tmp_path, lark_cli_stand_in, planner_claude, group):
    world_path = tmp_path / "world.json"
    world = json.loads(world_path.read_text())
    world["events"] = [{"type": "im.message.receive_v1", "chat_id": "oc_synthetic_group" if group else "oc_synthetic_dm",
        "chat_type": "group" if group else "p2p", "message_id": "om_synthetic_egress", "message_type": "text",
        "sender_id": OPEN_ID, "sender_type": "user", "content": "@HelperBot start background work",
        "mentions": [BOT_MENTION]}]
    world_path.write_text(json.dumps(world))
    fake = planner_claude("REQUESTED_REPLY", autonomous={1: "AUTONOMOUS_REPLY"})
    bin_dir, runtime, home = tmp_path / "bin", tmp_path / "runtime", tmp_path / "home"
    fake_claude(bin_dir / "claude", "import sys\nif '--version' in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
                + Path(fake.bin + ".py").read_text())
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
            conn = await asyncpg.connect(url)
            try:
                await seed_channel_configs(conn)
                await conn.execute("UPDATE channel_configs SET enabled=false")
                await conn.execute("UPDATE channel_configs SET enabled=true,config=$1::jsonb WHERE channel_id='channel-lark'",
                    json.dumps({"profile": "cli_main", "allowlist_app_id": "cli_main", "allowed_open_ids": [OPEN_ID],
                                "operator_sender_id": OPEN_ID}))
            finally:
                await conn.close()
        asyncio.run(seed())
        port = runner.free_port()
        assert port != 8767
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir,
                home=home, port=port, log_path=tmp_path / "serve.log", notes_root=home / "notes") as api:
            assert urlsplit(api.base_url).port == port != 8767
            deadline = time.monotonic() + 45
            while True:
                rows = sql("SELECT id,output_text FROM llm_records WHERE metadata->>'autonomous'='true' AND status='completed'")
                events = sql("SELECT state,reason,record_id FROM manager_events WHERE kind='autonomous'")
                calls = ([json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
                         if lark_cli_stand_in.exists() else [])
                sent_autonomous = any("AUTONOMOUS_REPLY" in arg for call in calls for arg in call)
                # The withheld row is inserted first; record_id is filled by the next statement.
                linked = bool(events) and events[0]["record_id"] is not None
                if rows and ((group and linked) or ((not group) and sent_autonomous)):
                    break
                assert time.monotonic() < deadline, (tmp_path / "serve.log").read_text()[-10000:]
                time.sleep(.1)
            assert [row["output_text"] for row in rows] == ["AUTONOMOUS_REPLY"]
            if group:
                assert [(row["state"], row["reason"]) for row in events] == [("skipped", "withheld_not_operator")]
                assert events[0]["record_id"] == rows[0]["id"]
                assert not sql("SELECT id FROM chat_messages WHERE content='AUTONOMOUS_REPLY'")
            else:
                assert events == []
        calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
        sends = [call for call in calls if any(name in call for name in ("+messages-send", "+messages-reply"))]
        assert any("REQUESTED_REPLY" in arg for call in sends for arg in call)
        assert any("AUTONOMOUS_REPLY" in arg for call in sends for arg in call) is not group
        assert any("consume" in call for call in calls), "the native lark-cli event consumer must run"
