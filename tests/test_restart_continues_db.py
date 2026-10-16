"""Restarting a task that ended goes on from its plan; starting it over plans it anew, on a real ``looper serve``.

One disposable ``looper serve`` on a new database runs a stand-in ``claude``
and one task per variant, each in a project of its own:

- ``none``, ``replan``, ``add``: the Planner plans ``a`` and ``b``, then ``c``
  after both and ``d`` after ``c``; ``c`` fails and the Planner fails the task
  (``a`` and ``b`` verified, ``c`` failed, ``d`` not started). Start (the
  dashboard's ``POST /api/tasks/start``) goes on from the plan: the Planner's
  [RESTART] turn answers ``none`` (``c`` and ``d`` run as they are),
  ``replan`` (``c`` gets a new instruction and ``d`` is dropped) or
  ``add_steps`` (``e`` after ``c``);
- ``startover``: the same failed task, started with ``start_over``: planned
  anew, every step run again;
- ``stop``: ``a`` is accepted while ``b`` runs, and the user stops the task;
- ``worktree``: the same, in a project with step workspaces: ``a`` is merged
  into the task's branch, ``b`` works in a worktree of its own when the task
  stops;
- ``noplan``: the user stops the task during its [PLAN] turn, before it has
  steps;
- ``agents``: the failed task of ``none`` gets a Worker profile and a Worker
  limit of 1 of its own before its restart, whose [RESTART] turn adds ``e``
  and ``f``, ready with ``c``.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database (the
run creates and drops a database beside it).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression.runner import Api, EvalError, fresh_database, init_repository, running_server

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

VARIANTS = ("none", "replan", "add", "startover", "stop", "worktree", "noplan", "agents")
FAIL_REASON = "c cannot parse the input"

# The stand-in. Every run finds its task's variant in its system prompt (the requirement says ``variant=<name>``)
# and logs what it did; a Worker of ``b`` in ``stop`` and ``worktree`` holds on its first run until the stop kills
# it, and the Planner of ``noplan`` holds its first [PLAN] turn until the test writes ``release-plan``.
_FAKE_CLAUDE = r'''
import json, os, re, sys, time
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

def logged(name):
    path = os.path.join(STATE, name)
    return [json.loads(line) for line in open(path)] if os.path.exists(path) else []

def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()

def answer(session, text):
    emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake"})
    emit({"type": "assistant", "session_id": session, "message": {"role": "assistant",
          "content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
          "total_cost_usd": 0.001, "usage": {"input_tokens": 10, "output_tokens": 10}})

def spec(step_id, depends_on, instruction=""):
    return {"id": step_id, "instruction": instruction or f"do {step_id}", "acceptance_criteria": [f"{step_id} works"],
            "depends_on": depends_on, "review": False}

step = re.search(r"^Your step: (\S+)$", prompt, re.MULTILINE)
if step:
    sys.stdin.readline()
    label = step.group(1)
    earlier = [r for r in logged("runs.jsonl") if r["variant"] == variant and r["step"] == label]
    partial = os.path.exists(f"{label}.txt")
    started = time.time()
    if variant == "agents":
        time.sleep(0.5)  # long enough that runs a limit lets overlap do overlap
    model = args[args.index("--model") + 1] if "--model" in args else ""
    log("runs.jsonl", {"step": label, "cwd": os.getcwd(), "partial": partial, "model": model, "start": started,
                       "end": time.time()})
    changed = {}
    if variant == "worktree":
        open(f"{label}.txt", "w").write("partial\n" if label == "b" and not earlier else f"{label} done\n")
        changed = {f"{label}.txt": "written"}
    if label == "b" and variant in ("stop", "worktree") and not earlier:
        time.sleep(120)  # the stop kills it first
    report = {"status": "completed", "changed_files": changed, "deleted_files": [], "summary": f"{label} done"}
    if label == "c" and not earlier:
        report = {"status": "failed", "changed_files": {}, "deleted_files": [], "summary": "c cannot parse the input"}
    answer("fake-worker", "Done.\n" + json.dumps(report))
else:
    log("planner_argv.jsonl", {"argv": args})
    for line in sys.stdin:
        if not line.strip():
            continue
        content = json.loads(line)["message"]["content"]
        text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
        log("planner_turns.jsonl", {"text": text})
        if text.startswith("[PLAN]") and variant == "noplan":
            while not os.path.exists(os.path.join(STATE, "release-plan")):
                time.sleep(0.1)
            reply = {"steps": [spec("a", [])]}
        elif text.startswith("[PLAN]") and variant in ("stop", "worktree"):
            reply = {"steps": [spec("a", []), spec("b", [])]}
        elif text.startswith("[PLAN]"):
            reply = {"steps": [spec("a", []), spec("b", []), spec("c", ["a", "b"]), spec("d", ["c"])]}
        elif text.startswith("[DECISION]") and "- status: failed" in text:
            reply = {"decision": "fail", "reason": "c cannot parse the input"}
        elif text.startswith("[DECISION]"):
            reply = {"decision": "accept", "reason": "meets the criteria", "summary": "done",
                     "final": {"delivered": "the steps", "how_to_use": "run it", "verification": "each check passed",
                               "open_issues": "none"}}
        elif text.startswith("[RESTART]") and variant == "replan":
            reply = {"reply": "c goes another way", "action": "replan",
                     "steps": [spec("c", ["a", "b"], "do c another way")]}
        elif text.startswith("[RESTART]") and variant == "add":
            reply = {"reply": "e checks c", "action": "add_steps", "steps": [spec("e", ["c"])]}
        elif text.startswith("[RESTART]") and variant == "agents":
            reply = {"reply": "e and f too", "action": "add_steps", "steps": [spec("e", []), spec("f", [])]}
        elif text.startswith("[RESTART]") or text.startswith("[CHAT]"):
            reply = {"reply": "Carrying on with the plan.", "action": "none"}
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


def _wait(api: Api, task_ids: dict[str, str], done: Callable[[str, dict[str, Any]], bool],
          timeout: float = 90.0) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        details = {variant: api.get(f"/api/tasks/{task_id}") for variant, task_id in task_ids.items()}
        if all(done(variant, detail) for variant, detail in details.items()):
            return details
        if time.monotonic() > deadline:
            raise AssertionError("timed out: " + "; ".join(
                f"{v} {d['status']} {_statuses(d)}" for v, d in details.items() if not done(v, d)))
        time.sleep(0.2)


def _statuses(detail: dict[str, Any]) -> dict[str, str]:
    return {s["step_key"]: s["status"] for s in detail.get("steps") or []}


def _ended(_variant: str, detail: dict[str, Any]) -> bool:
    return detail["status"] in ("completed", "failed", "stopped", "cancelled")


def _git(work_dir: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=work_dir, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def restarted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Every variant's task, before its restart and after it: what the API, the stand-in and git show."""
    root = tmp_path_factory.mktemp("restart").resolve()
    bin_dir, state = _install(root)
    runtime, home = root / "runtime", root / "home"
    for path in (runtime, home):
        path.mkdir()
    work = {variant: root / f"work-{variant}" for variant in VARIANTS}
    for path in work.values():
        path.mkdir()
    (work["worktree"] / "README").write_text("calc\n")
    init_repository(work["worktree"])

    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "serve.log") as api:
            ids: dict[str, str] = {}
            for variant in VARIANTS:
                project = {"name": f"calc-{variant}", "work_dir": str(work[variant])}
                if variant == "worktree":
                    project["step_workspaces"] = "worktree"
                ids[variant] = api.post("/api/tasks/create", {"requirement": f"build calc variant={variant}",
                                                               "project": project})["task_id"]
                api.post("/api/tasks/start", {"task_id": ids[variant]})
            with pytest.raises(EvalError, match="answered 400: .*start_over must be true or false"):
                api.post("/api/tasks/start", {"task_id": ids["none"], "start_over": "yes"})

            # Each task gets to where it ends: failed at c; b running (stopped below); the plan turn (stopped below).
            def ready(variant: str, detail: dict[str, Any]) -> bool:
                if variant in ("stop", "worktree"):
                    return _statuses(detail) == {"a": "verified", "b": "dispatched"} and len(
                        [r for r in _log(state, "runs.jsonl", variant) if r["step"] == "b"]) == 1
                if variant == "noplan":
                    return any(t["text"].startswith("[PLAN]") for t in _log(state, "planner_turns.jsonl", variant))
                return detail["status"] == "failed"

            _wait(api, ids, ready)
            for variant in ("stop", "worktree", "noplan"):
                api.post("/api/tasks/stop", {"task_id": ids[variant]})
            before = _wait(api, ids, _ended)
            merged_a = _git(work["worktree"], "log", "--format=%H", "--grep=^looper: merge step a")
            (state / "release-plan").touch()
            # The failed task's Workers run on another profile, and one at a time, from its restart on.
            api.post("/api/agent-profiles", {"name": "claude-code/sonnet", "runtime_id": "rt-claude-code",
                                             "provider_id": "prov-claude-subscription", "model": "sonnet"})
            agents = api.put(f"/api/tasks/{ids['agents']}/agents", {"profiles": {"worker": "claude-code/sonnet"},
                                                                     "concurrency": {"worker": 1}})["agents"]

            for variant in VARIANTS:
                api.post("/api/tasks/start", {"task_id": ids[variant], "start_over": variant == "startover"})
            after = _wait(api, ids, lambda variant, detail: detail["status"] == "completed")
            with pytest.raises(EvalError, match="answered 409: .*the task is completed"):
                api.put(f"/api/tasks/{ids['agents']}/agents", {"concurrency": {"worker": 2}})
            activity = {variant: [row["summary"] for row in api.get(f"/api/tasks/{task_id}/activity", limit=500)[
                "activity"]] for variant, task_id in ids.items()}

    return {"before": before, "after": after, "activity": activity, "state": state, "work": work,
            "merged_a": merged_a, "ids": ids, "agents": agents, "log": (root / "serve.log").read_text()}


