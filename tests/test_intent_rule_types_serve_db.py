"""One real looper serve path for the MR review type: list, explicit rule activation, then classification."""
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
from looper.agents import subscriptions
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
for ref, marker in re.findall(r'\[(m\d+)\][^\n]*?(MR_SYNTHETIC|SCOPE_SYNTHETIC|BUG_SYNTHETIC)', text):
    items.append({'type': {'MR_SYNTHETIC': 'mr_review', 'SCOPE_SYNTHETIC': 'scope_note',
                           'BUG_SYNTHETIC': 'bug_triage'}[marker],
                  'title': marker, 'summary': 'Synthetic request for the offline smoke',
                  'confidence': 'high', 'sources': [ref]})
reply = json.dumps({'intents': items, 'instructions_seen': []}) if items else 'NONE'
model = sys.argv[sys.argv.index('--model') + 1]
model = {'sonnet': 'claude-sonnet-5-5', 'opus': 'claude-opus-5-5'}.get(model, model)
usage = {'input_tokens': 20, 'output_tokens': 10, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
for event in ({'type': 'system', 'subtype': 'init', 'session_id': 'offline-types', 'model': model,
               'tools': [], 'mcp_servers': []},
              {'type': 'assistant', 'session_id': 'offline-types', 'message': {
                  'id': 'synthetic-output', 'role': 'assistant', 'model': model,
                  'content': [{'type': 'text', 'text': reply}], 'usage': usage}},
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'offline-types',
               'result': reply, 'total_cost_usd': .002, 'usage': usage, 'num_turns': 1}):
    print(json.dumps(event), flush=True)
'''


def test_mr_review_classification_runs_through_isolated_serve(monkeypatch, tmp_path, lark_cli_stand_in):
    parsed = urlsplit(str(DSN))
    if (parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port not in (54331, 5432)
            or unquote(parsed.path.lstrip("/")) in ("looper", "loopx")):
        raise ValueError("requires a local disposable PostgreSQL server")
    monkeypatch.setattr(runner, "_SERVE", '''import sys
from datetime import datetime, timezone
from functools import partial
from looper import ops_config
from looper.agents import serve
from looper.cli import main
config = ops_config.load(ops_config.Flags(runtime_root=sys.argv[2], port=int(sys.argv[1])))
ops_config.write_private(config.path, ops_config.render(config))
serve.SourceService = partial(serve.SourceService, interval=.2)
serve.IntentPushService = partial(serve.IntentPushService, interval=.2,
    clock=lambda: datetime.now(timezone.utc).replace(hour=14))
