"""Explorer lending and quota holds through disposable serve and real run-token HTTP calls."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

import asyncpg
import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
PROVIDER = "prov-claude-subscription"

_CLI = r'''
import json,os,sys,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path
state=Path(sys.argv[1]); args=sys.argv[2:]
if args[:1]==["--version"]:
    print("2.1.329 (stand-in)");sys.exit(0)
if args[:1]==["auth"]:
    print('{"loggedIn":true}');sys.exit(0)
flag=next((f for f in ("--append-system-prompt-file","--system-prompt-file") if f in args),None)
prompt=Path(args[args.index(flag)+1]).read_text() if flag else ""
def save(name,value):
    temporary=state/(name+"."+str(os.getpid())+".tmp")
    temporary.write_text(json.dumps(value));temporary.replace(state/name)
def gate(name):
    until=time.monotonic()+60
    while not (state/name).exists():
        if time.monotonic()>until: raise RuntimeError("stand-in gate timed out: "+name)
        time.sleep(.02)
def answer(text):
    for event in (
        dict(type="system",subtype="init",session_id="synthetic",model="synthetic"),
        dict(type="assistant",message=dict(role="assistant",content=[dict(type="text",text=text)])),
        dict(type="result",subtype="success",is_error=False,result=text,session_id="synthetic",
             total_cost_usd=.02,usage=dict(input_tokens=10,output_tokens=10))):
        print(json.dumps(event),flush=True)
first=sys.stdin.readline()
if not first: sys.exit(0)
content=json.loads(first)["message"]["content"]
if isinstance(content,list): content="".join(item.get("text","") for item in content)
save("peer-phase",dict(worker="Your step:" in prompt,explorer=isinstance(content,str) and content.startswith("Spawned by")))
if isinstance(content,str) and content.startswith("Spawned by"):
    brief=json.loads(content.split("\n",1)[1])
    if brief["goal"]=="quota-child":
        save("explorer-started",True);gate("release-explorer")
        reset=datetime.now(timezone.utc).timestamp()+120
        print(json.dumps(dict(type="rate_limit_event",rate_limit_info=dict(status="rejected",
            rateLimitType="seven_day",resetsAt=reset,utilization=1))),flush=True)
        print(json.dumps(dict(type="result",subtype="error_during_execution",is_error=True,
            api_error="usage_limit_reached",api_error_status=429,api_error_type="rate_limit_error",
            num_turns=1,result="synthetic quota exhausted",total_cost_usd=.02)),flush=True)
        sys.exit(1)
    save("second-explorer-started",True)
    answer(json.dumps(dict(summary="Read local source",findings=[dict(claim="Found evidence",
        evidence=dict(path="source.py"))],open_questions=[],confidence="high")))
    sys.exit(0)
if "Your step:" in prompt:
    print(json.dumps(dict(type="system",subtype="init",session_id="synthetic",model="synthetic")),flush=True)
    config=json.loads(Path(args[args.index("--mcp-config")+1]).read_text())
    env=next(server["env"] for server in config["mcpServers"].values()
             if "LOOPER_AGENT_TOKEN" in server.get("env",{}))
    def call(name,body):
        print(json.dumps(dict(type="assistant",message=dict(role="assistant",content=[
            dict(type="tool_use",id="call-"+str(time.monotonic_ns()),name=name,input=body)]))),flush=True)
        request=urllib.request.Request(env["LOOPER_AGENT_URL"]+"/api/agent/v1/ops/"+name,
            data=json.dumps(body).encode(),headers={"Content-Type":"application/json",
            "Authorization":"Bearer "+env["LOOPER_AGENT_TOKEN"]})
        try:
            with urllib.request.urlopen(request,timeout=60) as response: value=json.load(response)
        except urllib.error.HTTPError as exc:
            save("peer-error",dict(code=exc.code,body=exc.read().decode()))
            raise
        if value["status"]!="ok":
            save("peer-error",value)
            raise RuntimeError(str(value))
        return value["result"]
    ids=[]
    for label in ("quota-child","second-child"):
        value=call("looper_agent_spawn",dict(kind="explorer",key=label,brief=dict(
            goal=label,deliverable="Evidence",scope=["."])))
        ids.append(value["spawn"]["id"])
    save("spawn-ids",ids)
    save("first-wait",call("looper_agent_wait",dict(spawn_ids=[ids[0]],wait_seconds=30)))
    save("held-wait",call("looper_agent_wait",dict(spawn_ids=[ids[1]],wait_seconds=1)))
    gate("resume-worker")
    save("final-wait",call("looper_agent_wait",dict(spawn_ids=[ids[1]],wait_seconds=5)))
    answer("Done.\n"+json.dumps(dict(status="completed",changed_files={},deleted_files=[],summary="Explored")))
    sys.exit(0)
while first:
    # The Planner is a resident peer, so read and answer each turn separately.
    turn=json.loads(first)["message"]["content"]
    if isinstance(turn,list): turn="".join(item.get("text","") for item in turn)
    if "[PLAN]" in turn or "Your last reply broke the reply contract" in turn:
        reply=dict(steps=[dict(id="read",instruction="Read local source",acceptance_criteria=["Evidence read"],
            depends_on=[],review=False)])
    elif turn.startswith("[DECISION]"):
        reply=dict(decision="accept",reason="Stand-in completed",summary="Read source",
            final=dict(delivered="Evidence",how_to_use="Read source",verification="Stand-in checked",open_issues="none"))
    else: reply=dict(reply="stand-in reply",action="none")
    answer("Done.\n```json\n"+json.dumps(reply)+"\n```")
    first=sys.stdin.readline()
    if not first: break
'''


def _query(url, sql, *args, execute=False):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            return await conn.execute(sql, *args) if execute else [dict(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
    return asyncio.run(asyncio.wait_for(run(), 15))


def _until(check, what, seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        result = check()
        if result:
            return result
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(.04)


def _screenshots(api, task_id, phase, work_dir):
    directory = os.environ.get("LOOPER_ADMISSION_SCREENSHOT_DIR")
    if not directory:
        return
    from playwright.sync_api import sync_playwright
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            for width in (1280, 390):
                page = browser.new_page(viewport={"width": width, "height": 1000},
                    extra_http_headers={"Authorization": f"Bearer {api.token}"})
                page.goto(f"{api.base_url}/#tasks/{task_id}?tab=run-tree", timeout=15000)
                page.get_by_text("quota-child", exact=False).first.wait_for(timeout=15000)
                _public_paths(page, work_dir)
                page.screenshot(path=str(output / f"explorer-admission-{phase}-{width}.png"), full_page=True)
                page.goto(f"{api.base_url}/#tasks/{task_id}?tab=sessions", timeout=15000)
                activity = page.get_by_text(re.compile(r"^Explorer \d+:")).first
                activity.wait_for(timeout=15000)
                activity.scroll_into_view_if_needed()
                _public_paths(page, work_dir)
                page.screenshot(path=str(output / f"explorer-activity-{phase}-{width}.png"), full_page=True)
                page.close()
        finally:
            browser.close()


def _public_paths(page, work_dir):
    # Redact disposable source and runtime paths in exported screenshots only.
    page.evaluate("""replacements => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode;
            for (const [path, label] of replacements) {
                if (node.nodeValue.includes(path)) node.nodeValue = node.nodeValue.split(path).join(label);
            }
        }
    }""", [[str(work_dir), "/workspace"], [str(work_dir.parent), "/validation"]])


def test_serve_worker_lends_one_slot_then_reads_quota_hold(tmp_path, monkeypatch):
    state, bin_dir, runtime, home, source = (tmp_path / name for name in ("state", "bin", "runtime", "home", "source"))
    for directory in (state, bin_dir, runtime, home, source):
        directory.mkdir()
    (source / "source.py").write_text("synthetic evidence")
    script = tmp_path / "standin.py"
    script.write_text(_CLI)
    for name in ("claude", "codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "lark-cli"):
        executable = bin_dir / name
        executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} '
                              f'{shlex.quote(str(state))} "$@"\n')
        executable.chmod(0o755)
    monkeypatch.setattr(runner, "_SERVE", "from looper.agents import admission\nadmission.RECHECK_SECONDS=.05\n"
        "import sys\nfrom looper.cli import main\n"
        'sys.exit(main(["serve","--host","127.0.0.1","--port",sys.argv[1],"--runtime-root",sys.argv[2]]))\n')
    with fresh_database_without_recording(str(DSN)) as (url, _):
        _query(url, "INSERT INTO system_settings(key,value) VALUES "
               "('engine.failure_typing','record'),('engine.limit_signals.enabled','true'),"
               "('engine.admission.enabled','true')", execute=True)
        _query(url, "UPDATE llm_providers SET name='Stand-in account',max_concurrency=1 WHERE id=$1", PROVIDER, execute=True)
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                notes_root=home / "notes", log_path=tmp_path / "serve.log", env={"LOOPER_TELEGRAM_BOT_TOKEN": ""}) as api:
            project = api.post("/api/v1/project_admin/create", {"name": "Explorer admission", "work_dir": str(source)})[
                "result"]["project"]["project_id"]
            api.post("/api/v1/project_admin/update", {"project_id": project,
                "fields": {"task_workspaces": "in_place", "subagents": "on", "agent_room": "off"}})
            task_id = api.post("/api/v1/tasks/create", {"project_id": project,
                "requirement": "Read local source with two explorers"})["result"]["task_id"]
            api.post("/api/v1/ops/task_control/start", {"task_id": task_id})
            try:
                _until(lambda: (state / "explorer-started").exists() or (state / "peer-error").exists(),
                       "lent explorer starts")
                assert (state / "explorer-started").exists(), "stand-in tool call failed"
            except AssertionError:
                diagnostics = {"task": _query(url, "SELECT status,error FROM tasks WHERE id=$1", task_id),
                    "runs": _query(url, "SELECT agent_id,status,error FROM llm_records WHERE task_id=$1", task_id),
                    "spawns": _query(url, "SELECT id,state,detail FROM agent_spawns WHERE task_id=$1", task_id),
                    "peer": (state / "peer-phase").read_text() if (state / "peer-phase").exists() else "not launched",
                    "http_error": (state / "peer-error").read_text() if (state / "peer-error").exists() else None}
                raise AssertionError(f"lent explorer did not start: {diagnostics}\n"
                    + (tmp_path / "serve.log").read_text(errors="replace")[-6000:]) from None
            ids = json.loads((state / "spawn-ids").read_text())
            rows = _query(url, "SELECT id,state FROM agent_spawns WHERE task_id=$1 ORDER BY id", task_id)
            assert [(row["id"], row["state"]) for row in rows] == [(ids[0], "asking"), (ids[1], "queued")]
            limits = api.post("/api/v1/providers/limits", {"provider_id": PROVIDER})["result"]["providers"][0]["admission"]
            assert limits["running"] == 1 and sum(limits["waiting"].values()) == 1
            activity = _query(url, "SELECT kind,summary FROM activity WHERE task_id=$1 AND kind='subagent_wait'", task_id)
            assert activity and all("Explorer" in row["summary"] for row in activity)
            tree = api.post("/api/v1/runs/tree", {"task_id": task_id})
            assert tree["status"] == "ok"
            explorers = [node for node in tree["result"]["nodes"] if node["kind"] == "explorer"]
            assert len(explorers) == 1 and explorers[0]["state"] == "asking"
            assert explorers[0]["reason"] == "quota-child" and explorers[0]["parent_record_id"] is not None
            _screenshots(api, task_id, "lent", source)
            (state / "release-explorer").touch()
            _until(lambda: (state / "held-wait").exists(), "Worker observes quota hold")
            held = json.loads((state / "held-wait").read_text())["spawns"][0]
            assert held["id"] == ids[1] and held["state"] == "queued" and held["held"] == "quota"
            assert held["resets_at"] is not None
            assert not (state / "second-explorer-started").exists()
            limits = api.post("/api/v1/providers/limits", {"provider_id": PROVIDER})["result"]["providers"][0]["admission"]
            assert limits["state"] == "frozen" and limits["running"] == 1
            _screenshots(api, task_id, "frozen", source)
            api.post("/api/v1/providers/limits_clear", {"provider_id": PROVIDER})
            (state / "resume-worker").touch()
            _until(lambda: (state / "final-wait").exists(), "second explorer finishes after clear")
            assert json.loads((state / "final-wait").read_text())["spawns"][0]["state"] == "answered"
            _until(lambda: _query(url, "SELECT id FROM tasks WHERE id=$1 AND status='completed'", task_id), "task completion")