def _turns(restarted: dict[str, Any], variant: str, kind: str) -> list[str]:
    return [t["text"] for t in _log(restarted["state"], "planner_turns.jsonl", variant) if t["text"].startswith(kind)]


def _runs(restarted: dict[str, Any], variant: str) -> list[str]:
    return [r["step"] for r in _log(restarted["state"], "runs.jsonl", variant)]


@pytest.mark.parametrize(("variant", "steps", "runs_after"), [
    # c and d run as they are.
    ("none", {"a": "verified", "b": "verified", "c": "verified", "d": "verified"}, ["c", "d"]),
    # c, back to not started, gets a new instruction; d stays as dropped history.
    ("replan", {"a": "verified", "b": "verified", "c": "verified", "d": "skipped"}, ["c"]),
    # e joins the plan after c.
    ("add", {"a": "verified", "b": "verified", "c": "verified", "d": "verified", "e": "verified"}, ["c", "d", "e"]),
])
def test_a_failed_task_restarts_from_its_plan_without_running_its_accepted_steps_again(
    restarted: dict[str, Any], variant: str, steps: dict[str, str], runs_after: list[str],
) -> None:
    before, after = restarted["before"][variant], restarted["after"][variant]
    assert after["status"] == "completed", (after.get("error"), restarted["log"][-3000:])
    assert _statuses(before) == {"a": "verified", "b": "verified", "c": "failed", "d": "pending"}
    assert _statuses(after) == steps
    # The accepted steps kept their rows and ran once: a and b before the failure, then only what was not done.
    ids = {s["step_key"]: s["id"] for s in before["steps"]}
    assert {s["step_key"]: s["id"] for s in after["steps"] if s["step_key"] in ids} == ids
    runs = _runs(restarted, variant)
    assert sorted(runs[:3]) == ["a", "b", "c"]
    assert runs[3] == runs_after[0] and sorted(runs[3:]) == sorted(runs_after)
    # Planned once; one [RESTART] turn, in the session the Planner had, with what is done and why it ended.
    assert len(_turns(restarted, variant, "[PLAN]")) == 1
    (restart,) = _turns(restarted, variant, "[RESTART]")
    assert restart.startswith(f"[RESTART] The task failed: {FAIL_REASON}. The user restarted it")
    assert "- a [verified] do a" in restart and "  summary: a done" in restart and "  summary: b done" in restart
    assert "- c [pending] do c" in restart and "- d [pending] do d" in restart
    assert "attempt 1: failed (last result: failed: c cannot parse the input) when the task ended" in restart
    argvs = [row["argv"] for row in _log(restarted["state"], "planner_argv.jsonl", variant)]
    assert argvs[-1][argvs[-1].index("--resume") + 1] == f"fake-planner-{variant}"
    assert any(line.startswith(f"Planner answered the restart ({'add_steps' if variant == 'add' else variant})")
               for line in restarted["activity"][variant])
    instruction = {s["step_key"]: s["instruction"] for s in after["steps"]}["c"]
    assert instruction == ("do c another way" if variant == "replan" else "do c")


