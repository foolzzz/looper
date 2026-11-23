"""Account slots and quota recovery through an isolated serve and controlled CLI peers."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(
    not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL"
)
PROVIDER = "prov-claude-subscription"

_CLI = r"""
import json,re,sys,time
from datetime import datetime,timezone
from pathlib import Path
state=Path(sys.argv[1]); args=sys.argv[2:]
if args[:1] in (["--version"],["auth"]):
    print("2.0.0");sys.exit(0)
flag=next((f for f in ("--append-system-prompt-file","--system-prompt-file") if f in args),None)
prompt=Path(args[args.index(flag)+1]).read_text() if flag else ""
match=re.search(r"admission_variant=(\w+)",prompt)
variant=match.group(1) if match else "manager"
step=re.search(r"^Your step: (\S+)$",prompt,re.MULTILINE)
def log(kind,**values):
    with (state/"events.jsonl").open("a") as output:
        output.write(json.dumps(dict(kind=kind,variant=variant,**values))+"\n")
def answer(text):
    for event in (
        dict(type="system",subtype="init",session_id="admission-fixture",model="synthetic"),
        dict(type="assistant",message=dict(role="assistant",content=[dict(type="text",text=text)])),
        dict(type="result",subtype="success",is_error=False,result=text,session_id="admission-fixture",
             total_cost_usd=.001,usage=dict(input_tokens=10,output_tokens=10))):
        print(json.dumps(event),flush=True)
if step:
    sys.stdin.readline(); label=step.group(1); log("start",step=label)
    gate=state/("release-"+variant+"-"+label)
    deadline=time.monotonic()+150
    while not gate.exists():
        if time.monotonic()>deadline: raise RuntimeError("fixture Worker was not released")
        time.sleep(.03)
    log("end",step=label)
    if variant.startswith("quota"):
        reset=datetime.now(timezone.utc).timestamp()+2
        print(json.dumps(dict(type="rate_limit_event",rate_limit_info=dict(status="rejected",
            rateLimitType="seven_day",resetsAt=reset,utilization=1))),flush=True)
        print(json.dumps(dict(type="result",subtype="error_during_execution",is_error=True,
            api_error="usage_limit_reached",api_error_status=429,api_error_type="rate_limit_error",
            num_turns=1,result="synthetic quota exhausted")),flush=True)
        sys.exit(1)
    answer("Done.\n"+json.dumps(dict(status="completed",changed_files={},deleted_files=[],summary=label+" finished")))
    sys.exit(0)
final=dict(delivered="synthetic steps",how_to_use="read the results",verification="fixture checks passed",open_issues="none")
for line in sys.stdin:
    if not line.strip(): continue
    content=json.loads(line)["message"]["content"]
    text=content if isinstance(content,str) else "".join(block.get("text","") for block in content)
    if text.startswith("This run's requirement"): text=text[text.index("[PLAN]"):]
    if text.startswith("[PLAN]") or "Your last reply broke the reply contract" in text:
        labels=["a","b","c"] if variant=="serial" else ["a"]
        reply=dict(steps=[dict(id=label,instruction="finish "+label,acceptance_criteria=[label+" works"],
                              depends_on=[],review=False) for label in labels])
    elif text.startswith("[DECISION]"):
        reply=dict(decision="fail",reason="Synthetic quota failure") if variant.startswith("quota") else dict(
            decision="accept",reason="Synthetic step passed",summary="finished",**(dict(final=final) if "the task's last open step" in text else {}))
    else: reply=dict(reply="fixture reply",action="none")
    answer("Done.\n```json\n"+json.dumps(reply)+"\n```")
"""

_PATCH = """
import os,json
from datetime import datetime,timedelta,timezone
from pathlib import Path
from looper.agents import admission,gate_service
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.gate_service import GateChat
from looper.agents.task_notice import NotifyChannel,NotifyTarget,TargetKind
state=Path(os.environ["ADMISSION_FIXTURE_STATE"])
def clock():
    return datetime.now(timezone.utc)+timedelta(seconds=float((state/"offset").read_text()))
admission._controller.now=clock
admission.RECHECK_SECONDS=.05
async def target():
    return GateChat(NotifyChannel.TELEGRAM,NotifyTarget(TargetKind.CHAT,"synthetic-alert-chat"),"channel-telegram")
async def notify(self,channel_id,chat,text,**kwargs):
    assert channel_id=="channel-telegram" and chat=="synthetic-alert-chat"
    with (state/"alerts.jsonl").open("a") as output:
        output.write(json.dumps({"text":text})+"\\n")
    return True,None
