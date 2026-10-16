"""A task's Planner uses its own tools over MCP on a real ``looper serve``, with a stand-in ``claude``.

One disposable ``looper serve`` on a new database runs one task per variant,
each in a project of its own. In its [PLAN] turn the stand-in Planner starts
the ``looper`` MCP server from the ``--mcp-config`` it was launched with
(``looper mcp-server`` with its task's token) and calls its tools, then plans:

- ``profile``: the project allows ``claude-code/sonnet``; the Planner chooses
  it for the Workers, and the step its reply dispatches runs on it;
- ``empty``: the project's list is empty; the Planner's choice is refused and
  the Worker runs the default profile;
- ``concurrency``: the Planner lowers its task's Worker limit to 1 and plans
  two independent steps, which then run one after the other.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database (the
run creates and drops a database beside it).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression.runner import fresh_database, running_server

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

VARIANTS = ("profile", "empty", "concurrency")

_FAKE_CLAUDE = r'''
import json, os, re, subprocess, sys, time
STATE = sys.argv[1]
args = sys.argv[2:]
if args[:1] in (["--version"], ["auth"]):  # the server's startup self-check, not an agent run
    sys.exit(0)
prompt = ""
if "--append-system-prompt-file" in args:
    prompt = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
variant = re.search(r"variant=(\w+)", prompt).group(1)

def log(name, value):
    with open(os.path.join(STATE, name), "a") as f:
        f.write(json.dumps({"variant": variant, **value}) + "\n")

def answer(session, text):
    for event in ({"type": "system", "subtype": "init", "session_id": session, "model": "fake"},
                  {"type": "assistant", "session_id": session,
                   "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
                  {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
                   "total_cost_usd": 0.001, "usage": {"input_tokens": 10, "output_tokens": 10}}):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

def spec(step_id):
    return {"id": step_id, "instruction": f"do {step_id}", "acceptance_criteria": [f"{step_id} works"],
            "depends_on": [], "review": False}

step = re.search(r"^Your step: (\S+)$", prompt, re.MULTILINE)
if step:
    sys.stdin.readline()
    started = time.time()
    time.sleep(1.0 if variant == "concurrency" else 0)
    log("runs.jsonl", {"step": step.group(1), "model": args[args.index("--model") + 1], "started": started,
                       "ended": time.time()})
    report = {"status": "completed", "changed_files": {}, "deleted_files": [], "summary": f"{step.group(1)} done"}
    answer("fake-worker", "Done.\n" + json.dumps(report))
    sys.exit(0)

config = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["looper"]
log("planner.jsonl", {"token": config["env"]["LOOPER_AGENT_TOKEN"], "guide": "## Your tools (Looper's MCP server" in prompt})
server = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          env={**os.environ, **config["env"]}, text=True)
ids = iter(range(1, 10000))

def rpc(method, params):
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params}) + "\n")
    server.stdin.flush()
    return json.loads(server.stdout.readline())

def call(name, arguments):
    result = json.loads(rpc("tools/call", {"name": name, "arguments": arguments})["result"]["content"][0]["text"])
    log("calls.jsonl", {"tool": name, "answer": result})
    return result

rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "stand-in"}})
log("planner.jsonl", {"tools": [t["name"] for t in rpc("tools/list", {})["result"]["tools"]]})
for line in sys.stdin:
    if not line.strip():
        continue
    content = json.loads(line)["message"]["content"]
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
    if text.startswith("[PLAN]"):
        call("looper_task_profiles", {})
        if variant == "concurrency":
            call("looper_concurrency", {"scope": "task", "limits": {"worker": 1}, "reason": "one shared database"})
            reply = {"steps": [spec("a"), spec("b")]}
        else:
            call("looper_task_profile_set", {"role": "worker", "profile_id": "claude-code/sonnet",
                                             "reason": "the step needs a stronger model"})
            reply = {"steps": [spec("a")]}
    elif text.startswith("[DECISION]"):
        final = "the task's last open one" in text
        reply = {"decision": "accept", "reason": "meets the criteria", "summary": "done"}
        if final:
            reply["final"] = {"delivered": "the steps", "how_to_use": "run it", "verification": "each check passed",
                              "open_issues": "none"}
    else:
        reply = {"reviews": []}
    answer(f"fake-planner-{variant}", "Done.\n\n```json\n" + json.dumps(reply) + "\n```")
'''


def _install(root: Path) -> tuple[Path, Path]:
    state = root / "state"
    state.mkdir()
    (root / "fake_claude.py").write_text(_FAKE_CLAUDE, encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(root / 'fake_claude.py'))} "
                      f"{shlex.quote(str(state))} \"$@\"\n")
    claude.chmod(0o755)
    return bin_dir, state


def _log(state: Path, name: str, variant: str) -> list[dict[str, Any]]:
    path = state / name
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["variant"] == variant]


def _tools(base_url: str, token: str) -> int:
    request = urllib.request.Request(f"{base_url}/api/agent/v1/tools", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Every variant's task once it completed, with what the stand-in logged and the server's log."""
    root = tmp_path_factory.mktemp("planner-tools").resolve()
    bin_dir, state = _install(root)
    runtime, home = root / "runtime", root / "home"
    for path in (runtime, home):
        path.mkdir()
    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "serve.log") as api:
            sonnet = api.post("/api/agent-profiles", {"name": "claude-code/sonnet", "runtime_id": "rt-claude-code",
                                                      "provider_id": "prov-claude-subscription", "model": "sonnet"})
            ids: dict[str, str] = {}
            for variant in VARIANTS:
                work = root / f"work-{variant}"
                work.mkdir()
                project = api.post("/api/projects", {"name": f"calc-{variant}", "work_dir": str(work)})
                if variant == "profile":
                    api.put(f"/api/projects/{project['project_id']}/allowed-profiles",
                            {"profile_ids": [sonnet["profile"]["profile_id"]]})
                ids[variant] = api.post("/api/tasks/create", {"requirement": f"build calc variant={variant}",
                                                              "project_id": project["project_id"]})["task_id"]
                api.post("/api/tasks/start", {"task_id": ids[variant]})
            deadline = time.monotonic() + 90
            while True:
                details = {variant: api.get(f"/api/tasks/{task_id}") for variant, task_id in ids.items()}
                if all(d["status"] in ("completed", "failed", "stopped", "cancelled") for d in details.values()):
                    break
                assert time.monotonic() < deadline, {v: d["status"] for v, d in details.items()}
                time.sleep(0.2)
            tokens = {variant: _log(state, "planner.jsonl", variant)[0]["token"] for variant in VARIANTS}
            after_end = {variant: _tools(api.base_url, token) for variant, token in tokens.items()}
    return {"details": details, "state": state, "after_end": after_end, "tokens": tokens,
            "log": (root / "serve.log").read_text()}