def test_a_failed_tasks_new_worker_profile_and_limit_run_its_restart(restarted: dict[str, Any]) -> None:
    before, after = restarted["before"]["agents"], restarted["after"]["agents"]
    assert (before["status"], before["agents"]["editable"]) == ("failed", True)
    assert (after["status"], after["agents"]["editable"]) == ("completed", False)
    worker = restarted["agents"]["roles"]["worker"]
    assert (worker["profile"]["value"]["name"], worker["profile"]["source"]) == ("claude-code/sonnet", "task")
    assert (worker["concurrency"]["value"], worker["concurrency"]["source"]) == (1, "task")
    assert _statuses(after) == {step: "verified" for step in "abcdef"}
    runs = _log(restarted["state"], "runs.jsonl", "agents")
    # Before the change the built-in profile ran a, b and c's first attempt; after it, every run is on the new one.
    assert sorted((r["step"], r["model"]) for r in runs[:3]) == [("a", "opus"), ("b", "opus"), ("c", "opus")]
    assert sorted((r["step"], r["model"]) for r in runs[3:]) == [
        ("c", "sonnet"), ("d", "sonnet"), ("e", "sonnet"), ("f", "sonnet")]
    # c, e and f were ready at once, and ran one at a time.
    rerun = sorted(runs[3:], key=lambda r: r["start"])
    assert all(later["start"] >= earlier["end"] for earlier, later in zip(rerun, rerun[1:])), rerun
    assert "Worker waits for room in this task (1 running, limit 1)" in restarted["activity"]["agents"]


