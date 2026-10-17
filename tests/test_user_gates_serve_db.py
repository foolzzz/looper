"""User gates on a real ``looper serve`` with a stand-in ``claude``, through the dashboard's API.

One disposable ``looper serve`` on a new database runs one task per variant, each in a project of its own:

- ``pass``: the project lets its Planners open gates. The Planner plans ``s1`` (exploring) and gate ``g1``; while
  ``s1`` runs the gate waits and the task needs the user; the user answers on the dashboard; the Planner closes the
  gate and adds ``s2``, whose Worker reads what was settled;
- ``off``: the project does not (the default): a gate in the plan gets a repair turn, and the contract says nothing
  of gates;
- ``restart``: ``s1`` fails while the gate waits, and the Planner fails the task; the user writes in the gate of the
  stopped task, then starts it again going on from its plan: the gate still waits, gets the message, and closes;
- ``startover``: the same failed task started over: its gate and discussion are gone, and the new plan has none.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database (the run creates and drops a database
beside it).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression.runner import fresh_database, running_server

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

VARIANTS = ("pass", "off", "restart", "startover")

_FAKE_CLAUDE = r'''
import json, os, re, sys
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
    return [r for r in (json.loads(line) for line in open(path)) if r["variant"] == variant] \
        if os.path.exists(path) else []

def answer(session, text):
    for event in ({"type": "system", "subtype": "init", "session_id": session, "model": "fake"},
                  {"type": "assistant", "session_id": session,
                   "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
                  {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
                   "total_cost_usd": 0.001, "usage": {"input_tokens": 10, "output_tokens": 10}}):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

def spec(step_id, *deps):
    return {"id": step_id, "instruction": f"do {step_id}", "acceptance_criteria": [f"{step_id} works"],
            "depends_on": list(deps), "review": False}

GATE = {"id": "g1", "kind": "gate", "topic": "storage", "depends_on": [],
        "agenda": [{"question": "Which database?", "options": ["PostgreSQL", "SQLite"]}]}
work = re.search(r"^Your step: (\S+)$", prompt, re.MULTILINE)
if work:
    sys.stdin.readline()
    label = work.group(1)
    first = not [r for r in logged("runs.jsonl") if r["step"] == label]
    log("runs.jsonl", {"step": label, "prompt": prompt})
    failed = variant in ("restart", "startover") and label == "s1" and first
    if label == "s1" and variant == "pass":
        # Long enough for the user to answer at the gate while it runs.
        while not [t for t in logged("turns.jsonl") if t["text"].startswith("[GATE]")]:
            import time; time.sleep(0.1)
    report = {"status": "failed" if failed else "completed", "changed_files": {}, "deleted_files": [],
              "summary": f"{label} {'failed' if failed else 'done'}"}
    answer("fake-worker", "Done.\n" + json.dumps(report))
    sys.exit(0)

log("planner.jsonl", {"prompt": prompt})
FINAL = {"delivered": "the steps", "how_to_use": "run it", "verification": "each check passed", "open_issues": "none"}
for line in sys.stdin:
    if not line.strip():
        continue
    content = json.loads(line)["message"]["content"]
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
    log("turns.jsonl", {"text": text})
    plans = [t for t in logged("turns.jsonl") if t["text"].startswith("[PLAN] Plan the task")]
    if "Your last reply broke the reply contract" in text:
        reply = {"steps": [spec("s1")]}
    elif text.startswith("[PLAN]") and variant == "startover" and len(plans) > 1:
        reply = {"steps": [spec("s9")]}
    elif text.startswith("[PLAN]"):
        reply = {"steps": [spec("s1"), GATE]}
    elif text.startswith("[GATE]"):
        reply = {"reply": "PostgreSQL then.", "gate": "close",
                 "agenda": [{"id": "q1", "question": "Which database?", "status": "resolved", "answer": "PostgreSQL"}],
                 "summary": {"decisions": ["PostgreSQL"], "assumptions": [], "open": []},
                 "action": "add_steps", "steps": [spec("s2", "g1")]}
    elif text.startswith("[RESTART]"):
        reply = {"reply": "going on", "action": "none"}
    elif text.startswith("[DECISION]"):
        step = re.search(r"^\[DECISION\] Step (\S+)", text).group(1)
        final = "the task's last open step" in text
        if "status: failed" in text:
            reply = {"decision": "fail", "reason": f"{step} failed"}
        else:
            reply = {"decision": "accept", "summary": "done", **({"final": FINAL} if final else {})}
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


def _until(check: Any, what: str, seconds: float = 120) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.2)


ENDED = ("completed", "failed", "stopped", "cancelled")


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Every variant's task once it ended (``restart`` and ``startover`` after they were started again), what the
    stand-in logged, and what the API showed on the way."""
    root = tmp_path_factory.mktemp("user-gates").resolve()
    bin_dir, state = _install(root)
    runtime, home = root / "runtime", root / "home"
    for path in (runtime, home):
        path.mkdir()
    seen: dict[str, Any] = {}
    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "serve.log") as api:
            ids: dict[str, str] = {}
            for variant in VARIANTS:
                work = root / f"work-{variant}"
                work.mkdir()
                project = api.post("/api/projects", {"name": f"calc-{variant}", "work_dir": str(work)})
                if variant != "off":
                    api.put(f"/api/projects/{project['project_id']}", {"user_gates": "on"})
                ids[variant] = api.post("/api/tasks/create", {"requirement": f"build calc variant={variant}",
                                                              "project_id": project["project_id"]})["task_id"]
                api.post("/api/tasks/start", {"task_id": ids[variant]})

            def detail(variant: str) -> dict[str, Any]:
                return api.get(f"/api/tasks/{ids[variant]}")

            # pass: the gate waits for the user while s1 runs; the task needs the user.
            seen["waiting"] = _until(lambda: (d := detail("pass"))["gates"] and d["gates"][0]["state"] == "open"
                                     and d["status"] == "waiting_user" and d, "the pass gate to open")
            seen["needs_you"] = [t["task_id"] for t in api.get("/api/tasks", group="needs_you")["tasks"]]
            seen["answered"] = api.post(f"/api/tasks/{ids['pass']}/gates/g1/messages", {
                "text": "PostgreSQL", "agenda_item": "q1", "option": "PostgreSQL"})
            first = {v: _until(lambda v=v: (d := detail(v))["status"] in ENDED and d, f"{v} to end") for v in VARIANTS}
            # restart: the user writes in the gate of the failed task, which keeps the message for the restart.
            seen["kept"] = api.post(f"/api/tasks/{ids['restart']}/gates/g1/messages", {"text": "PostgreSQL please"})
            seen["before_restart"] = detail("restart")["gates"]
            api.post("/api/tasks/start", {"task_id": ids["restart"]})
            api.post("/api/tasks/start", {"task_id": ids["startover"], "start_over": True})
            time.sleep(0.5)
            again = {v: _until(lambda v=v: (d := detail(v))["status"] in ENDED and d, f"{v} to end again")
                     for v in ("restart", "startover")}
            threads = {v: api.get(f"/api/tasks/{ids[v]}/gates/g1/messages") for v in ("pass", "restart")}
    return {"first": first, "again": again, "ids": ids, "state": state, "seen": seen, "threads": threads,
            "log": (root / "serve.log").read_text()}