def _calls(ran: dict[str, Any], variant: str) -> dict[str, dict[str, Any]]:
    return {row["tool"]: row["answer"] for row in _log(ran["state"], "calls.jsonl", variant)}


def test_the_planner_gets_its_three_tools_and_how_to_use_them(ran: dict[str, Any]) -> None:
    for variant in VARIANTS:
        assert ran["details"][variant]["status"] == "completed", (variant, ran["log"][-3000:])
        started, listed = _log(ran["state"], "planner.jsonl", variant)
        assert started["guide"] is True and started["token"].startswith("planner.")
        assert listed["tools"] == ["looper_task_profiles", "looper_task_profile_set", "looper_concurrency"]
    # One token per task; none works once its task has ended, and none is in the server's log.
    assert len(set(ran["tokens"].values())) == len(VARIANTS)
    assert ran["after_end"] == {variant: 401 for variant in VARIANTS}
    assert not any(token in ran["log"] for token in ran["tokens"].values())


def test_a_profile_the_planner_chooses_in_its_plan_turn_runs_the_step_it_dispatches(ran: dict[str, Any]) -> None:
    calls = _calls(ran, "profile")
    assert [p["name"] for p in calls["looper_task_profiles"]["result"]["allowed"]] == ["claude-code/sonnet"]
    assert calls["looper_task_profile_set"]["result"]["next_run"] == {"name": "claude-code/sonnet",
                                                                      "source": "planner_task"}
    assert [r["model"] for r in _log(ran["state"], "runs.jsonl", "profile")] == ["sonnet"]
    journal = [(e["kind"], e["summary"]) for e in ran["details"]["profile"]["journal"] if e["kind"] == "profile_choice"]
    assert journal == [("profile_choice", "Workers of the rest of the task: claude-code/sonnet. Reason: the step "
                                          "needs a stronger model")]
    # With the project's list empty, the choice is refused and the Worker runs what the settings give.
    refused = _calls(ran, "empty")["looper_task_profile_set"]
    assert refused["status"] == "error" and refused["error"]["code"] == "forbidden"
    assert [r["model"] for r in _log(ran["state"], "runs.jsonl", "empty")] == ["opus"]


def test_a_task_limit_the_planner_lowers_in_its_plan_turn_holds_the_steps_it_dispatches(ran: dict[str, Any]) -> None:
    answer = _calls(ran, "concurrency")["looper_concurrency"]
    assert answer["status"] == "ok" and answer["result"]["changes"][0]["after"] == {"value": 1, "source": "task"}
    runs = sorted(_log(ran["state"], "runs.jsonl", "concurrency"), key=lambda r: r["started"])
    assert [r["step"] for r in runs] in (["a", "b"], ["b", "a"])
    assert runs[1]["started"] >= runs[0]["ended"]  # one after the other
    detail = ran["details"]["concurrency"]
    assert detail["agents"]["roles"]["worker"]["concurrency"]["task"] == 1
    assert [e["summary"] for e in detail["journal"] if e["kind"] == "concurrency_change"] == [
        "Workers at once in this task: 16 → 1. Reason: one shared database"]