def test_a_stopped_task_runs_its_running_step_again_and_not_its_accepted_one(restarted: dict[str, Any]) -> None:
    before, after = restarted["before"]["stop"], restarted["after"]["stop"]
    assert before["status"] == "stopped" and _statuses(before) == {"a": "verified", "b": "dispatched"}
    assert _statuses(after) == {"a": "verified", "b": "verified"}
    assert sorted(_runs(restarted, "stop")) == ["a", "b", "b"]
    (restart,) = _turns(restarted, "stop", "[RESTART]")
    assert restart.startswith("[RESTART] The task was stopped. The user restarted it")
    assert "attempt 1: was running when the task ended; the restart put it back to not started" in restart
    entries = [(e["kind"], e["summary"]) for e in after["journal"] if e["step_id"] == before["steps"][1]["id"]]
    assert ("step_retry", "attempt 1: was running when the task ended; the restart put it back to not started") \
        in entries


def test_a_restart_keeps_merged_work_and_goes_on_in_the_running_steps_worktree(restarted: dict[str, Any]) -> None:
    work, after = restarted["work"]["worktree"], restarted["after"]["worktree"]
    assert _statuses(after) == {"a": "verified", "b": "verified"}
    runs = _log(restarted["state"], "runs.jsonl", "worktree")
    assert sorted(r["step"] for r in runs) == ["a", "b", "b"]
    first_b, second_b = [r for r in runs if r["step"] == "b"]
    # b's second attempt ran in the worktree of its first, which kept what that one had written.
    assert second_b["cwd"] == first_b["cwd"] and "/worktrees/" in second_b["cwd"] and second_b["partial"]
    # a's merge commit, made before the stop, is still on the branch; b's merge came after it.
    assert restarted["merged_a"] and _git(work, "log", "--format=%H", "--grep=^looper: merge step a") == \
        restarted["merged_a"]
    assert _git(work, "log", "--format=%s", "-3").splitlines()[0].startswith("looper: merge step b")
    assert (work / "a.txt").read_text() == "a done\n" and (work / "b.txt").read_text() == "b done\n"


def test_starting_over_plans_the_task_anew(restarted: dict[str, Any]) -> None:
    before, after = restarted["before"]["startover"], restarted["after"]["startover"]
    assert before["status"] == "failed" and after["status"] == "completed"
    # The old steps are gone; every step ran again; the new Planner started a new session.
    assert {s["id"] for s in after["steps"]}.isdisjoint({s["id"] for s in before["steps"]})
    assert _statuses(after) == {k: "verified" for k in "abcd"}
    assert sorted(_runs(restarted, "startover")) == sorted(["a", "b", "c", "a", "b", "c", "d"])
    assert len(_turns(restarted, "startover", "[PLAN]")) == 2 and _turns(restarted, "startover", "[RESTART]") == []
    argvs = [row["argv"] for row in _log(restarted["state"], "planner_argv.jsonl", "startover")]
    assert len(argvs) == 2 and "--resume" not in argvs[1]


def test_a_task_stopped_before_it_had_a_plan_is_planned_anew(restarted: dict[str, Any]) -> None:
    before, after = restarted["before"]["noplan"], restarted["after"]["noplan"]
    assert before["status"] == "stopped" and before["steps"] == []
    assert _statuses(after) == {"a": "verified"}
    assert len(_turns(restarted, "noplan", "[PLAN]")) == 2 and _turns(restarted, "noplan", "[RESTART]") == []
    assert "Restarted without a saved plan to continue from: planning anew" in restarted["activity"]["noplan"]
