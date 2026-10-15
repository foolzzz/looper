"""The fixed task set.

Each task is a requirement the real Planner gets, the files its working
directory starts with, an optional chat message sent to the Planner (a
supplement) or new requirement text it is edited to, once the plan exists,
a check of the delivered files, and the flow it must show (at least so many
steps, reviews, retries or chat turns). ``fake`` scripts what
the stand-in Claude (``fake_claude.py``) does for it, so fake mode exercises
the same flow without a model.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalTask:
    id: str
    requirement: str
    check: Callable[[Path], str | None]  # the reason the delivered files are wrong, else None
    files: dict[str, str] = field(default_factory=dict)  # seeded into the working directory
    chat: str | None = None  # sent to the task's Planner once its plan exists (a supplement to the task)
    edit_requirement: str | None = None  # the requirement is edited to this once the plan exists
    # min_steps, min_reviews, min_retries, min_chat_turns, min_independent_pairs, min_bracketed_pairs,
    # min_parallel_workers, min_progress_notes, min_final_summary, min_plan_changes; max_plan_changes
    expect: dict[str, int] = field(default_factory=dict)
    fake: dict[str, Any] = field(default_factory=dict)  # the stand-in Claude's script


def _read(work_dir: Path, name: str) -> str | None:
    path = work_dir / name
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.is_file() else None


def _equals(name: str, expected: str) -> Callable[[Path], str | None]:
    def check(work_dir: Path) -> str | None:
        actual = _read(work_dir, name)
        if actual is None:
            return f"{name} is missing"
        return None if actual == expected else f"{name} holds {actual[:40]!r}, expected {expected!r}"

    return check


def _numbers_and_sum(work_dir: Path) -> str | None:
    numbers = _read(work_dir, "numbers.txt")
    if numbers is None:
        return "numbers.txt is missing"
    if numbers.split() != ["1", "2", "3", "4", "5"]:
        return "numbers.txt does not hold 1 to 5, one per line"
    return _equals("sum.txt", "15")(work_dir)


def _config_json(work_dir: Path) -> str | None:
    text = _read(work_dir, "config.json")
    if text is None:
        return "config.json is missing"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "config.json is not valid JSON"
    if not isinstance(data, dict) or data.get("name") != "demo" or data.get("port") != 8080:
        return "config.json does not have name \"demo\" and port 8080"
    return None


def _powers_and_table(work_dir: Path) -> str | None:
    for name, power in (("squares.txt", 2), ("cubes.txt", 3)):
        text = _read(work_dir, name)
        if text is None:
            return f"{name} is missing"
        if text.split() != [str(n ** power) for n in range(1, 6)]:
            return f"{name} does not hold n**{power} for n from 1 to 5, one per line"
    table = _read(work_dir, "table.txt")
    if table is None:
        return "table.txt is missing"
    expected = [f"{n} {n ** 2} {n ** 3}" for n in range(1, 6)]
    return None if [" ".join(line.split()) for line in table.splitlines()] == expected else \
        "table.txt does not hold `n n² n³` for n from 1 to 5, one per line"


def _client_calls_service(work_dir: Path) -> str | None:
    client = work_dir / "client" / "client.py"
    if not client.is_file():
        return "client/client.py is missing"
    if not (work_dir / "service" / "service.py").is_file():
        return "service/service.py is missing"
    for args, expected in ((["add", "2", "3"], "5"), (["mul", "4", "5"], "20")):
        command = "client.py " + " ".join(args)
        try:
            done = subprocess.run([sys.executable, str(client), *args], cwd=work_dir, capture_output=True, text=True,
                                  timeout=30, check=False)
        except subprocess.TimeoutExpired:
            return f"{command} did not finish in 30 s"
        if done.stdout.strip() != expected:
            return f"{command} printed {done.stdout.strip()[:40]!r}, expected {expected}"
    return None


# The fake Claude's service and client for interface_first: JSON {"op", "a", "b"} in, {"result"} out.
_FAKE_SERVICE = """\
import json, sys
request = json.load(sys.stdin)
ops = {"add": lambda a, b: a + b, "mul": lambda a, b: a * b}
json.dump({"result": ops[request["op"]](request["a"], request["b"])}, sys.stdout)
"""
_FAKE_CLIENT = """\
import json, os, subprocess, sys
op, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
service = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service", "service.py")
answer = subprocess.run([sys.executable, service], input=json.dumps({"op": op, "a": a, "b": b}),
                        capture_output=True, text=True, check=True)
