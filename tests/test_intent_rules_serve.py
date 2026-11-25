"""A fresh serve exposes the preview protocol without any model or external writes."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_rule_management_round_trip_on_fresh_serve(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_SERVE", """import sys
from looper import ops_config
config = ops_config.load(ops_config.Flags(runtime_root=sys.argv[2], port=int(sys.argv[1])))
assert config.port != 8767
ops_config.write_private(config.path, ops_config.render(config))
""" + runner._SERVE)
    runtime, bins = tmp_path / "runtime", tmp_path / "bin"
    bins.mkdir()
    fake_claude(bins / "claude", "import sys\nprint('2.1.0')\nsys.exit(0)\n")
    with fresh_database_without_recording(DSN) as (database_url, _):
        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bins,
                                   log_path=tmp_path / "serve.log", home=tmp_path / "home") as api:
            def call(action, body=None):
                return api.post("/api/v1/intent_rules/" + action, body or {})["result"]
            initial = call("list")
            assert initial["generation"] == 0 and initial["rules"] == []
            assert initial["runtime_ready"] is True
            command = {"kind": "rule_create", "rule": {"enabled": True, "position": 1,
                "intent_type": "any", "scope": {"kind": "all"}, "conditions": [],
                "action": "push_for_confirmation", "project_id": None, "template_key": None}}
            preview = call("preview", {"expected_generation": 0, "change": command})
            assert call("list")["rules"] == []
            confirmation = {"preview_id": preview["preview_id"], "acceptance": preview["required_acceptance"]}
            result = call("confirm", confirmation)
            assert result["generation"] == 1 and call("confirm", confirmation) == result
            rid = result["rules"][0]["id"]
            assert call("history", {"rule_id": rid})["versions"][0]["version"] == 1
            tokens = [call("preview", {"expected_generation": 1, "change": {"kind": "rule_disable",
                "rule_id": rid, "expected_version": 1}})["preview_id"] for _ in range(2)]
            def confirm(token):
                try:
                    return call("confirm", {"preview_id": token, "acceptance": []})
                except runner.EvalError as exc:
                    return str(exc)
            with ThreadPoolExecutor(max_workers=2) as workers:
                answers = list(workers.map(confirm, tokens))
            assert sum(isinstance(answer, dict) for answer in answers) == 1
            assert any("409" in str(answer) and "conflict" in str(answer) for answer in answers)
            with pytest.raises(runner.EvalError, match="409"):
                call("preview", {"expected_generation": 2, "change": {"kind": "settings", "enabled": True}})
            assert call("list")["generation"] == 2


def test_rules_off_preserves_push_and_manual_build_on_fresh_serve(tmp_path, monkeypatch):
    import asyncio
    import json
    import time
    from datetime import datetime, timezone

    import asyncpg

    from looper.agents import subscriptions
    from tests import subscription_support as stream
    from tests.leads_support import intent

    # Keep the actual bus, router, HTTP dispatch and push poller. Only channel I/O is offline.
    monkeypatch.setattr(runner, "_SERVE", '''import json, sys
from functools import partial
from pathlib import Path
from looper.agents import serve
from looper.agents.channels.base import PostReceipt
from looper.agents.message import MessageType
from looper.cli import main
from looper import ops_config
config = ops_config.load(ops_config.Flags(runtime_root=sys.argv[2], port=int(sys.argv[1])))
assert config.port != 8767
ops_config.write_private(config.path, ops_config.render(config))
trace = Path(sys.argv[2]) / 'offline-events.jsonl'
class OfflineRegistry(serve.ChannelRegistry):
    async def post(self, channel, target, text, **kwargs):
        with trace.open('a') as output:
            output.write(json.dumps({'kind':'post','channel':channel,'chat':target.id,'text':text})+'\\n')
        return PostReceipt(True, external_ids=('synthetic-push',))
    async def edit(self, *args, **kwargs):
        return PostReceipt(True, external_ids=('synthetic-push',))
class ObservedBus(serve.MessageBus):
    async def publish(self, envelope):
        if envelope.message_type == MessageType.TASK_START:
            with trace.open('a') as output:
                output.write(json.dumps({'kind':'start','task_id':envelope.payload['task_id']})+'\\n')
        return await super().publish(envelope)
serve.ChannelRegistry = OfflineRegistry
serve.MessageBus = ObservedBus
serve.IntentPushService = partial(serve.IntentPushService, interval=.1)
sys.exit(main(['serve','--host','127.0.0.1','--port',sys.argv[1],'--runtime-root',sys.argv[2]]))
''')
    bins, runtime, work = tmp_path / "bin", tmp_path / "runtime", tmp_path / "work"
    bins.mkdir()
    work.mkdir()
    fake_claude(bins / "claude", "import sys\nprint('2.1.0')\nsys.exit(0)\n")
    with fresh_database_without_recording(DSN) as (url, _):
        async def seed():
            async with asyncpg.create_pool(url, min_size=1, max_size=2) as pool:
                await stream.source(pool)
                await stream.lark_targets(pool)
                await pool.execute("UPDATE channel_configs SET config=config || jsonb_build_object('operator_sender_id',$1::text) WHERE channel_id='channel-lark'", stream.DM)
                return await stream.add(pool, "Synthetic review request", kind="p2p", chat_id="ou_peer_smoke",
                                        sent_at=datetime.now(timezone.utc))
        seq = asyncio.run(seed())
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bins,
                log_path=tmp_path / "serve.log", home=tmp_path / "home") as api:
            def post(action, body):
                return api.post("/api/v1/" + action.replace(".", "/"), body)["result"]
            post("intents.settings_put", {"enabled": True})
            project = post("project_admin.create", {"name": "Offline manual review", "work_dir": str(work)})["project"]
            fields = subscriptions.preset("intents", source_id=stream.SOURCE, zone_name="UTC")
            fields.update(cron="0 0 1 1 *", notify_target={"channel": "lark", "chat": stream.DM})
            schedule = post("schedule_admin.create", {"project_id": project["project_id"], "fields": fields})["schedule"]
            schedule = post("schedule_admin.get", {"schedule_id": schedule["schedule_id"]})["schedule"]
            post("schedule_admin.confirm_target", {"schedule_id": schedule["schedule_id"],
                "config_version": schedule["subscription"]["config_version"]})
            async def stored(sql, *args, create=False):
                conn = await asyncpg.connect(url)
                try:
                    if create:
                        return await intent(conn, [seq], schedule_id=schedule["schedule_id"], project_id=project["project_id"])
                    return await conn.fetchval(sql, *args)
                finally:
                    await conn.close()
            iid = asyncio.run(stored("", create=True))
            deadline = time.monotonic() + 15
            while asyncio.run(stored("SELECT count(*) FROM intent_posts WHERE intent_id=$1 AND state='sent'", iid)) != 1:
                assert time.monotonic() < deadline, "offline intent push did not arrive"
                time.sleep(.05)
            built = post("intents.build", {"intent_id": str(iid)})
            assert asyncio.run(stored("SELECT state FROM intents WHERE id=$1", iid)) == "tasked"
            assert asyncio.run(stored("SELECT origin_channel_id FROM tasks WHERE id=$1", built["task_id"])) == ""
            assert asyncio.run(stored("SELECT origin_reply_route IS NULL FROM tasks WHERE id=$1", built["task_id"]))
            assert asyncio.run(stored("SELECT count(*) FROM intent_rule_hits")) == 0
            assert post("intent_rules.settings_get", {})["enabled"] is False
            events = [json.loads(line) for line in (runtime / "offline-events.jsonl").read_text().splitlines()]
            assert any(row["kind"] == "post" and row["chat"] == stream.DM for row in events)
            assert any(row["kind"] == "start" and row["task_id"] == built["task_id"] for row in events)
