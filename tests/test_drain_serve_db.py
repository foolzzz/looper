"""Planned-restart drain on real serve, with controlled CLI peers and a fresh database."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import asyncpg
import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from tests.db_support import DSN
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_CLI = r'''
import json, os, re, sys, time
from pathlib import Path

state = Path(sys.argv[1])
args = sys.argv[2:]
if args[:1] in (["--version"], ["auth"]):
    print("2.0.0")
    sys.exit(0)
flag = next((f for f in ("--append-system-prompt-file", "--system-prompt-file") if f in args), None)
prompt = Path(args[args.index(flag) + 1]).read_text() if flag else ""
variant_match = re.search(r"drain_variant=(\w+)", prompt)
variant = variant_match.group(1) if variant_match else "manager"

def log(kind, **values):
    with (state / "events.jsonl").open("a") as output:
        output.write(json.dumps({"kind": kind, "variant": variant, **values}) + "\n")

def answer(text):
    for event in (
        {"type": "system", "subtype": "init", "session_id": "drain-fake-" + variant, "model": "fake"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": text,
         "session_id": "drain-fake-" + variant, "total_cost_usd": 0.001,
         "usage": {"input_tokens": 10, "output_tokens": 10}},
    ):
        print(json.dumps(event), flush=True)

step = re.search(r"^Your step: (\S+)$", prompt, re.MULTILINE)
if step:
    sys.stdin.readline()
    label = step.group(1)
    log("worker", step=label)
    if variant in ("restart", "deadline") and label == "s1":
        limit = time.monotonic() + 90
        while not (state / ("release-" + variant)).exists():
            if time.monotonic() >= limit:
                raise RuntimeError("test did not release its Worker")
            time.sleep(0.05)
    answer("Done.\n" + json.dumps({"status": "completed", "changed_files": {}, "deleted_files": [],
                                   "summary": label + " finished"}))
    sys.exit(0)

def spec(label):
    return {"id": label, "instruction": "finish " + label, "acceptance_criteria": [label + " works"],
            "depends_on": [], "review": False}

final = {"delivered": "the steps", "how_to_use": "run it", "verification": "checks passed", "open_issues": "none"}
manager = "You are the Manager Agent" in prompt
for line in sys.stdin:
    if not line.strip():
        continue
    content = json.loads(line)["message"]["content"]
    text = content if isinstance(content, str) else "".join(block.get("text", "") for block in content)
    log("manager" if manager else "planner", text=text)
    if manager:
        answer("Drain fixture answer: " + text)
        continue
    if text.startswith("This run's requirement"):
        text = text[text.index("[PLAN]"):]
    if text.startswith("[PLAN]") or "Your last reply broke the reply contract" in text:
        reply = {"steps": [spec("s1"), *([spec("s2")] if variant == "restart" else [])]}
    elif text.startswith("[DECISION]"):
        reply = {"decision": "accept", "summary": "finished",
                 **({"final": final} if "the task's last open step" in text else {})}
    else:
        reply = {"reply": "continuing", "action": "none"}
    answer("Done.\n```json\n" + json.dumps(reply) + "\n```")
'''


def _until(check, what: str, seconds: float = 45):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.1)


def _query(url: str, sql: str, *args, execute: bool = False):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            if execute:
                return await conn.execute(sql, *args)
            return [dict(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
    return asyncio.run(asyncio.wait_for(run(), 15))


def _events(state: Path, kind: str, variant: str | None = None):
    path = state / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["kind"] == kind and (variant is None or row["variant"] == variant)]


@contextmanager
def _fixture(root: Path):
    # Match the repository's disposable-server policy before creating any database.
    require_disposable_database(str(DSN), served=False)
    assert urlsplit(str(DSN)).port in (5432, 54329)
    state, bin_dir, runtime, home = (root / part for part in ("state", "bin", "runtime", "home"))
    for directory in (state, bin_dir, runtime, home):
        directory.mkdir()
    script = root / "fake_claude.py"
    script.write_text(_CLI)
    executable = bin_dir / "claude"
    executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
                          f"{shlex.quote(str(state))} \"$@\"\n")
    executable.chmod(0o755)
    with fresh_database(str(DSN)) as (url, _):
        yield url, state, {"database_url": url, "runtime_root": runtime, "bin_dir": bin_dir,
                           "home": home, "notes_root": home / "notes"}


def _project(api, root: Path, name: str):
    work = root / ("work-" + name)
    work.mkdir()
    project = api.post("/api/v1/project_admin/create", {"name": name, "work_dir": str(work)})["result"]["project"]
    api.post("/api/v1/project_admin/update", {"project_id": project["project_id"],
             "fields": {"task_workspaces": "in_place"}})
    return project["project_id"]


def _task(api, project: str, variant: str):
    task = api.post("/api/v1/tasks/create", {"project_id": project,
                    "requirement": "Finish the fixture drain_variant=" + variant})["result"]["task_id"]
    api.post("/api/v1/ops/task_control/start", {"task_id": task})
    return task


def _detail(api, task):
    return api.post("/api/v1/tasks/detail", {"task_id": task})["result"]


def _drain(api, operation="drain_status", body=None):
    data = json.dumps(body or {}).encode()
    request = Request(api.base_url + "/api/v1/system/" + operation, data=data, method="POST",
                      headers={"Authorization": "Bearer " + api.token, "Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        assert response.status == 200
        return json.load(response)["result"]


def _messages(api):
    return api.post("/api/v1/operator_chat/messages", {"session_id": "drain-chat"})["result"]["messages"]


def _send(api, message_id):
    return api.post("/api/v1/operator_chat/send", {"session_id": "drain-chat", "message_id": message_id,
                    "text": message_id})["result"]


def _completed(api, task):
    detail = _until(lambda: (detail := _detail(api, task))["status"] in
                    ("completed", "failed", "cancelled", "stopped") and detail, "the task to finish")
    assert detail["status"] == "completed", detail
    return detail


def test_drain_restart_replays_held_runs_and_manager_once_and_keeps_planners_moving(tmp_path):
    """Worker results reach a decision before DRAINED; TASK_START remains usable throughout."""
    with _fixture(tmp_path) as (url, state, server):
        with running_server(**server, log_path=tmp_path / "before.log") as api:
            api.post("/api/v1/agent_settings/update_role", {"role": "worker", "values": {"max_concurrency": 1}})
            project = _project(api, tmp_path, "restart")
            _send(api, "before-drain")
            _until(lambda: any(m["message_id"] == "before-drain:reply" for m in _messages(api)),
                   "the serving Manager's positive control reply")
            task = _task(api, project, "restart")
            _until(lambda: _events(state, "worker", "restart"), "the first Worker to start")
            _until(lambda: len([s for s in _detail(api, task)["steps"] if s["status"] == "dispatched"]) == 2,
                   "both steps to be dispatched")
            status = _drain(api, "drain", {"scope": "runs", "timeout_seconds": 60})
            assert status["phase"] == "draining" and status["scope"] == "runs"
            (run,) = status["waiting"]["runs"]
            assert run["task_id"] == task and run["worker_id"] and run["role"] == "worker"
            assert run["since"] and run["keepable"] is False
            assert api.post("/api/v1/system/status", {})["result"]["drain"]["phase"] == "draining"
            _send(api, "held-during-drain")

            # A second TASK_START and its planning turn must execute, even though its run cannot take a slot.
            fresh = _task(api, project, "fresh")
            _until(lambda: any(s["status"] == "dispatched" for s in _detail(api, fresh)["steps"]),
                   "TASK_START and the Planner to dispatch during drain")
            assert _events(state, "planner", "fresh")
            assert not _events(state, "worker", "fresh")
            assert len(_events(state, "worker", "restart")) == 1
            assert len(_events(state, "manager")) == 1
            assert not any(m["message_id"] == "held-during-drain:reply" for m in _messages(api))
            journal = server["runtime_root"] / ".local/agent-bus"
            entries = [json.loads(line) for line in (journal / "journal.jsonl").read_text().splitlines()]
            envelope = next(row for row in entries if row["payload"].get("text") == "held-during-drain")
            assert envelope["message_id"] not in (journal / "delivered.txt").read_text().splitlines()

            (state / "release-restart").touch()
            drained = _until(lambda: (status := _drain(api))["phase"] == "drained" and status,
                             "the Worker result and Planner decision to drain")
            assert all(not value for value in drained["waiting"].values())
            steps = {step["step_key"]: step["status"] for step in _detail(api, task)["steps"]}
            assert steps == {"s1": "verified", "s2": "dispatched"}
            assert any(row["text"].startswith("[DECISION]") for row in _events(state, "planner", "restart"))

        with running_server(**server, log_path=tmp_path / "after.log") as api:
            assert _drain(api)["phase"] == "serving"
            _completed(api, task)
            _completed(api, fresh)
            _until(lambda: any(m["message_id"] == "held-during-drain:reply" for m in _messages(api)),
                   "the replayed Manager reply")
            assert sum(m["message_id"] == "held-during-drain:reply" for m in _messages(api)) == 1
            reply = next(m for m in _messages(api) if m["message_id"] == "held-during-drain:reply")
            assert reply["content"].startswith("Drain fixture answer: ")
            assert "held-during-drain" in reply["content"]
            assert [row["step"] for row in _events(state, "worker", "restart")] == ["s1", "s2"]
            assert len(_events(state, "worker", "fresh")) == 1
            assert sum("held-during-drain" in row["text"] for row in _events(state, "manager")) == 1
            reply = next(m for m in _messages(api) if m["message_id"] == "held-during-drain:reply")
            assert reply["content"].startswith("Drain fixture answer: ")
            assert reply["content"].endswith("held-during-drain")
            _until(lambda: not _query(url, "SELECT id FROM llm_records WHERE status='running'"),
                   "all resumed records to finish")
            assert not _query(url, "SELECT id FROM llm_records WHERE status='interrupted'")


@pytest.mark.parametrize("scope", ["turns", "runs"])
def test_deadline_reports_unkeepable_worker_and_cancel_releases_held_run(tmp_path, scope):
    with _fixture(tmp_path) as (url, state, server):
        with running_server(**server, log_path=tmp_path / "serve.log") as api:
            project = _project(api, tmp_path, "deadline")
            task = _task(api, project, "deadline")
            _until(lambda: _events(state, "worker", "deadline"), "the long Worker to start")
            _drain(api, "drain", {"scope": scope, "timeout_seconds": 1})
            held = _task(api, project, "cancelled_drain")
            _until(lambda: any(s["status"] == "dispatched" for s in _detail(api, held)["steps"]),
                   "the held task to dispatch")
            status = _until(lambda: (status := _drain(api))["deadline_passed"] and status,
                            "the drain deadline to pass")
            assert status["phase"] == "draining"
            assert [run["task_id"] for run in status["waiting"]["runs"]] == [task]
            assert status["waiting"]["runs"][0]["keepable"] is False
            assert not _events(state, "worker", "cancelled_drain")
            _send(api, "cancel-releases-manager")
            assert not any(m["message_id"] == "cancel-releases-manager:reply" for m in _messages(api))
            assert _drain(api, "drain_cancel")["phase"] == "serving"
            _until(lambda: _events(state, "worker", "cancelled_drain"), "drain_cancel to release the held run", 10)
            assert not (state / "release-deadline").exists()
            _until(lambda: any(m["message_id"] == "cancel-releases-manager:reply" for m in _messages(api)),
                   "drain_cancel to release the held Manager message", 10)
            (state / "release-deadline").touch()
            _completed(api, task)
            _completed(api, held)


def test_drain_restart_keeps_web_gate_command_reply(tmp_path):
    """Code-owned replies need their web destination even when external chat persistence is disabled."""
    with _fixture(tmp_path) as (_, state, server):
        body = {"session_id": "drain-chat", "message_id": "gates-before-drain", "text": "/gates"}
        with running_server(**server, log_path=tmp_path / "before.log") as api:
            api.post("/api/v1/operator_chat/send", body)
            _until(lambda: any(m["message_id"] == "gates-before-drain:reply" for m in _messages(api)),
                   "the serving gate command's positive control reply")
            _drain(api, "drain", {"scope": "turns", "timeout_seconds": 60})
            body["message_id"] = "gates-during-drain"
            api.post("/api/v1/operator_chat/send", body)
            _until(lambda: _drain(api)["phase"] == "drained", "the service to drain")
            assert not any(m["message_id"] == "gates-during-drain:reply" for m in _messages(api))
            journal = server["runtime_root"] / ".local/agent-bus"
            entries = [json.loads(line) for line in (journal / "journal.jsonl").read_text().splitlines()]
            held = next(row for row in entries if
                        row["payload"].get("channel_context", {}).get("client_message_id") == "gates-during-drain")
            assert held["message_id"] not in (journal / "delivered.txt").read_text().splitlines()

        with running_server(**server, log_path=tmp_path / "after.log") as api:
            replies = _until(lambda: [m for m in _messages(api) if m["message_id"] == "gates-during-drain:reply"],
                             "the replayed gate command's saved reply")
            assert len(replies) == 1 and replies[0]["content"]
            assert not _events(state, "manager")  # /gates is answered by code, never the model.
            entries = [json.loads(line) for line in (journal / "journal.jsonl").read_text().splitlines()]
            reply = next(row for row in entries if row.get("reply_to") == held["message_id"])
            _until(lambda: reply["message_id"] in (journal / "delivered.txt").read_text().splitlines(),
                   "the saved reply to be acknowledged")


def test_schedule_due_while_drained_catches_up_after_restart_without_gate_failure(tmp_path):
    with _fixture(tmp_path) as (url, state, server):
        with running_server(**server, log_path=tmp_path / "before.log") as api:
            project = _project(api, tmp_path, "schedule")
            schedule = api.post("/api/v1/schedule_admin/create", {"project_id": project, "fields": {
                "name": "Drain catch-up", "cron": "* * * * *", "timezone": "UTC", "catch_up": "once",
                "requirement": "Finish scheduled work drain_variant=scheduled", "notify": "never",
                "gate_command": "printf 'synthetic gate input'", "run_timeout_seconds": 60}})["result"]["schedule"]
            sid = schedule["schedule_id"]
            control = api.post("/api/v1/schedule_admin/run", {"schedule_id": sid})["result"]["run"]
            _until(lambda: (rows := _query(url, "SELECT status FROM schedule_runs WHERE id=$1", control["run_id"]))
                   and rows[0]["status"] == "completed", "the positive-control gate and scheduled task to finish")
            _drain(api, "drain", {"scope": "runs", "timeout_seconds": 60})
            _until(lambda: _drain(api)["phase"] == "drained", "the service to drain")
            _query(url, "UPDATE schedules SET enabled=TRUE, next_fire_at=date_trunc('minute', now()) - "
                        "interval '3 minutes' WHERE id=$1", sid, execute=True)
            (due,) = _query(url, "SELECT next_fire_at FROM schedules WHERE id=$1", sid)
            # Cross a real 15-second scheduler tick: no claim, gate, or cursor advance may happen during drain.
            started = time.monotonic()
            _until(lambda: time.monotonic() - started >= 16, "one scheduler tick during drain", 20)
            assert _query(url, "SELECT next_fire_at FROM schedules WHERE id=$1", sid) == [due]
            assert len(_query(url, "SELECT id FROM schedule_runs WHERE schedule_id=$1", sid)) == 1

        with running_server(**server, log_path=tmp_path / "after.log") as api:
            rows = _until(lambda: (rows := _query(url, "SELECT trigger, status, task_id FROM schedule_runs "
                          "WHERE schedule_id=$1 AND trigger='catch_up'", sid))
                          and rows[0]["status"] == "completed" and rows,
                          "the missed schedule to catch up after restart")
            assert len(rows) == 1 and rows[0]["task_id"]
            assert not _query(url, "SELECT id FROM schedule_runs WHERE schedule_id=$1 AND status='gate_failed'", sid)
            assert _drain(api)["phase"] == "serving"