gate_service.dashboard_target=target
ChannelRegistry.notify=notify
"""


def _query(url, sql, *args, execute=False):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            return (
                await conn.execute(sql, *args)
                if execute
                else [dict(row) for row in await conn.fetch(sql, *args)]
            )
        finally:
            await conn.close()

    return asyncio.run(asyncio.wait_for(run(), 15))


def _until(check, what, seconds=60):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _events(state, variant, kind="start"):
    path = state / "events.jsonl"
    rows = (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )
    return [row for row in rows if row["kind"] == kind and row["variant"] == variant]


def _limits(api):
    return api.post("/api/v1/providers/limits", {"provider_id": PROVIDER})["result"][
        "providers"
    ][0]["admission"]


def test_serve_serializes_freezes_probes_then_operator_clear(tmp_path, monkeypatch):
    state, bin_dir, runtime, home = (
        tmp_path / name for name in ("state", "bin", "runtime", "home")
    )
    for directory in (state, bin_dir, runtime, home):
        directory.mkdir()
    (state / "offset").write_text("0")
    script = tmp_path / "fixture_claude.py"
    script.write_text(_CLI)
    cli = bin_dir / "claude"
    cli.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(state))} "$@"\n'
    )
    cli.chmod(0o755)
    monkeypatch.setattr(runner, "_SERVE", _PATCH + runner._SERVE)
    with fresh_database_without_recording(str(DSN)) as (url, _):
        _query(
            url,
            "INSERT INTO system_settings(key,value) VALUES "
            "('engine.failure_typing','record'),('engine.limit_signals.enabled','true'),('engine.admission.enabled','true')",
            execute=True,
        )
        _query(
            url,
            "UPDATE llm_providers SET max_concurrency=1 WHERE id=$1",
            PROVIDER,
            execute=True,
        )
        with runner.running_server(
            database_url=url,
            runtime_root=runtime,
            bin_dir=bin_dir,
            home=home,
            notes_root=home / "notes",
            log_path=tmp_path / "serve.log",
            env={"ADMISSION_FIXTURE_STATE": str(state)},
        ) as api:
            work = tmp_path / "work"
            work.mkdir()
            project = api.post(
                "/api/v1/project_admin/create",
                {"name": "Admission fixture", "work_dir": str(work)},
            )["result"]["project"]["project_id"]
            api.post(
                "/api/v1/project_admin/update",
                {"project_id": project, "fields": {"task_workspaces": "in_place"}},
            )

            def task(variant):
                task_id = api.post(
                    "/api/v1/tasks/create",
                    {
                        "project_id": project,
                        "requirement": "Finish admission_variant=" + variant,
                    },
                )["result"]["task_id"]
                api.post("/api/v1/ops/task_control/start", {"task_id": task_id})
                return task_id

            task("quota")
            _until(lambda: _events(state, "quota"), "quota fixture Worker")
            serial = task("serial")
            _until(
                lambda: sum(_limits(api)["waiting"].values()) == 3,
                "three account waiters",
            )
            assert not _events(state, "serial")
            (state / "release-quota-a").touch()
            _until(lambda: _limits(api)["state"] == "frozen", "quota freeze")
            _until(lambda: (state / "alerts.jsonl").exists(), "stand-in freeze alert")
            _until(
                lambda: _query(
                    url,
                    "SELECT summary FROM activity WHERE task_id=$1 AND summary LIKE '%out of quota%'",
                    serial,
                ),
                "frozen task activity",
            )
            assert not _events(state, "serial")
            assert len((state / "alerts.jsonl").read_text().splitlines()) == 1
            (state / "offset").write_text("65")
            probe = _until(lambda: _events(state, "serial"), "one oldest-waiter probe")
            assert len(probe) == 1 and _limits(api)["state"] == "probing"
            assert (
                _limits(api)["running"] == 1
                and sum(_limits(api)["waiting"].values()) == 2
            )
            (state / ("release-serial-" + probe[0]["step"])).touch()
            _until(
                lambda: len(_events(state, "serial")) == 2,
                "successful probe releases the next Worker",
            )
            assert _limits(api)["state"] == "open" and _limits(api)["running"] == 1
            second = _events(state, "serial")[1]["step"]
            (state / ("release-serial-" + second)).touch()
            _until(
                lambda: len(_events(state, "serial")) == 3, "third serialized Worker"
            )
            third = _events(state, "serial")[2]["step"]
            (state / ("release-serial-" + third)).touch()
            _until(lambda: _limits(api)["running"] == 0, "account releases all slots")
            task("quota_again")
            _until(lambda: _events(state, "quota_again"), "second quota fixture")
            (state / "release-quota_again-a").touch()
            _until(lambda: _limits(api)["state"] == "frozen", "second freeze")
            cleared = api.post(
                "/api/v1/providers/limits_clear", {"provider_id": PROVIDER}
            )["result"]
            assert cleared["state"] == "open" and _limits(api)["state"] == "open"
            events = _query(
                url,
                "SELECT from_state,to_state,actor FROM provider_admission_events ORDER BY id",
            )
            assert [(row["from_state"], row["to_state"]) for row in events[:3]] == [
                ("open", "frozen"),
                ("frozen", "probing"),
                ("probing", "open"),
            ]
            assert events[-1]["actor"] == "operator"
            assert len((state / "alerts.jsonl").read_text().splitlines()) == 2
