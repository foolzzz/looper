"""A task's memory through a real ``loopx serve`` on a new database, with a stand-in ``claude``.

The Planner plans ``d``, then ``x`` after ``d``, ``y`` after ``x`` and ``c``
after ``d`` and ``y``, and accepts each step. Each Worker reports
``learnings``; code writes a journal entry when a step is accepted, the
Planner's next turn and a later Worker's first turn carry the entries back,
and a project with ``distill_lessons`` gets a lessons document in its
knowledge when the task completes. Runs only when LOOPX_TEST_DATABASE_URL
points at a disposable database (each run creates and drops one beside it).
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from tests.test_restart_resume_db import DSN, _log, _wait

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

LESSONS = "# Lessons\n\n- The build needs `make gen` first."
LEARNINGS = {"d": ["build needs `make gen` first"], "x": ["x found a flaky test"], "y": ["y found a slow API"], "c": []}

_FAKE_CLAUDE = r'''
import json, os, re, sys
STATE = sys.argv[1]
args = sys.argv[2:]
LEARNINGS = json.loads(os.environ["FAKE_LEARNINGS"])
prompt = ""
if "--append-system-prompt-file" in args:
    prompt = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()

def log(name, value):
    with open(os.path.join(STATE, name), "a") as f:
        f.write(json.dumps(value) + "\n")

def text_of(line):
    content = json.loads(line)["message"]["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content)

def answer(session, text):
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": "fake"}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                      "session_id": session, "total_cost_usd": 0.001}), flush=True)

def fenced(value):
    return "Done.\n\n```json\n" + json.dumps(value) + "\n```"

step = re.search(r"^Your step: (\S+)$", prompt, re.MULTILINE)
if step:
    label = step.group(1)
    log("workers.jsonl", {"step": label, "first_turn": text_of(sys.stdin.readline()), "prompt": prompt})
    report = {"status": "completed", "changed_files": {}, "deleted_files": [], "summary": f"{label} done",
              "learnings": LEARNINGS[label]}
    answer("fake-worker", "Done.\n" + json.dumps(report))
else:
    for line in sys.stdin:
        if not line.strip():
            continue
        text = text_of(line)
        log("planner_turns.jsonl", text)
        if text.startswith("[PLAN]"):
            def s(x, *deps):
                return {"id": x, "instruction": f"do {x}", "acceptance_criteria": [f"{x} works"],
                        "depends_on": list(deps), "review": False}
            reply = {"steps": [s("d"), s("x", "d"), s("y", "x"), s("c", "d", "y")]}
        elif text.startswith("[LESSONS]"):
            reply = {"lessons": os.environ["FAKE_LESSONS"]}
        else:
            reply = {"decision": "accept", "reason": "meets the criteria", "summary": "all done"}
        answer("fake-planner", fenced(reply))
'''


def _install(root: Path) -> tuple[Path, Path]:
    import json

    state = root / "state"
    state.mkdir()
    (root / "fake_claude.py").write_text(_FAKE_CLAUDE, encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f"export FAKE_LEARNINGS={shlex.quote(json.dumps(LEARNINGS))} FAKE_LESSONS={shlex.quote(LESSONS)}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(root / 'fake_claude.py'))} {shlex.quote(str(state))} "
        '"$@"\n'
    )
    claude.chmod(0o755)
    return bin_dir, state


@pytest.mark.parametrize("distill_lessons", [True, None], ids=["lessons on", "lessons off"])
def test_accepted_steps_are_journaled_fed_back_and_distilled_into_knowledge(
    tmp_path: Path, distill_lessons: bool | None,
) -> None:
    root = tmp_path.resolve()
    bin_dir, state = _install(root)
    runtime, work, home = root / "runtime", root / "work", root / "home"
    for path in (runtime, work, home):
        path.mkdir()

    with fresh_database(str(DSN)) as (database_url, _):
        with running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                            log_path=root / "serve.log") as api:
            project = api.post("/api/projects", {"name": "calc", "work_dir": str(work),
                                                 "distill_lessons": distill_lessons})
            assert project["distill_lessons"] is distill_lessons
            task_id = api.post("/api/tasks/create", {"requirement": "build the calc package",
                                                      "project_id": project["project_id"]})["task_id"]
            api.post("/api/tasks/start", {"task_id": task_id})
            done = _wait(api, task_id, lambda d: d["status"] in ("completed", "failed", "stopped"))
            knowledge = api.get(f"/api/projects/{project['project_id']}/knowledge")
            documents = [api.get(f"/api/projects/{project['project_id']}/knowledge/{doc['document_id']}")
                         for doc in knowledge["documents"]]

    assert done["status"] == "completed", (done.get("error"), (root / "serve.log").read_text()[-3000:])
    # An accepted step writes a journal entry with its summary and learnings, in the order accepted.
    journal = done["journal"]
    assert [(e["step_key"], e["kind"], e["summary"], e["learnings"]) for e in journal] == [
        (key, "step_accepted", f"{key} done", LEARNINGS[key]) for key in "dxyc"]

    # The Planner's next turn after d's acceptance carries d's entry, and the plan line its summary.
    turns = _log(state, "planner_turns.jsonl")
    on_d, on_x = (next(t for t in turns if t.startswith(f"[DECISION] Step {key} ")) for key in "dx")
    assert "Progress and findings" not in on_d
    progress = on_x[on_x.index("Progress and findings"):on_x.index("Result of step x")]
    assert "- d: d done\n  - build needs `make gen` first" in progress
    assert "- d [verified] do d" in on_x and "  summary: d done" in on_x

    # c depends on d and y: their entries come first (in that order), then x's, though x is newer than d.
    worker = {run["step"]: run for run in _log(state, "workers.jsonl")}
    assert "Findings so far" not in worker["d"]["first_turn"]
    first_turn = worker["c"]["first_turn"]
    findings = first_turn[first_turn.index("Findings so far in this task"):]
    assert findings.index("- d: d done") < findings.index("- y: y done") < findings.index("- x: x done")
    assert "build needs `make gen` first" in findings
    assert "Findings so far" not in worker["c"]["prompt"]  # data in the user turn, not instructions

    # With distill_lessons, completion adds one lessons document to the project's knowledge; without, nothing.
    lessons_turns = [t for t in turns if t.startswith("[LESSONS]")]
    if distill_lessons:
        assert len(lessons_turns) == 1 and "build needs `make gen` first" in lessons_turns[0]
        assert [(d["document"]["title"], d["document"]["source_kind"], d["content"]) for d in documents] == [
            ("Lessons: build the calc package", "text", LESSONS)]
    else:
        assert lessons_turns == [] and documents == []
