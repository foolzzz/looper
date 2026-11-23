"""Continuous and one-off group recognition through isolated serve, bridge files and offline CLIs."""
from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.agents import extraction, subscriptions
from looper.agents.serve import seed_channel_configs
from tests import msgflow_support as flow
from tests.db_support import DSN, fresh_database_without_recording
from tests.fake_claude import fake_claude
from lark_cli_stand_in import OPEN_ID

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_FAKE_PROCESSOR = r'''
import json, re, sys
if '--version' in sys.argv:
    print('2.1.329 (offline runtime)'); sys.exit(0)
if sys.argv[1:] == ['auth', '--help']:
    print('Usage: claude auth status'); sys.exit(0)
if sys.argv[1:3] == ['auth', 'status']:
    print('{"loggedIn":true}'); sys.exit(0)
if '--output-format' not in sys.argv:
    sys.exit(76)
turn = json.loads(sys.stdin.readline())['message']['content']
text = turn if isinstance(turn, str) else ''.join(block.get('text', '') for block in turn)
flag = next(flag for flag in ('--system-prompt-file', '--append-system-prompt-file') if flag in sys.argv)
prompt = open(sys.argv[sys.argv.index(flag) + 1]).read()
with open(SEEN, 'a') as seen:
    seen.write(json.dumps({'input': text, 'instructions': prompt}) + '\n')
items = []
for ref, marker in re.findall(r'\[(m\d+)\][^\n]*?(REQUEST_SYNTHETIC|BUG_SYNTHETIC|DM_CONTROL|MENTION_CONTROL)', text):
    items.append({'type':'bug_triage' if marker == 'BUG_SYNTHETIC' else 'dev_task',
        'title':marker, 'summary':'Synthetic request for the offline smoke', 'confidence':'high', 'sources':[ref]})
reply = json.dumps({'intents':items, 'instructions_seen':[]}) if items else 'NONE'
model = sys.argv[sys.argv.index('--model') + 1]
model = {'sonnet':'claude-sonnet-5-5','opus':'claude-opus-5-5'}.get(model, model)
usage = {'input_tokens':20,'output_tokens':10,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}
for event in ({'type':'system','subtype':'init','session_id':'offline-group','model':model,'tools':[],'mcp_servers':[]},
    {'type':'assistant','session_id':'offline-group','message':{'id':'synthetic-output','role':'assistant',
        'model':model,'content':[{'type':'text','text':reply}],'usage':usage}},
    {'type':'result','subtype':'success','is_error':False,'session_id':'offline-group','result':reply,
        'total_cost_usd':.002,'usage':usage,'num_turns':1}):
    print(json.dumps(event), flush=True)
'''