print(json.loads(answer.stdout)["result"])
"""


def _build_log(work_dir: Path) -> str | None:
    text = _read(work_dir, "build.log")
    if text is None:
        return "build.log is missing"
    return None if "BUILD OK" in text else "build.log does not contain BUILD OK"


def _step(step_id: str, instruction: str, criteria: list[str], *, depends_on: list[str] | None = None,
          review: bool = False) -> dict[str, Any]:
    return {"id": step_id, "instruction": instruction, "acceptance_criteria": criteria,
            "depends_on": depends_on or [], "review": review}


# Fails on its first run and passes afterwards, so the first Worker's step fails.
_FLAKY_BUILD = """\
#!/bin/sh
if [ -f .build-attempted ]; then
  echo "BUILD OK"
  exit 0
fi
touch .build-attempted
echo "BUILD FAILED: transient error, run it again in a new attempt" >&2
exit 1
"""


# Two steps, the first slow enough that a change made once the plan exists reaches the Planner before the second
# starts; the change is to the second step.
_TWO_STEPS = ("Work in two steps, the second depending on the first. Step 1: run `sleep 20`, then create part1.txt "
              "in the working directory whose whole content is `one`. Step 2: create part2.txt in the working "
              "directory whose whole content is `{word}`.")


def _all(*checks: Callable[[Path], str | None]) -> Callable[[Path], str | None]:
    def check(work_dir: Path) -> str | None:
        return next((problem for c in checks if (problem := c(work_dir))), None)

    return check


def _two_steps_fake(changed: str | None) -> dict[str, Any]:
    """The script of a two-step task whose change the stand-in Planner answers by replanning the second step to
    write ``changed`` (None: a change that leaves the plan as it is, passed on to the running step)."""
    script: dict[str, Any] = {
        "plan": [_step("s1", "Run sleep 20, then create part1.txt containing one.", ["part1.txt holds one"]),
                 _step("s2", "Create part2.txt containing two.", ["part2.txt holds two"], depends_on=["s1"])],
        "attempts": {"s1": [{"status": "completed", "seconds": 3, "files": {"part1.txt": "one\n"}}],
                     "s2": [{"status": "completed", "files": {"part2.txt": "two\n"}}]},
    }
    if changed is not None:
        script["change_replan"] = [_step("s2b", f"Create part2.txt containing {changed}.",
                                         [f"part2.txt holds {changed}"], depends_on=["s1"])]
        script["attempts"]["s2b"] = [{"status": "completed", "files": {"part2.txt": f"{changed}\n"}}]
    return script


TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        id="single_step",
        requirement="Create a file named hello.txt in the working directory whose whole content is the line "
                    "`hello world`.",
        check=_equals("hello.txt", "hello world"),
        expect={"min_steps": 1},
        fake={
            "plan": [_step("s1", "Create hello.txt containing `hello world`.", ["hello.txt holds hello world"])],
            "attempts": {"s1": [{"status": "completed", "files": {"hello.txt": "hello world\n"}}]},
        },
    ),
    EvalTask(
        id="dependent_steps",
        requirement="In the working directory, first create numbers.txt holding the integers 1 to 5, one per "
                    "line. Then, as a separate step that depends on the first one, create sum.txt holding only "
                    "their sum, computed from numbers.txt.",
        check=_numbers_and_sum,
        # A task of several steps ends with a final summary of every field, and its decisions left progress notes.
        expect={"min_steps": 2, "min_progress_notes": 1, "min_final_summary": 1},
        fake={
            "plan": [
                _step("s1", "Create numbers.txt with 1 to 5, one per line.", ["numbers.txt holds 1..5"]),
                _step("s2", "Create sum.txt with the sum of numbers.txt.", ["sum.txt holds 15"], depends_on=["s1"]),
            ],
            "attempts": {
                "s1": [{"status": "completed", "files": {"numbers.txt": "1\n2\n3\n4\n5\n"}}],
                "s2": [{"status": "completed", "files": {"sum.txt": "15\n"}}],
            },
        },
    ),
    EvalTask(
        # More steps than the eight a plan was once limited to: the plan is taken as it is.
        id="many_steps",
        requirement="In the working directory, create nine files, f1.txt to f9.txt, each holding only its own "
                    "number (f1.txt holds 1, f2.txt holds 2, and so on). Make each file a step of its own, each "
                    "after the one before.",
        check=_all(*(_equals(f"f{i}.txt", str(i)) for i in range(1, 10))),
        expect={"min_steps": 9},
        fake={
            "plan": [_step(f"s{i}", f"Create f{i}.txt holding {i}.", [f"f{i}.txt holds {i}"],
                           depends_on=[f"s{i - 1}"] if i > 1 else None) for i in range(1, 10)],
            "attempts": {f"s{i}": [{"status": "completed", "files": {f"f{i}.txt": f"{i}\n"}}] for i in range(1, 10)},
        },
    ),
    EvalTask(
        # Two parts that need nothing of each other, then one that needs both: the plan should let the two run at
        # the same time.
        id="parallel_parts",
        requirement="In the working directory, produce three files, each by a step of its own: squares.txt holding "
                    "the squares of 1 to 5, one per line; cubes.txt holding the cubes of 1 to 5, one per line; and "
                    "table.txt, made from those two files, with one line per number n from 1 to 5 holding n, its "
                    "square and its cube separated by single spaces (for example `2 4 8`).",
        check=_powers_and_table,
        expect={"min_steps": 3, "min_independent_pairs": 1, "min_parallel_workers": 2},
        fake={
            "plan": [
                _step("s1", "Create squares.txt with the squares of 1 to 5.", ["squares.txt holds 1 4 9 16 25"]),
                _step("s2", "Create cubes.txt with the cubes of 1 to 5.", ["cubes.txt holds 1 8 27 64 125"]),
                _step("s3", "Create table.txt from squares.txt and cubes.txt.", ["table.txt holds n n² n³"],
                      depends_on=["s1", "s2"]),
            ],
            "attempts": {
                # Each takes a second, so the two run side by side as real Workers would.
                "s1": [{"status": "completed", "seconds": 1, "files": {"squares.txt": "1\n4\n9\n16\n25\n"}}],
                "s2": [{"status": "completed", "seconds": 1, "files": {"cubes.txt": "1\n8\n27\n64\n125\n"}}],
                "s3": [{"status": "completed",
                        "files": {"table.txt": "".join(f"{n} {n ** 2} {n ** 3}\n" for n in range(1, 6))}}],
            },
        },
    ),
    EvalTask(
        # A service and its client, which must agree on a request format the requirement leaves open: the plan
        # should fix the format first, build the two parts side by side, and check them together last.
        id="interface_first",
        requirement="Build a tiny calculator as a service and its client, in two directories of the working "
                    "directory. service/service.py is a command-line service: it reads one request from standard "
                    "input and writes its answer to standard output, both as JSON in a format you design, and it "
                    "can add and multiply two integers. client/client.py is a command-line client: `python "
                    "client/client.py add 2 3` sends the request to the service by running service/service.py "
                    "and prints only the result, here 5, and `python client/client.py mul 4 5` prints 20. The "
                    "client must not compute the result itself. Finish by checking the two together with those "
                    "two commands.",
        check=_client_calls_service,
        expect={"min_bracketed_pairs": 1, "min_parallel_workers": 2},
        fake={
            "plan": [
                _step("s1", "Write the request and answer format both parts use in PROTOCOL.md.",
                      ["PROTOCOL.md defines the request and the answer"]),
                _step("s2", "Write service/service.py to PROTOCOL.md.", ["service.py answers add and mul"],
                      depends_on=["s1"]),
                _step("s3", "Write client/client.py to PROTOCOL.md.", ["client.py runs the service"],
                      depends_on=["s1"]),
                _step("s4", "Run both client commands.", ["add 2 3 prints 5, mul 4 5 prints 20"],
                      depends_on=["s2", "s3"]),
            ],
            "attempts": {
                "s1": [{"status": "completed", "files": {
                    "PROTOCOL.md": 'Request: {"op": "add" | "mul", "a": int, "b": int}. Answer: {"result": int}.\n'}}],
                "s2": [{"status": "completed", "seconds": 1, "files": {"service/service.py": _FAKE_SERVICE}}],
                "s3": [{"status": "completed", "seconds": 1, "files": {"client/client.py": _FAKE_CLIENT}}],
                "s4": [{"status": "completed", "files": {}}],
            },
        },
    ),
    EvalTask(
        id="reviewed_step",
        requirement="Create config.json in the working directory: a JSON object whose `name` is the string "
                    "\"demo\" and whose `port` is the number 8080. An independent Reviewer must check this "
                    "file before the work counts as done.",
        check=_config_json,
        expect={"min_reviews": 1},
        fake={
            "plan": [_step("s1", "Create config.json with name demo and port 8080.",
                           ["config.json has name \"demo\" and port 8080"], review=True)],
            "attempts": {"s1": [{"status": "completed",
                                 "files": {"config.json": "{\"name\": \"demo\", \"port\": 8080}\n"}}]},
        },
    ),
    EvalTask(
        id="retry_after_failure",
        requirement="The working directory has build.sh, which has side effects, so only the Worker of the step "
                    "may run it. The step: run `sh build.sh` once and save its standard output in build.log. If "
                    "it exits non-zero, do not run it again in the same attempt and do not change build.sh; "
                    "report the step as failed with the error, so that it is retried in a new attempt. The task "
                    "is done when build.log contains BUILD OK.",
        files={"build.sh": _FLAKY_BUILD},
        check=_build_log,
        expect={"min_retries": 1},
        fake={
            "plan": [_step("s1", "Run `sh build.sh` once and save its output in build.log.",
                           ["build.log contains BUILD OK"])],
            "attempts": {"s1": [
                {"status": "failed", "files": {".build-attempted": ""},
                 "summary": "sh build.sh exited 1: BUILD FAILED: transient error"},
                {"status": "completed", "files": {"build.log": "BUILD OK\n"}},
            ]},
        },
    ),
    EvalTask(
        id="chat_correction",
        requirement="Create a file named color.txt in the working directory whose whole content is the single "
                    "word `red`.",
        chat="Correction: color.txt must contain the word `blue` instead of `red`. Make sure the final file "
             "says blue.",
        check=_equals("color.txt", "blue"),
        expect={"min_chat_turns": 1},
        fake={
            "plan": [_step("s1", "Create color.txt containing `red`.", ["color.txt holds red"])],
            "attempts": {"s1": [{"status": "completed", "files": {"color.txt": "red\n"}}]},
            # The Worker waits for the forwarded correction before it writes.
            "wait_for_input": {"s1": {"seconds": 20, "files": {"color.txt": "blue\n"}}},
        },
    ),
    EvalTask(
        id="supplement_replan",
        requirement=_TWO_STEPS.format(word="two"),
        chat="Change for the second step: part2.txt must contain `TWO-CHANGED` instead of `two`.",
        check=_all(_equals("part1.txt", "one"), _equals("part2.txt", "TWO-CHANGED")),
        # The second step's instruction and criteria say `two`: the plan must change, not only the Worker's reading.
        expect={"min_steps": 2, "min_chat_turns": 1, "min_plan_changes": 1},
        fake=_two_steps_fake("TWO-CHANGED"),
    ),
    EvalTask(
        id="requirement_edit",
        requirement=_TWO_STEPS.format(word="two"),
        edit_requirement=_TWO_STEPS.format(word="TWO-EDITED"),
        check=_all(_equals("part1.txt", "one"), _equals("part2.txt", "TWO-EDITED")),
        expect={"min_steps": 2, "min_chat_turns": 1, "min_plan_changes": 1},
        fake=_two_steps_fake("TWO-EDITED"),
    ),
    EvalTask(
        id="supplement_no_change",
        requirement=_TWO_STEPS.format(word="two"),
        chat="Just confirming, nothing changes: part2.txt should contain `two`, as the task says.",
        check=_all(_equals("part1.txt", "one"), _equals("part2.txt", "two")),
        expect={"min_steps": 2, "min_chat_turns": 1, "max_plan_changes": 0},
        fake=_two_steps_fake(None),
    ),
)


def select(ids: list[str] | None) -> tuple[EvalTask, ...]:
    """The tasks named in ``ids`` (all of them without), in the set's order; raises ``ValueError`` for an
    unknown id."""
    if not ids:
        return TASKS
    known = {task.id for task in TASKS}
    unknown = sorted(set(ids) - known)
    if unknown:
        raise ValueError(f"unknown task id(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    return tuple(task for task in TASKS if task.id in ids)
