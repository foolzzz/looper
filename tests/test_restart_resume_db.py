"""A task carries on after ``looper serve`` is stopped with SIGINT and started again, from the database alone.

A real server (the M6 eval's disposable ``looper serve`` on a new database)
runs a stand-in ``claude``: the Planner plans four steps, accepts ``a``,
retries ``b`` with feedback and sends ``c`` to review. While ``b``'s retry and
``c``'s Reviewer run, the server stops; the next server resumes the task:
no new plan, the Planner's session resumed with its cost baseline, ``b``'s
retry run again with its feedback, ``c`` reviewed again, then ``d``. No plan
or session file is written. A task that failed at its spend cap continues the
same way after a restart, from its plan in the database. A ready task (created,
never started) stays ready through a restart until it is started. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database (each run creates and
drops a database beside it).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression.runner import Api, EvalError, fresh_database, running_server

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PLANNER_TURN_COST = 0.001

# The stand-in: the role is in the system prompt, as in the eval's fake. Workers and Reviewers that must still
# be running when the server stops wait for the ``release`` file the test writes after it has stopped.
_FAKE_CLAUDE = r'''
import json, os, re, sys, time
STATE = sys.argv[1]
args = sys.argv[2:]
if args[:1] in (["--version"], ["auth"]):  # the server's startup self-check, not an agent run
    sys.exit(0)
prompt = ""
if "--append-system-prompt-file" in args:
    prompt = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()

def log(name, value):
    with open(os.path.join(STATE, name), "a") as f:
        f.write(json.dumps(value) + "\n")

def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()

def answer(session, text, cost):
    emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake"})
    emit({"type": "assistant", "session_id": session, "message": {"role": "assistant",
          "content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
          "total_cost_usd": round(cost, 6), "usage": {"input_tokens": 10, "output_tokens": 10}})

def hold():
    while not os.path.exists(os.path.join(STATE, "release")):
        time.sleep(0.1)

def fenced(value):
    return "Done.\n\n```json\n" + json.dumps(value) + "\n```"

step = re.search(r"^(?:Your step|The step under review): (\S+)$", prompt, re.MULTILINE)
if re.search(r"^The step under review:", prompt, re.MULTILINE):
    sys.stdin.readline()
    log("runs.jsonl", {"role": "reviewer", "step": step.group(1)})
    hold()
    answer("fake-reviewer", json.dumps({"passed": True, "findings": [], "summary": "Meets the criteria."}), 0.001)
elif step:
    sys.stdin.readline()
    label = step.group(1)
    retried = re.search(r"being retried \(attempt (\d+)\)", prompt)
    attempt = int(retried.group(1)) if retried else 1
    log("runs.jsonl", {"role": "worker", "step": label, "attempt": attempt, "feedback": "fix the parser" in prompt})
    status = "completed"
    if label == "b" and attempt == 1:
        status = "failed"
    elif label == "b":
        hold()
    report = {"status": status, "changed_files": {}, "deleted_files": [], "summary": f"{label} attempt {attempt}"}
    answer("fake-worker", "Done.\n" + json.dumps(report), 0.002)
else:
    log("planner_argv.jsonl", args)
    cost_file = os.path.join(STATE, "planner_cost")
    # Like Claude, a resumed session reports its running total, carried on from before.
    cost = float(open(cost_file).read()) if "--resume" in args and os.path.exists(cost_file) else 0.0
    for line in sys.stdin:
        if not line.strip():
            continue
        content = json.loads(line)["message"]["content"]
        text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
        log("planner_turns.jsonl", text.splitlines()[0])
        uuid = json.loads(line).get("uuid")
        if text.startswith("[PLAN]") and "capped" in prompt:
            reply = {"steps": [
                {"id": x, "instruction": f"do {x}", "acceptance_criteria": [f"{x} works"], "depends_on": [],
                 "review": False} for x in ("x", "y")]}
        elif text.startswith("[PLAN]"):
            reply = {"steps": [
                {"id": "a", "instruction": "do a", "acceptance_criteria": ["a works"], "depends_on": [], "review": False},
                {"id": "b", "instruction": "do b", "acceptance_criteria": ["b works"], "depends_on": [], "review": False},
                {"id": "c", "instruction": "do c", "acceptance_criteria": ["c works"], "depends_on": [], "review": True},
                {"id": "d", "instruction": "do d", "acceptance_criteria": ["d works"], "depends_on": ["a", "b", "c"],
                 "review": False}]}
        elif text.startswith("[DECISION] Step b") and "- status: failed" in text:
            reply = {"decision": "retry", "reason": "b failed", "feedback": "fix the parser"}
        elif "needs a passing review" in text:
            reply = {"decision": "review", "reason": "c needs a look"}
        elif text.startswith("[DECISION]"):
            reply = {"decision": "accept", "reason": "meets the criteria", "summary": "all four steps done",
                     "final": {"delivered": "all four steps done", "how_to_use": "run d",
                               "verification": "each step's check passed", "open_issues": "none"}}
        else:
            reply = {"reviews": []}
        cost += 0.001
        open(cost_file, "w").write(str(cost))
        answer("fake-planner-session", fenced(reply), cost)
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


def _log(state: Path, name: str) -> list[Any]:
    path = state / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _wait(api: Api, task_id: str, done: Any, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        detail = api.get(f"/api/tasks/{task_id}")
        if done(detail):
            return detail
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out; the task is {detail.get('status')}: {detail.get('steps')}")
        time.sleep(0.2)


def _steps(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["step_key"]: s for s in detail.get("steps") or []}


def test_a_task_carries_on_after_the_server_restarts(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    bin_dir, state = _install(root)
    runtime, work, home = root / "runtime", root / "work", root / "home"
    for path in (runtime, work, home):
        path.mkdir()
    cut_off = {"a": "verified", "b": "dispatched", "c": "completed", "d": "pending"}

    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "first.log") as api:
            created = api.post("/api/tasks/create", {"requirement": "build calc",
                                                      "project": {"name": "calc", "work_dir": str(work)}})
            task_id = created["task_id"]
            api.post("/api/tasks/start", {"task_id": task_id})
            before = _wait(api, task_id, lambda d: {k: s["status"] for k, s in _steps(d).items()} == cut_off
                           and len(_log(state, "runs.jsonl")) == 5)  # a, b, c, b's retry, c's Reviewer
        # The server stopped with SIGINT while b's retry and c's Reviewer ran.
        (state / "release").touch()
        with running_server(**server, log_path=root / "second.log") as api:
            after = _wait(api, task_id, lambda d: d["status"] in ("completed", "failed", "stopped"))
            records = api.get("/api/llm-records", task_id=task_id, limit=500)["records"]

    assert after["status"] == "completed", (after.get("error"), (root / "second.log").read_text()[-3000:])
    assert after["result_summary"].startswith("## Delivered\nall four steps done\n\n## How to use\nrun d")
    assert before["status"] == "executing"
    assert (_steps(before)["b"]["retries"], _steps(before)["b"]["feedback"]) == (1, ["fix the parser"])
    steps = _steps(after)
    assert {k: s["status"] for k, s in steps.items()} == {k: "verified" for k in "abcd"}
    assert (steps["b"]["retries"], steps["b"]["feedback"]) == (1, ["fix the parser"])  # kept across the restart

    # Planned once; the restarted Planner resumed its session.
    turns = _log(state, "planner_turns.jsonl")
    assert [t for t in turns if t.startswith("[PLAN]")] == [turns[0]]
    argvs = _log(state, "planner_argv.jsonl")
    assert len(argvs) == 2 and "--resume" not in argvs[0]
    assert argvs[1][argvs[1].index("--resume") + 1] == "fake-planner-session"
    # The steps went on from where the stop left them: b's retry ran again with its feedback, c went back to
    # review, a was not run again, and d ran once its dependencies were accepted.
    runs = [(r["role"], r["step"], r.get("attempt"), r.get("feedback")) for r in _log(state, "runs.jsonl")]
    assert sorted(runs[:5]) == sorted([("worker", "a", 1, False), ("worker", "b", 1, False), ("worker", "c", 1, False),
                                       ("worker", "b", 2, True), ("reviewer", "c", None, None)])
    assert sorted(runs[5:7]) == sorted([("worker", "b", 2, True), ("reviewer", "c", None, None)])
    assert runs[7:] == [("worker", "d", 1, False)]
    # The M2 baseline held: each finished Planner turn records its own 0.001, never the session's total.
    planner_costs = [r["cost_usd"] for r in records if r["agent_id"].startswith("planner-") and r["cost_usd"]]
    assert planner_costs and all(c == pytest.approx(PLANNER_TURN_COST) for c in planner_costs)
    # The database holds the state: no plan or session file was written.
    assert list(runtime.glob(".local/**/*.json")) == []
    assert not (runtime / ".local" / "planner").exists() and not (runtime / ".local" / "manager").exists()


def test_a_task_failed_at_its_cap_continues_from_the_database_after_a_restart(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    bin_dir, state = _install(root)
    runtime, work, home = root / "runtime", root / "work", root / "home"
    for path in (runtime, work, home):
        path.mkdir()

    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "first.log") as api:
            # The plan turn ($0.001) leaves room for both Workers; their $0.002 each crosses the cap.
            project = api.post("/api/projects", {"name": "capped", "work_dir": str(work), "task_budget_usd": 0.0015})
            task_id = api.post("/api/tasks/create", {"requirement": "capped calc", "project_id": project["project_id"]})[
                "task_id"]
            api.post("/api/tasks/start", {"task_id": task_id})
            failed = _wait(api, task_id, lambda d: d["status"] == "failed")
        with running_server(**server, log_path=root / "second.log") as api:
            assert api.get(f"/api/tasks/{task_id}")["status"] == "failed"  # a failed task is not resumed
            api.post(f"/api/tasks/{task_id}/continue", {"task_budget_usd": 1})
            done = _wait(api, task_id, lambda d: d["status"] == "completed")

    assert failed["fail_kind"] == "task_budget"
    assert {k: s["status"] for k, s in _steps(done).items()} == {"x": "verified", "y": "verified"}
    turns = _log(state, "planner_turns.jsonl")
    assert [t for t in turns if t.startswith("[PLAN]")] == [turns[0]]  # planned once
    argvs = _log(state, "planner_argv.jsonl")
    assert argvs[-1][argvs[-1].index("--resume") + 1] == "fake-planner-session"
    assert list(runtime.glob(".local/**/*.json")) == []


def test_a_ready_task_stays_ready_through_a_restart_until_it_is_started(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    bin_dir, state = _install(root)
    (state / "release").touch()  # nothing the task runs waits
    runtime, work, home = root / "runtime", root / "work", root / "home"
    for path in (runtime, work, home):
        path.mkdir()

    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "first.log") as api:
            task_id = api.post("/api/tasks/create", {"requirement": "build calc",
                                                      "project": {"name": "calc", "work_dir": str(work)}})["task_id"]
        with running_server(**server, log_path=root / "second.log") as api:
            time.sleep(1)  # time for a resumed task to start its Planner
            ready, planned = api.get(f"/api/tasks/{task_id}")["status"], _log(state, "planner_argv.jsonl")
            api.post(f"/api/tasks/{task_id}/start", None)
            done = _wait(api, task_id, lambda d: d["status"] in ("completed", "failed", "stopped"))
            with pytest.raises(EvalError, match="answered 400"):
                api.post(f"/api/tasks/{task_id}/start", None)  # it is no longer ready

    assert (ready, planned) == ("init", [])
    assert done["status"] == "completed", (done.get("error"), (root / "second.log").read_text()[-3000:])
    assert len(_log(state, "planner_argv.jsonl")) == 1