def test_followed_groups_run_and_revoke_through_isolated_serve(monkeypatch, tmp_path, lark_cli_stand_in):
    parsed = urlsplit(str(DSN))
    if (parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port not in (54331, 5432)
            or unquote(parsed.path.lstrip("/")) in ("looper", "loopx")):
        raise ValueError("requires a local disposable PostgreSQL server")
    # Keep the public CLI startup and production services; only shorten their polling and pin delivery to daytime.
    monkeypatch.setattr(runner, "_SERVE", '''import sys
from datetime import datetime, timezone
from functools import partial
from looper.agents import serve
from looper.cli import main
serve.SourceService = partial(serve.SourceService, interval=.2)
serve.IntentPushService = partial(serve.IntentPushService, interval=.2,
    clock=lambda: datetime.now(timezone.utc).replace(hour=14))
sys.exit(main(['serve','--host','127.0.0.1','--port',sys.argv[1],'--runtime-root',sys.argv[2]]))
''')
    world = tmp_path / "world.json"
    value = json.loads(world.read_text())
    value["events"] = []
    world.write_text(json.dumps(value))
    root = tmp_path / "group-serve"
    root.mkdir()
    home, bin_dir, bridge = (root / name for name in ("home", "bin", "bridge"))
    for path in (home, bin_dir, bridge):
        path.mkdir()
    seen = root / "processor.jsonl"
    fake_claude(bin_dir / "claude", f"SEEN = {str(seen)!r}\n" + _FAKE_PROCESSOR)
    for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk"):
        fake_claude(bin_dir / name, "import sys\nsys.exit(76)\n")
    chats = {"ou_peer_smoke": {"name": "示例私聊", "type": "p2p"},
             "oc_followed_smoke": {"name": "示例产品群", "type": "group"},
             "oc_other_smoke": {"name": "示例其他群", "type": "group"}}
    for chat in chats:
        flow.append(bridge, chat, flow.message(f"om_initial_{chat}", sent_at=datetime.now(timezone.utc)))
    flow.write_names(bridge, chats=chats)
    environment = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
        "GROK_HOME": str(home / ".grok"), "CURSOR_CONFIG_DIR": str(home / ".cursor-config"),
        "CURSOR_DATA_DIR": str(home / ".cursor-data"), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"), "LOOPER_TELEGRAM_BOT_TOKEN": ""}
    with fresh_database_without_recording(str(DSN)) as (url, _):
        def sql(statement, *args, fetch=False):
            async def run():
                conn = await asyncpg.connect(url)
                try:
                    return await conn.fetch(statement, *args) if fetch else await conn.execute(statement, *args)
                finally:
                    await conn.close()
            return asyncio.run(run())

        async def seed():
            conn = await asyncpg.connect(url)
            try:
                await seed_channel_configs(conn)
                await conn.execute("UPDATE channel_configs SET enabled=false")
                await conn.execute("UPDATE channel_configs SET enabled=true,config=$1::jsonb WHERE channel_id='channel-lark'",
                    json.dumps({"profile":"cli_main", "allowlist_app_id":"cli_main", "allowed_open_ids":[OPEN_ID],
                                "operator_sender_id":OPEN_ID}))
                await conn.execute("INSERT INTO system_settings(key,value) VALUES('user.timezone','UTC')")
            finally:
                await conn.close()
        asyncio.run(seed())
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 8791))
                port = 8791
            except OSError:
                port = runner.free_port()
        with runner.running_server(database_url=url, runtime_root=root / "runtime", bin_dir=bin_dir, home=home,
                notes_root=home / "notes", env=environment, port=port, startup_timeout=60,
                log_path=root / "serve.log") as api:
            def post(domain, body):
                result = api.post(f"/api/v1/{domain}", body)
                if result["status"] == "needs_confirmation":
                    result = api.post("/api/v1/confirm", {"confirmation_id": result["confirmation_id"]})
                assert result["status"] == "ok", result
                return result["result"]

            def until(check, description):
                deadline = time.monotonic() + 45
                while True:
                    answer = check()
                    if answer:
                        return answer
                    assert time.monotonic() < deadline, description
                    time.sleep(.1)

            post("message_stream/configure", {"source_id":"smoke", "name":"Synthetic bridge", "ingest_mode":"bridge",
                "bridge_dir":str(bridge), "start_from":"beginning", "cron":"* * * * *", "timezone":"UTC",
                "user_open_id":OPEN_ID})
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 3, "initial bridge import")
            post("proactive_delivery/update", {"enabled":True})
            post("intents/settings_put", {"enabled":True})
            post("intent_group_reads/settings_put", {"source_id":"smoke", "enabled":True})
            preview = post("intent_group_reads/preview", {"source_id":"smoke", "chat_id":"oc_followed_smoke"})
            assert preview["group"]["messages_24h"] == 0  # the first bridge pass imports existing file contents
            assert {row["origin"] for row in sql("SELECT origin FROM source_events", fetch=True)} == {"import"}
            followed = post("intent_group_reads/put", {"source_id":"smoke", "chat_id":"oc_followed_smoke"})
            assert [row["chat_id"] for row in followed["groups"]] == ["oc_followed_smoke"]
            project = post("project_admin/create", {"name":"Group recognition smoke", "work_dir":str(root)})["project"]
            fields = subscriptions.preset("intents", source_id="smoke", zone_name="UTC")
            fields.update(cron="0 0 1 1 *", notify_target={"channel":"lark", "chat":OPEN_ID})
            schedule = post("schedule_admin/create", {"project_id":project["project_id"], "fields":fields})["schedule"]
            sid = schedule["schedule_id"]
            post("schedule_admin/confirm_target", {"schedule_id":sid,
                                                    "config_version":schedule["subscription"]["config_version"]})
            sent = datetime.now(timezone.utc) + timedelta(seconds=1)
            flow.append(bridge, "oc_followed_smoke",
                flow.message("om_request", text="REQUEST_SYNTHETIC", sent_at=sent),
                flow.message("om_bug", text="BUG_SYNTHETIC", sent_at=sent),
                flow.message("om_chatter", text="今天食堂有饺子", sent_at=sent),
                flow.message("om_mention", text="MENTION_CONTROL", sent_at=sent, mentions=[(OPEN_ID,"操作者")]),
                flow.message("om_bot", text="REQUEST_SYNTHETIC bot", sent_at=sent, sender_type="app", sender="cli_synthetic"))
            flow.append(bridge, "oc_other_smoke", flow.message("om_other", text="REQUEST_SYNTHETIC unlisted", sent_at=sent))
            flow.append(bridge, "ou_peer_smoke", flow.message("om_dm", text="DM_CONTROL", sent_at=sent))
            sql("UPDATE event_sources SET last_run_at=NULL WHERE id='smoke'")
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 10, "live bridge append")
            run = post("schedule_admin/run", {"schedule_id":sid})["run"]
            def finished(run_id):
                row = sql("SELECT status,error FROM schedule_runs WHERE id=$1", run_id, fetch=True)[0]
                return row if row["status"] in ("completed", "failed", "timed_out") else None

            ended = until(lambda: finished(run["run_id"]), "first intent run")
            assert ended["status"] == "completed", dict(ended)
            counts = sql("SELECT extract_counts FROM subscription_runs WHERE run_id=$1", run["run_id"], fetch=True)[0][0]
            counts = json.loads(counts) if isinstance(counts, str) else counts
            assert (counts["new"], counts["group_read"], counts["group_over_cap"]) == (4,3,0)
            recognized = sql("SELECT title,type_key,via FROM intents ORDER BY id", fetch=True)
            assert {(row["title"], row["type_key"], row["via"]) for row in recognized} == {
                ("REQUEST_SYNTHETIC","dev_task","followed_group"), ("BUG_SYNTHETIC","bug_triage","followed_group"),
                ("MENTION_CONTROL","dev_task","subscription"), ("DM_CONTROL","dev_task","subscription")}
            until(lambda: len(sql("SELECT id FROM proactive_deliveries WHERE source='intent'", fetch=True)) == 4,
                  "intent delivery decisions")
            decisions = sql("SELECT urgency,chat_kind,chat,decision FROM proactive_deliveries WHERE source='intent'", fetch=True)
            assert all(tuple(row.values()) == ("normal","direct",OPEN_ID,"send") for row in decisions)
            assert sql("SELECT read,skipped FROM intent_group_read_days", fetch=True)[0]["read"] == 3
            post("intent_group_reads/delete", {"source_id":"smoke", "chat_id":"oc_followed_smoke"})
            sent = datetime.now(timezone.utc) + timedelta(seconds=1)
            flow.append(bridge, "oc_followed_smoke", flow.message("om_revoked", text="REVOKED_SYNTHETIC", sent_at=sent))
            flow.append(bridge, "ou_peer_smoke", flow.message("om_dm_after", text="DM_CONTROL", sent_at=sent))
            sql("UPDATE event_sources SET last_run_at=NULL WHERE id='smoke'")
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 12, "append after revocation")
            run = post("schedule_admin/run", {"schedule_id":sid})["run"]
            ended = until(lambda: finished(run["run_id"]), "second intent run")
            assert ended["status"] == "completed", dict(ended)
            assert len(sql("SELECT id FROM intents WHERE via='followed_group'", fetch=True)) == 2
            assert len(sql("SELECT id FROM intents", fetch=True)) == 5
            launches = [json.loads(line) for line in seen.read_text().splitlines()]
            assert len(launches) == 2 and extraction.GROUP_REQUESTS_NOTE in launches[0]["instructions"]
            assert extraction.GROUP_REQUESTS_NOTE not in launches[1]["instructions"]
            assert "REVOKED_SYNTHETIC" not in launches[1]["input"]
            assert post("intent_group_reads/list", {"source_id":"smoke"})["groups"] == []
            outgoing = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
            sends = [call for call in outgoing if "+messages-send" in call]
            assert sends and all("--user-id" in call and OPEN_ID in call for call in sends)

            # A separate switch enables a confirmed one-off read of the unlisted group's live messages.
            listed_before = sql("SELECT * FROM intent_group_reads ORDER BY source_id,chat_id", fetch=True)
            cursor_before = sql("SELECT cursor_seq FROM subscriptions WHERE schedule_id=$1", sid, fetch=True)[0][0]
            state = post("intent_recognitions/settings_put", {"source_id":"smoke", "enabled":True})
            assert state["enabled"] is True and state["recognitions"] == []
            preview = post("intent_recognitions/preview", {"source_id":"smoke", "chat_id":"oc_other_smoke"})
            assert preview["events"] == 1 and preview["truncated"] is None
            assert (datetime.fromisoformat(preview["range_to"]) - datetime.fromisoformat(preview["range_from"])).days == 7
            recognition = post("intent_recognitions/start", {"source_id":"smoke", "chat_id":"oc_other_smoke",
                "since":preview["range_from"], "until":preview["range_to"]})["recognition"]
            assert recognition["id"].startswith("R-")
            def recognized_once():
                records = post("intent_recognitions/list", {"source_id":"smoke"})["recognitions"]
                return records[0] if records and records[0]["state"] != "running" else None
            record = until(recognized_once, "one-off recognition completion")
            assert record["state"] == "done", record
            assert record["events_in_range"] == record["events_read"] == 1
            assert record["cost_usd"] > 0 and len(record["intents"]) == 1
            rid = int(record["id"].removeprefix("R-"))
            one_off = sql("SELECT id,title,type_key,via,recognition_id,run_id,record_id FROM intents "
                          "WHERE recognition_id=$1", rid, fetch=True)
            assert len(one_off) == 1
            item = one_off[0]
            assert (item["title"],item["type_key"],item["via"],item["recognition_id"],item["run_id"]) == (
                "REQUEST_SYNTHETIC", "dev_task", "on_request", rid, None)
            assert record["intents"][0]["id"] == f"I-{item['id']:04}"
            assert sql("SELECT * FROM intent_group_reads ORDER BY source_id,chat_id", fetch=True) == listed_before
            assert sql("SELECT cursor_seq FROM subscriptions WHERE schedule_id=$1", sid, fetch=True)[0][0] == cursor_before
            # Wait until the live push poller has visited the one-off row, proving its suppression path ran.
            until(lambda: sql("SELECT expires_at FROM intents WHERE id=$1", item["id"], fetch=True)[0][0],
                  "one-off row visited by the push service")
            assert sql("SELECT p.id FROM intent_posts p JOIN intents i ON i.id=p.intent_id "
                       "WHERE i.recognition_id=$1", rid, fetch=True) == []
            recorded = sql("SELECT agent_id,run_kind,input_text FROM llm_records WHERE id=$1",
                           item["record_id"], fetch=True)
            assert len(recorded) == 1 and recorded[0]["agent_id"] == f"processor:intent-recognition-{rid}"
            assert recorded[0]["run_kind"] == "processor"
            reference = json.loads(recorded[0]["input_text"])
            assert reference["kind"] == "intent_recognition" and reference["recognition_id"] == rid
            assert reference["events"] == 1 and reference["sha256"] and reference["chars"] > 0
            launches = [json.loads(line) for line in seen.read_text().splitlines()]
            assert len(launches) == 3 and extraction.GROUP_REQUESTS_NOTE in launches[-1]["instructions"]
            assert "REQUEST_SYNTHETIC unlisted" in launches[-1]["input"]
