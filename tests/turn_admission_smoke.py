"""Isolated serve/browser smoke; uses scripted CLI peers and a synthetic Lark adapter.

Run as ``python -m tests.turn_admission_smoke --static-dir <built-dashboard> --output <evidence>``
with LOOPER_TEST_DATABASE_URL naming a disposable PostgreSQL server. No model is called.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from urllib.parse import urlsplit

from evals.prompt_regression import runner
from looper.agents.operator_auth import cookie_name
from tests.db_support import fresh_database_without_recording
from tests.test_admission_serve_db import PROVIDER, _CLI, _events, _limits, _query as _database_query, _until

_PATCH = r'''
import asyncio,json,os
from pathlib import Path
from looper.agents import admission,completion_events,gate_service
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.base import DeliveryReceipt,PostReceipt
from looper.agents.channels.chat_history import record_reply
from looper.agents.channels.status_line import status_kind,StatusKind
from looper.agents.message import MessageEnvelope,MessageType,MANAGER_ADDRESS,TASK_ROUTER_ADDRESS,channel_address
state=Path(os.environ["TURN_ADMISSION_SMOKE_STATE"])
admission.RECHECK_SECONDS=.05
class Adapter:
    channel_id="channel-lark"
    channel_type="lark"
    def __init__(self,bus):
        self.bus=bus
        self.tasks=[asyncio.create_task(self.consume()),asyncio.create_task(self.inbox())]
    def healthcheck(self): return {"healthy":True}
    def prepare_stop(self): pass
    async def stop(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
    async def notify(self,chat,text,*,idempotency_key=None):
        return PostReceipt(True)
    async def consume(self):
        async for envelope in self.bus.subscribe(self.channel_id):
            progress=envelope.message_type is MessageType.STATUS_UPDATE
            if progress and status_kind(envelope.payload) is not StatusKind.PROGRESS: continue
            with (state/"lark.jsonl").open("a") as output:
                output.write(json.dumps({"kind":"progress" if progress else "answer",
                    "text":envelope.payload.get("text"),"metadata_keys":sorted(envelope.metadata)},ensure_ascii=False)+"\n")
            await record_reply("lark",envelope,DeliveryReceipt(channel="lark",success=True))
    async def inbox(self):
        seen=set()
        while True:
            for path in sorted(state.glob("inbox-*.json")):
                if path.name in seen: continue
                seen.add(path.name)
                data=json.loads(path.read_text())
                if data["kind"]=="wake":
                    await admission.wake()
                elif data["kind"]=="completion":
                    target=completion_events.Target("manager","channel-web",data["thread"])
                    await self.bus.publish(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS,target=MANAGER_ADDRESS,
                        message_type=MessageType.GOAL_STATUS,payload={"task_id":data["task_id"],"goal_id":"synthetic-goal",
                            "status":"completed","summary":"合成任务已完成"},metadata={"task_title":"合成任务",
                            "origin_thread":data["thread"],"origin_channel_id":"channel-web"}))
                    await asyncio.sleep(.2)
                    await self.bus.publish(completion_events.wake(MANAGER_ADDRESS,target))
                else:
                    await self.bus.publish(MessageEnvelope.create(source=channel_address("lark"),target=MANAGER_ADDRESS,
                        message_type=MessageType.USER_MESSAGE,payload={"text":data["text"],
                            "channel_context":{"chat_id":"synthetic-lark-chat","chat_type":"p2p","sender_id":"synthetic-operator"}},
                        metadata={"origin_channel_id":"channel-lark"}))
                (state/(path.name+".done")).touch()
            await asyncio.sleep(.03)
original_init=ChannelRegistry.__init__
def initialize(self,bus):
    original_init(self,bus)
    self._channels["channel-lark"]=Adapter(bus)
ChannelRegistry.__init__=initialize
'''


def _query(*args, **kwargs):
    # Playwright sync owns an event loop in this thread; PostgreSQL uses its own.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_database_query, *args, **kwargs).result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    admin_url = os.environ["LOOPER_TEST_DATABASE_URL"]
    assert urlsplit(admin_url).port == 54331, "use the disposable test PostgreSQL server"
    assert (args.static_dir / "index.html").is_file()
    args.output.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

    with tempfile.TemporaryDirectory(prefix="looper-turn-admission-") as temporary:
        root = Path(temporary)
        state, bin_dir, runtime, home = (root / name for name in ("state", "bin", "runtime", "home"))
        for directory in (state, bin_dir, runtime, home):
            directory.mkdir()
        cli_script = root / "peer.py"
        cli_script.write_text(_CLI.replace('    if text.startswith("[PLAN]") or',
            '    log("turn",text=text)\n    if text.startswith("[PLAN]") or'))
        executable = bin_dir / "claude"
        executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(cli_script))} '
                              f'{shlex.quote(str(state))} "$@"\n')
        executable.chmod(0o755)
        original_serve = runner._SERVE
        runner._SERVE = _PATCH + original_serve
        try:
            with fresh_database_without_recording(admin_url) as (url, _):
                _query(url, "INSERT INTO system_settings(key,value) VALUES "
                    "('engine.failure_typing','record'),('engine.limit_signals.enabled','true'),"
                    "('engine.admission.enabled','true'),('manager.lanes.enabled','true'),"
                    "('manager.completion_turns','dm_only')", execute=True)
                _query(url, "UPDATE llm_providers SET max_concurrency=1,name='合成账号' WHERE id=$1", PROVIDER, execute=True)
                with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir,
                        home=home, notes_root=home / "notes", log_path=root / "serve.log",
                        env={"TURN_ADMISSION_SMOKE_STATE": str(state),
                             "PYTEST_LOOPER_STATIC_DIR": str(args.static_dir.resolve())}) as api:
                    work = root / "work"
                    work.mkdir()
                    project = api.post("/api/v1/project_admin/create", {"name":"合成准入验证","work_dir":str(work)})[
                        "result"]["project"]["project_id"]
                    api.post("/api/v1/project_admin/update", {"project_id":project,"fields":{"task_workspaces":"in_place"}})
                    def task(variant):
                        task_id = api.post("/api/v1/tasks/create", {"project_id":project,
                            "requirement":"Finish admission_variant=" + variant})["result"]["task_id"]
                        api.post("/api/v1/ops/task_control/start", {"task_id":task_id})
                        return task_id
                    serial = 0
                    def inbox(kind, **fields):
                        nonlocal serial
                        serial += 1
                        (state / f"inbox-{serial:03}.json").write_text(json.dumps({"kind":kind,**fields}))
                    def lark():
                        path = state / "lark.jsonl"
                        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
                    def ready(page):
                        try:
                            page.wait_for_load_state("networkidle", timeout=1000)
                        except BrowserTimeout:
                            pass  # The dashboard keeps its live SSE subscriptions open.
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(viewport={"width":1280,"height":900})
                        context.add_cookies([{"name":cookie_name(urlsplit(api.base_url).port),"value":api.token,
                                              "url":api.base_url}])
                        page = context.new_page()
                        page.goto(api.base_url + "/#chat")
                        ready(page)
                        page.locator("textarea").wait_for()
                        thread = page.evaluate("localStorage.getItem('looper-chat-session-id')")
                        assert thread
                        print("browser ready", flush=True)
                        ordinary = task("ordinary")
                        _until(lambda:_events(state,"ordinary"),"running stand-in Worker")
                        before = len(_events(state,"manager",kind="turn"))
                        page.locator("textarea").fill("请确认排队后会继续回答。")
                        page.locator("textarea").press("Enter")
                        _until(lambda:_limits(api)["waiting"].get("interactive",0)==1,"dashboard turn waiting")
                        assert len(_events(state,"manager",kind="turn")) == before
                        page.screenshot(path=str(args.output / "dashboard-wait-1280.png"))
                        (state / "release-ordinary-a").touch()
                        page.get_by_text("fixture reply",exact=False).first.wait_for(timeout=30000)
                        _until(lambda:_limits(api)["running"]==0,"normal turns released")
                        page.screenshot(path=str(args.output / "dashboard-answer-1280.png"))
                        print("dashboard queue and reply passed", flush=True)
                        quota = task("quota")
                        _until(lambda:_events(state,"quota"),"quota Worker")
                        (state / "release-quota-a").touch()
                        _until(lambda:_limits(api)["state"]=="frozen","429 freezes account")
                        queued = task("queued")
                        inbox("lark",text="请在额度恢复后回答这条合成消息。")
                        _until(lambda:any("额度已用完" in row["text"] for row in lark()),"one Lark freeze progress")
                        assert sum("额度已用完" in row["text"] for row in lark()) == 1
                        assert _limits(api)["waiting"].get("user_task",0) >= 1
                        assert not _events(state,"queued")
                        page.locator("textarea").fill("这条消息也应保留到额度恢复。")
                        page.locator("textarea").press("Enter")
                        # The routed main session is still answering Lark: the dashboard waits behind it.
                        page.goto(api.base_url + "/#settings/providers")
                        ready(page)
                        page.get_by_text("合成账号",exact=True).first.wait_for(timeout=15000)
                        page.get_by_text("合成账号",exact=True).first.scroll_into_view_if_needed()
                        account = page.get_by_role("listitem").filter(has=page.get_by_text("合成账号", exact=True))
                        account.screenshot(path=str(args.output / "account-frozen-1280.png"))
                        page.set_viewport_size({"width":390,"height":844})
                        page.get_by_text("合成账号",exact=True).first.scroll_into_view_if_needed()
                        account.screenshot(path=str(args.output / "account-frozen-390.png"))
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                        assert _query(url,"SELECT content FROM chat_messages WHERE content LIKE '%额度已用完%'") == []
                        api.post("/api/v1/providers/limits_clear",{"provider_id":PROVIDER})
                        _until(lambda:any(row["kind"]=="answer" and "fixture reply" in row["text"] for row in lark()),
                               "Lark answers after Check now")
                        _until(lambda:_events(state,"queued"),"Planner and queued Worker resume after Check now")
                        (state / "release-queued-a").touch()
                        _until(lambda:_limits(api)["running"]==0,"all enabled turns released")
                        assert _query(url,"SELECT content FROM chat_messages WHERE content LIKE '%额度已用完%'") == []
                        records = _query(url,"SELECT input_text FROM llm_records WHERE agent_id='manager'")
                        assert all("额度已用完" not in row["input_text"] for row in records)
                        print("freeze and clear passed", flush=True)
                        _query(url,"INSERT INTO provider_limit_windows(provider_id,window_name,status,utilization,source,observed_at,resets_at) "
                            "VALUES($1,'seven_day','warning',.95,'claude_stream',now(),now()+interval '1 hour') "
                            "ON CONFLICT(provider_id,window_name) DO UPDATE SET status='warning',utilization=.95,observed_at=now(),"
                            "resets_at=now()+interval '1 hour'",PROVIDER,execute=True)
                        inbox("wake")
                        page.goto(api.base_url + "/#chat")
                        ready(page)
                        page.locator("textarea").wait_for()
                        thread = page.evaluate("localStorage.getItem('looper-chat-session-id')")
                        assert thread
                        inbox("completion",task_id=ordinary,thread=thread)
                        _until(lambda:_limits(api)["waiting"].get("background",0)>=1,"completion held at 0.95")
                        before = len(_events(state,"manager",kind="turn"))
                        page.locator("textarea").fill("请优先回答这条消息。")
                        page.locator("textarea").press("Enter")
                        _until(lambda:len(_events(state,"manager",kind="turn"))>before,"user preempts waiting completion")
                        row = _until(lambda:_query(url,"SELECT state,reason,delivery FROM manager_events WHERE task_id=$1 "
                            "AND state='fallback'",ordinary),"completion fallback after preemption")[0]
                        assert (row["state"],row["reason"],row["delivery"]) == ("fallback","turn_stopped","sent")
                        assert not _limits(api)["waiting"].get("background",0)
                        page.screenshot(path=str(args.output / "completion-preempted-390.png"),full_page=True)
                        (args.output / "lark.json").write_text(json.dumps(lark(),ensure_ascii=False,indent=2)+"\n")
                        (args.output / "results.json").write_text(json.dumps({"dashboard_wait_then_reply":True,
                            "quota_freeze_one_lark_progress":True,"planner_wait_then_clear":True,
                            "freeze_status_not_persisted":True,"manager_inputs_exclude_freeze_status":True,
                            "completion_utilization_preempt":True},ensure_ascii=False,indent=2)+"\n")
                        browser.close()
        except Exception:
            if (root / "serve.log").exists():
                (args.output / "serve-private.log").write_text((root / "serve.log").read_text())
            raise
        finally:
            runner._SERVE = original_serve
    print("turn admission serve/browser smoke passed")


if __name__ == "__main__":
    main()
