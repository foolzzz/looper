"""Bridge → collect → independent parity → bot alert → one-setting rollback through serve."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import asyncpg
import pytest

from looper.agents.serve import seed_channel_configs
from evals.prompt_regression.runner import EvalError
from tests import msgflow_support as flow
from tests.db_support import DSN, fresh_database_without_recording
from tests.message_collection_serve_support import ROOT, assert_target, serving, until
from lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID, OPEN_ID, WORLD

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_isolated_serve_bridge_collect_parity_alert_and_rollback(tmp_path):
    import looper

    print("sys.executable=", sys.executable, "looper.__file__=", looper.__file__)
    assert Path(looper.__file__).resolve().parent == ROOT / "looper"
    root = tmp_path / "collection-service"
    root.mkdir()
    bridge = root / "bridge"
    bridge.mkdir()
    now = datetime.now(timezone.utc)
    first = flow.message("om_first_synthetic", sent_at=now-timedelta(minutes=30))
    second = flow.message("om_second_synthetic", sent_at=now-timedelta(minutes=30))
    root_msg = flow.message("om_root_synthetic", sent_at=now-timedelta(minutes=20), thread_id="omt_synthetic")
    reply = flow.message("om_reply_synthetic", sent_at=now-timedelta(minutes=10), thread_id="omt_synthetic",
                         parent_id="om_root_synthetic", root_id="om_root_synthetic")
    flow.append(bridge, "oc_collect_synthetic", first, second, root_msg, reply)
    flow.write_names(bridge, chats={"oc_collect_synthetic": {"name": "Synthetic group", "type": "group"}})
    world = root / "world.json"
    state = deepcopy(WORLD)
    state["events"] = []
    state["collector"] = {"auth": "ready", "identity": OPEN_ID,
                          "messages": {"oc_collect_synthetic": [first, second, root_msg, reply]}}
    world.write_text(json.dumps(state))
    with fresh_database_without_recording(str(DSN)) as (url, _):
        def sql(statement, *args, fetch=False):
            async def execute():
                conn = await asyncpg.connect(url)
                try:
                    return await conn.fetch(statement, *args) if fetch else await conn.execute(statement, *args)
                finally:
                    await conn.close()
            return asyncio.run(execute())

        async def seed():
            conn = await asyncpg.connect(url)
            try:
                await seed_channel_configs(conn)
                await conn.execute("UPDATE channel_configs SET enabled=false")
                await conn.execute("UPDATE channel_configs SET enabled=true,config=$1::jsonb WHERE channel_id='channel-lark'",
                    json.dumps({"profile": "cli_main", "allowlist_app_id": "cli_main", "allowed_open_ids": [OPEN_ID],
                                "operator_sender_id": OPEN_ID}))
                await conn.execute("INSERT INTO system_settings(key,value) VALUES('attention.enabled','true')")
            finally:
                await conn.close()
        asyncio.run(seed())

        with serving(root, url, world) as (api, ops):
            def post(action, body=None):
                assert_target(ops.path, ops.runtime_root, api)
                answer = api.post("/api/v1/" + action.replace(".", "/"), body or {})
                assert answer["status"] == "ok", answer
                return answer["result"]

            def observe(phase):
                observer = os.environ.get("LOOPER_COLLECTION_BROWSER")
                if not observer:
                    return
                directory = Path(observer)
                directory.mkdir(exist_ok=True)
                path = directory / "state.json"
                path.write_text(json.dumps({"phase": phase, "base_url": api.base_url, "token": api.token,
                    "config": str(ops.path), "runtime_root": str(ops.runtime_root)}))
                path.chmod(0o600)
                until(lambda: (directory / (phase + ".done")).exists(), timeout=180)

            for table in ("source_collection_settings", "source_parity_sessions", "source_stream_alerts"):
                assert not sql(f"SELECT 1 FROM {table}", fetch=True)
            calls_file = root / "lark-calls.jsonl"
            assert not any("collector-synthetic" in json.loads(line) for line in calls_file.read_text().splitlines())
            post("message_stream.configure", {"source_id": "synthetic", "ingest_mode": "bridge", "bridge_dir": str(bridge),
                "start_from": "beginning", "cron": "* * * * *", "user_open_id": OPEN_ID})
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 4)
            observe("bridge")
            bridge_position = dict(sql("SELECT bridge_offset,bridge_inode,bridge_size FROM source_chats", fetch=True)[0])
            post("message_collection.update", {"source_id": "synthetic", "fields": {"lark_profile": "collector-synthetic",
                "user_open_id": OPEN_ID, "alerts_enabled": True, "alert_channel": "lark", "alert_chat": OPEN_ID}})
            switched = post("message_collection.mode_update", {"source_id": "synthetic", "ingest_mode": "collect",
                "expected_generation": "0", "bridge_stopped": True})
            session = switched["parity_session_id"]
            assert session and switched["mode"] == "collect"
            accepted = post("message_collection.run_now", {"source_id": "synthetic"})
            until(lambda: sql("SELECT status FROM source_runs WHERE id=$1 AND finished_at IS NOT NULL",
                             int(accepted["run_id"]), fetch=True))
            view = post("message_collection.get", {"source_id": "synthetic"})
            assert view["latest_run"]["status"] == "ok", view
            assert view["parity"]["state"] == "matched" and not view["parity"]["empty_window"], (
                view["parity"]["state"], view["parity"]["reason"], view["parity"]["coverage"], view["parity"]["totals"])
            assert view["parity"]["totals"]["bridge"] == view["parity"]["totals"]["collect"] == 4
            assert len(sql("SELECT seq FROM source_events", fetch=True)) == 4
            assert len(sql("SELECT external_id FROM source_parity_members WHERE side='collect'", fetch=True)) == 4
            assert dict(sql("SELECT bridge_offset,bridge_inode,bridge_size FROM source_chats", fetch=True)[0]) == bridge_position
            # A real CLI subprocess reads the stored report, without account I/O or writes.
            before_calls = calls_file.read_bytes()
            result = subprocess.run([sys.executable, "-m", "looper.cli", "msgflow", "parity", "--source", "synthetic",
                "--session", session, "--json", "--config", str(ops.path), "--runtime-root", str(ops.runtime_root)],
                cwd=ROOT, capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["totals"]["intersection"] == 4
            assert calls_file.read_bytes() == before_calls
            # A new window's event gets one canonical seq; parity keeps rechecking the same frozen window.
            new_message = flow.message("om_new_synthetic", sent_at=datetime.now(timezone.utc)-timedelta(milliseconds=1))
            state["collector"]["messages"]["oc_collect_synthetic"].append(new_message)
            world.write_text(json.dumps(state))
            accepted = post("message_collection.run_now", {"source_id": "synthetic"})
            until(lambda: sql("SELECT status FROM source_runs WHERE id=$1 AND status='ok'",
                              int(accepted["run_id"]), fetch=True))
            assert len(sql("SELECT seq FROM source_events", fetch=True)) == 5
            state["collector"]["messages"]["oc_collect_synthetic"].remove(second)
            world.write_text(json.dumps(state))
            accepted = post("message_collection.run_now", {"source_id": "synthetic"})
            until(lambda: sql("SELECT status FROM source_runs WHERE id=$1 AND status='ok'",
                              int(accepted["run_id"]), fetch=True))
            drift = post("message_collection.get", {"source_id": "synthetic"})["parity"]
            assert drift["state"] == "drift" and drift["totals"]["bridge_only"] == 1, drift
            assert drift["session"] == session
            until(lambda: sql("SELECT 1 FROM source_stream_alerts WHERE kind='parity_drift' AND open_state='sent'", fetch=True))
            result = subprocess.run([sys.executable, "-m", "looper.cli", "msgflow", "parity", "--source", "synthetic",
                "--session", session, "--json", "--config", str(ops.path), "--runtime-root", str(ops.runtime_root)],
                cwd=ROOT, capture_output=True, text=True, timeout=20)
            assert result.returncode == 1 and json.loads(result.stdout)["totals"] == drift["totals"]
            attention = post("attention.summary")
            assert any(item["facts"].get("session") == session for section in attention["sections"]
                       for item in section["items"] if item["facts"].get("source") == "message_stream"), attention
            observe("drift")
            # Missing authentication prevents every account read after the auth check and opens one episode.
            state["collector"]["auth"] = "missing"
            world.write_text(json.dumps(state))
            for _ in range(2):
                accepted = post("message_collection.run_now", {"source_id": "synthetic"})
                until(lambda: sql("SELECT status FROM source_runs WHERE id=$1 AND status='auth_failed'",
                                  int(accepted["run_id"]), fetch=True))
            auth_alert = until(lambda: sql("SELECT * FROM source_stream_alerts WHERE kind='auth'", fetch=True))
            assert len(auth_alert) == 1
            assert not sql("SELECT 1 FROM source_stream_alerts WHERE kind='collector_failure'", fetch=True)
            until(lambda: sql("SELECT 1 FROM source_stream_alerts WHERE kind='auth' AND open_state='sent'", fetch=True))
            observe("auth")
            state["collector"]["auth"] = "ready"
            state["collector"]["messages"]["oc_collect_synthetic"].append(second)
            world.write_text(json.dumps(state))
            accepted = post("message_collection.run_now", {"source_id": "synthetic"})
            until(lambda: sql("SELECT status FROM source_runs WHERE id=$1 AND status='ok'",
                              int(accepted["run_id"]), fetch=True))
            until(lambda: sql("SELECT 1 FROM source_stream_alerts WHERE kind='auth' AND resolved_at IS NOT NULL", fetch=True))
            until(lambda: sql("SELECT 1 FROM source_stream_alerts WHERE kind='parity_drift' AND recovery_state='sent'", fetch=True))
            assert post("message_collection.get", {"source_id": "synthetic"})["parity"]["state"] == "matched"
            observe("matched")
            receipts = [json.loads(line) for line in (root / "lark-calls.jsonl.delivered.jsonl").read_text().splitlines()]
            alert_keys = [r["key"] for r in receipts if r.get("key", "").startswith("message-stream-alert:")]
            assert len(alert_keys) >= 4
            assert len(alert_keys) == len(set(alert_keys))
            assert all(r["profile"] == "cli_main" for r in receipts)
            post("message_collection.mode_update", {"source_id": "synthetic", "ingest_mode": "bridge"})
            assert post("message_stream.status")["sources"][0]["ingest_mode"] == "bridge"
            assert_target(ops.path, ops.runtime_root, api)
        user_reads = sum("collector-synthetic" in json.loads(line) for line in (root / "lark-calls.jsonl").read_text().splitlines())
        flow.append(bridge, "oc_collect_synthetic", flow.message("om_after_rollback", sent_at=datetime.now(timezone.utc)))
        # The bot's live ingress and overlapping startup history keep their independent SQLite boundary.
        at = datetime.now(timezone.utc)-timedelta(seconds=1)
        admitted = "Synthetic admitted bot event."
        ignored = "Synthetic unmentioned group event."
        state["events"] = [
            {"type": "im.message.receive_v1", "chat_id": "oc_ingress_synthetic", "chat_type": "group",
             "message_id": "om_admitted_synthetic", "message_type": "text", "sender_id": OPEN_ID,
             "sender_type": "user", "content": admitted, "mentions": [BOT_MENTION]},
            {"type": "im.message.receive_v1", "chat_id": "oc_ingress_synthetic", "chat_type": "group",
             "message_id": "om_ignored_synthetic", "message_type": "text", "sender_id": OPEN_ID,
             "sender_type": "user", "content": ignored},
        ]
        state["bot_history"] = {"oc_ingress_synthetic": [
            flow.message("om_admitted_synthetic", text=admitted, sent_at=at, sender=OPEN_ID,
                         mentions=[(BOT_OPEN_ID, "HelperBot")]),
            flow.message("om_ignored_synthetic", text=ignored, sent_at=at, sender=OPEN_ID),
        ]}
        state["history_two_pages"] = True
        world.write_text(json.dumps(state))

        async def debt():
            from looper.agents.channels.lark_inbox import LarkInbox
            inbox = await LarkInbox.open(root / "runtime/.local/lark/channel-lark/inbox.sqlite3")
            try:
                await inbox.checkpoint(at.timestamp()-60)
                await inbox.defer("oc_ingress_synthetic", at.timestamp()-60, at.timestamp()+1)
            finally:
                await inbox.close()
        asyncio.run(debt())
        with serving(root, url, world) as (api, ops):
            until(lambda: sql("SELECT 1 FROM chat_messages WHERE role='user' AND content LIKE $1",
                              "%"+admitted+"%", fetch=True))
            until(lambda: len(sql("SELECT seq FROM source_events", fetch=True)) == 6, timeout=80)
            assert len(sql("SELECT 1 FROM chat_messages WHERE role='user' AND content LIKE $1",
                           "%"+admitted+"%", fetch=True)) == 1
            assert not sql("SELECT 1 FROM chat_messages WHERE role='user' AND content LIKE $1",
                           "%"+ignored+"%", fetch=True)
            assert sql("SELECT ingest_mode FROM event_sources", fetch=True)[0]["ingest_mode"] == "bridge"
            assert sum("collector-synthetic" in json.loads(line) for line in (root / "lark-calls.jsonl").read_text().splitlines()) == user_reads
        calls = [json.loads(line) for line in (root / "lark-calls.jsonl").read_text().splitlines()]
        assert any("--as" in call and "bot" in call and any("overlap" in arg for arg in call) for call in calls)

        async def catchup_readback():
            from looper.agents.channels.lark_inbox import LarkInbox
            inbox = await LarkInbox.open(root / "runtime/.local/lark/channel-lark/inbox.sqlite3")
            try:
                assert "oc_ingress_synthetic" not in await inbox.debts()
                assert await inbox.processed("om_admitted_synthetic")
            finally:
                await inbox.close()
        asyncio.run(catchup_readback())


def test_two_isolated_servers_share_writer_lock_and_shutdown_interrupts_collection(tmp_path):
    from looper.db import message_collection as collection, message_stream as store
    from looper.agents.source_service import run_bridge_pass

    primary, secondary = tmp_path / "primary", tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    bridge = tmp_path / "bridge"
    flow.append(bridge, "oc_lock_synthetic", flow.message("om_lock_synthetic", sent_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
    barrier = tmp_path / "pause-auth"
    barrier.touch()
    state = deepcopy(WORLD)
    state["events"] = []
    state["collector"] = {"auth": "ready", "identity": OPEN_ID, "messages": {}, "pause_auth": str(barrier)}
    world = tmp_path / "world.json"
    world.write_text(json.dumps(state))
    with fresh_database_without_recording(str(DSN)) as (url, _):
        async def seed():
            pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
            try:
                source = await store.save_source("synthetic", {"ingest_mode": "bridge", "bridge_dir": str(bridge),
                    "bridge_start_from": "beginning", "lark_profile": "collector-synthetic", "user_open_id": OPEN_ID}, db=pool)
                await run_bridge_pass(source, db=pool)
                await collection.switch_mode("synthetic", "collect", expected_generation=0, bridge_stopped=True, db=pool)
                await pool.execute("UPDATE source_collection_settings SET last_fire_at=now()")
            finally:
                await pool.close()
        asyncio.run(seed())
        with serving(primary, url, world) as (api, ops):
            assert_target(ops.path, ops.runtime_root, api)
            accepted = api.post("/api/v1/message_collection/run_now", {"source_id": "synthetic"})
            assert accepted["status"] == "ok", accepted
            run_id = int(accepted["result"]["run_id"])
            until(lambda: (primary / "lark-calls.jsonl").exists() and "auth" in (primary / "lark-calls.jsonl").read_text())
            with serving(secondary, url, world) as (other, other_ops):
                assert_target(other_ops.path, other_ops.runtime_root, other)
                with pytest.raises(EvalError, match="answered 409"):
                    other.post("/api/v1/message_collection/run_now", {"source_id": "synthetic"})
            # Leaving while the local CLI waits must cancel/reap it and persist interruption.
        async def read():
            conn = await asyncpg.connect(url)
            try:
                return dict(await conn.fetchrow("SELECT status,error FROM source_runs WHERE id=$1", run_id))
            finally:
                await conn.close()
        result = asyncio.run(read())
        assert result["status"] == "interrupted", result