sys.exit(main(['serve','--host','127.0.0.1','--port',sys.argv[1],'--runtime-root',sys.argv[2]]))
''')
    world = tmp_path / "world.json"
    value = json.loads(world.read_text())
    value["events"] = []
    world.write_text(json.dumps(value))
    root = tmp_path / "type-serve"
    root.mkdir()
    home, bin_dir, bridge = (root / name for name in ("home", "bin", "bridge"))
    for path in (home, bin_dir, bridge):
        path.mkdir()
    seen = root / "processor.jsonl"
    fake_claude(bin_dir / "claude", f"SEEN = {str(seen)!r}\n" + _FAKE_PROCESSOR)
    for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk"):
        fake_claude(bin_dir / name, "import sys\nsys.exit(76)\n")
    chats = {"ou_peer_smoke": {"name": "示例私聊", "type": "p2p"},
             "oc_followed_smoke": {"name": "示例产品群", "type": "group"}}
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
                await conn.execute(
                    "UPDATE channel_configs SET enabled=true,config=$1::jsonb WHERE channel_id='channel-lark'",
                    json.dumps({"profile": "cli_main", "allowlist_app_id": "cli_main", "allowed_open_ids": [OPEN_ID],
                                "operator_sender_id": OPEN_ID}))
                await conn.execute("INSERT INTO system_settings(key,value) VALUES('user.timezone','UTC')")
            finally:
                await conn.close()
        asyncio.run(seed())
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 8793))
                port = 8793
            except OSError:
                port = runner.free_port()
        assert port != 8767
        with runner.running_server(database_url=url, runtime_root=root / "runtime", bin_dir=bin_dir, home=home,
                notes_root=home / "notes", env=environment, port=port, startup_timeout=90,
                log_path=root / "serve.log") as api:
            assert urlsplit(api.base_url).port == port and port != 8767
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

            post("message_stream/configure", {"source_id": "smoke", "name": "Synthetic bridge", "ingest_mode": "bridge",
                "bridge_dir": str(bridge), "start_from": "beginning", "cron": "* * * * *", "timezone": "UTC",
                "user_open_id": OPEN_ID})
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 2, "initial bridge import")
            post("proactive_delivery/update", {"enabled": True})
            post("intents/settings_put", {"enabled": True})
            post("intent_group_reads/settings_put", {"source_id": "smoke", "enabled": True})
            post("intent_group_reads/put", {"source_id": "smoke", "chat_id": "oc_followed_smoke"})
            hidden = post("intent_types/list", {})
            assert "mr_review" not in [row["key"] for row in hidden["types"]]
            with pytest.raises(runner.EvalError, match="404") as refused:
                api.post("/api/v1/intent_types/history", {"key": "mr_review"})
            assert "unknown intent type" in str(refused.value)
            assert "识别评审" not in str(refused.value)
            with pytest.raises(runner.EvalError, match="404") as archived:
                api.post("/api/v1/intent_types/archive", {"key": "mr_review"})
            assert "unknown intent type" in str(archived.value)
            assert post("intent_rules/settings_get", {})["runtime_ready"] is True
            activation = post("intent_rules/preview", {"expected_generation": 0,
                "change": {"kind": "settings", "enabled": True}})
            assert sql("SELECT value FROM system_settings WHERE key='intents.rules.enabled'", fetch=True) == []
            post("intent_rules/confirm", {"preview_id": activation["preview_id"],
                "acceptance": activation["required_acceptance"]})
            shown = post("intent_types/list", {})
            assert "mr_review" in [row["key"] for row in shown["types"]]
            with pytest.raises(runner.EvalError, match="409") as builtin:
                api.post("/api/v1/intent_types/archive", {"key": "mr_review"})
            assert "cannot be archived" in str(builtin.value)
            post("intent_types/create", {"key": "scope_note", "name": "范围备注", "guidance": "用户自己记下的一条说明。",
                                        "priority": "quiet"})
            project = post("project_admin/create", {"name": "Type recognition smoke", "work_dir": str(root)})["project"]
            fields = subscriptions.preset("intents", source_id="smoke", zone_name="UTC")
            fields.update(cron="0 0 1 1 *", notify_target={"channel": "lark", "chat": OPEN_ID})
            schedule = post("schedule_admin/create", {"project_id": project["project_id"], "fields": fields})["schedule"]
            sid = schedule["schedule_id"]
            post("schedule_admin/confirm_target", {"schedule_id": sid,
                                                    "config_version": schedule["subscription"]["config_version"]})
            sent = datetime.now(timezone.utc) + timedelta(seconds=1)
            flow.append(bridge, "oc_followed_smoke",
                flow.message("om_mr", text="MR_SYNTHETIC", sent_at=sent, sender=flow.ALICE),
                flow.message("om_bug", text="BUG_SYNTHETIC", sent_at=sent, sender=flow.ALICE),
                flow.message("om_scope", text="SCOPE_SYNTHETIC", sent_at=sent, sender=OPEN_ID))
            sql("UPDATE event_sources SET last_run_at=NULL WHERE id='smoke'")
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 5, "synthetic messages imported")
            run = post("schedule_admin/run", {"schedule_id": sid})["run"]

            def finished(run_id):
                row = sql("SELECT status,error FROM schedule_runs WHERE id=$1", run_id, fetch=True)[0]
                return row if row["status"] in ("completed", "failed", "timed_out") else None

            ended = until(lambda: finished(run["run_id"]), "first intent run")
            assert ended["status"] == "completed", dict(ended)
            recognized = {(row["title"], row["type_key"], row["via"]) for row in sql(
                "SELECT title,type_key,via FROM intents ORDER BY id", fetch=True)}
            assert recognized == {("MR_SYNTHETIC", "mr_review", "followed_group"),
                                  ("BUG_SYNTHETIC", "bug_triage", "followed_group"),
                                  ("SCOPE_SYNTHETIC", "scope_note", "followed_group")}
            assert sql("SELECT count(*) AS n FROM tasks", fetch=True)[0]["n"] == 0
            prompts = [json.loads(line) for line in seen.read_text().splitlines()]
            first = next(row for row in prompts if "MR_SYNTHETIC" in row["input"] and "later" not in row["input"])
            assert "MR review requests" in first["instructions"]
            sql("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
            sent = datetime.now(timezone.utc) + timedelta(seconds=2)
            flow.append(bridge, "oc_followed_smoke",
                flow.message("om_mr_later", text="MR_SYNTHETIC later", sent_at=sent, sender=flow.ALICE))
            sql("UPDATE event_sources SET last_run_at=NULL WHERE id='smoke'")
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 6, "later message imported")
            run = post("schedule_admin/run", {"schedule_id": sid})["run"]
            ended = until(lambda: finished(run["run_id"]), "second intent run")
            assert ended["status"] == "completed", dict(ended)
            counts = sql("SELECT extract_counts FROM subscription_runs WHERE run_id=$1", run["run_id"], fetch=True)[0][0]
            counts = json.loads(counts) if isinstance(counts, str) else counts
            assert counts["dropped"].get("unknown_type") == 1
            assert "rules_type" not in counts["dropped"]
            assert sql("SELECT count(*) AS n FROM intents WHERE type_key='mr_review'", fetch=True)[0]["n"] == 1
            prompts = [json.loads(line) for line in seen.read_text().splitlines()]
            last = next(row for row in prompts if "later" in row["input"])
            assert "MR review requests" not in last["instructions"]
            hidden_again = post("intent_types/list", {})
            assert "mr_review" not in [row["key"] for row in hidden_again["types"]]