def _turns(ran: dict[str, Any], variant: str) -> list[str]:
    return [t["text"] for t in _log(ran["state"], "turns.jsonl", variant)]


def test_the_user_answers_at_the_gate_while_another_step_runs_and_the_next_step_reads_the_answer(
    ran: dict[str, Any],
) -> None:
    detail = ran["first"]["pass"]
    assert detail["status"] == "completed", ran["log"][-3000:]
    waiting = ran["seen"]["waiting"]
    assert waiting["group"] == "needs_you" and ran["ids"]["pass"] in ran["seen"]["needs_you"]
    assert ran["seen"]["answered"]["delivered"] == "queued"
    (gate,) = detail["gates"]
    assert (gate["step"], gate["state"], gate["summary"]["decisions"], gate["turns"]) == ("g1", "passed",
                                                                                           ["PostgreSQL"], 1)
    assert gate["plan_change"] == {"added": ["s2"], "dropped": [], "stopped": []}
    turns = _turns(ran, "pass")
    gate_turn = next(t for t in turns if t.startswith("[GATE]"))
    assert re.search(r'^- \(dashboard, \d\d:\d\d UTC, chose "PostgreSQL" for q1\) PostgreSQL$', gate_turn, re.MULTILINE)
    s2 = next(r["prompt"] for r in _log(ran["state"], "runs.jsonl", "pass") if r["step"] == "s2")
    # The task section lists it with the supplements, each line indented under its bullet.
    assert "- Settled with the user at gate g1 (storage):\n  Decisions:\n  - PostgreSQL" in s2
    assert [(m["role"], m["kind"]) for m in ran["threads"]["pass"]["messages"]] == [
        ("looper", "opened"), ("user", "message"), ("planner", "reply"), ("looper", "closed")]
    kinds = [e["kind"] for e in detail["journal"]]
    assert kinds.count("gate_opened") == kinds.count("gate_closed") == 1
    assert [(s["via"], s["delivered_at"] is not None) for s in detail["supplements"]] == [("gate", True)]
    assert "[GATE]: the user wrote in a gate's discussion" in _log(ran["state"], "planner.jsonl", "pass")[0]["prompt"]


def test_where_gates_are_off_a_gate_is_refused_and_the_contract_says_nothing_of_them(ran: dict[str, Any]) -> None:
    detail = ran["first"]["off"]
    assert detail["status"] == "completed" and detail["gates"] == []
    assert "[GATE]" not in _log(ran["state"], "planner.jsonl", "off")[0]["prompt"]
    assert "this project does not let you open gates (user_gates is off)" in _turns(ran, "off")[1]


def test_going_on_from_the_plan_keeps_the_waiting_gate_and_gives_it_the_message_kept_meanwhile(
    ran: dict[str, Any],
) -> None:
    assert ran["first"]["restart"]["status"] == "failed"
    assert ran["seen"]["kept"]["delivered"] == "kept"
    assert [g["state"] for g in ran["seen"]["before_restart"]] == ["discussing"]
    detail = ran["again"]["restart"]
    assert detail["status"] == "completed", ran["log"][-3000:]
    (gate,) = detail["gates"]
    assert gate["state"] == "passed"
    kinds = [(m["role"], m["kind"]) for m in ran["threads"]["restart"]["messages"]]
    assert kinds == [("looper", "opened"), ("user", "message"), ("looper", "resumed"), ("planner", "reply"),
                     ("looper", "closed")]
    turns = _turns(ran, "restart")
    assert any(t.startswith("[RESTART]") and '- g1 [gate: discussing' in t for t in turns)
    assert re.search(r"^- \(dashboard, \d\d:\d\d UTC\) PostgreSQL please$",
                     next(t for t in turns if t.startswith("[GATE]")), re.MULTILINE)


def test_starting_over_drops_the_gate_and_its_discussion(ran: dict[str, Any]) -> None:
    assert ran["first"]["startover"]["status"] == "failed"
    assert [g["state"] for g in ran["first"]["startover"]["gates"]] == ["open"]
    detail = ran["again"]["startover"]
    assert detail["status"] == "completed" and detail["gates"] == []
    assert [s["step_key"] for s in detail["steps"]] == ["s9"]
