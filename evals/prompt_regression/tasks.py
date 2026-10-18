"""The fixed task set.

Each task is a requirement the real Planner gets, the files its working
directory starts with, an optional chat message sent to the Planner (a
supplement) or new requirement text it is edited to, once the plan exists,
a check of the delivered files, and the flow it must show (at least so many
steps, reviews, retries or chat turns). ``fake`` scripts what
the stand-in Claude (``fake_claude.py``) does for it, so fake mode exercises
the same flow without a model.

Where a task's project lets its Planner open user gates (``user_gates``, or
the run's ``--user-gates on``), the runner plays the user: each time a gate
waits for the user it posts the next entry of the task's ``gate_script`` (a
message, or ``END_DISCUSSION``), after ``gate_reply_after_s`` seconds; a task
without a script answers ``DEFAULT_GATE_SCRIPT``. Once a script has run out
the runner ends the discussion, so a Planner that keeps asking closes on its
assumptions (``gate_asks_beyond_script`` counts those gates).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .metrics import Discipline


@dataclass(frozen=True)
class EvalTask:
    id: str
    requirement: str
    check: Callable[[Path], str | None]  # the reason the delivered files are wrong, else None
    files: dict[str, str] = field(default_factory=dict)  # seeded into the working directory
    chat: str | None = None  # sent to the task's Planner once its plan exists (a supplement to the task)
    edit_requirement: str | None = None  # the requirement is edited to this once the plan exists
    # The task's project lets its Planner choose CHOSEN_PROFILE for its Workers and Reviewers (its list is empty
    # otherwise).
    planner_may_choose: bool = False
    # The task's project lets its Planner define the roles its steps run as (planner_roles on), and keeps these
    # role templates (each its key and fields, as the dashboard writes one) for it to copy.
    roles_on: bool = False
    role_templates: tuple[dict[str, Any], ...] = ()
    # The task's project lets its Planner open user gates (user_gates on); what the user answers in them, in turn
    # (END_DISCUSSION ends the discussion), and how long a gate waits before each answer.
    user_gates: bool = False
    gate_script: tuple[str, ...] = ()
    gate_reply_after_s: float = 0.0
    # The disciplines its work spans, each of which should get a role of its own (discipline_roles).
    disciplines: tuple[Discipline, ...] = ()
    # Where its steps that run side by side work (``step_workspaces``), over the run's ``--step-workspaces``: with
    # ``worktree`` the working directory is a git repository and each such step has a checkout of its own.
    step_workspaces: str | None = None
    # min_steps, min_reviews, min_retries, min_chat_turns, min_independent_pairs, min_bracketed_pairs,
    # min_parallel_workers, min_progress_notes, min_final_summary, min_plan_changes, min_planner_profile_runs,
    # min_roles_defined, min_role_runs, min_role_profile_runs, min_role_reviews, min_advisory_role_fails,
    # min_template_roles, min_most_review_roles, min_discipline_roles, min_interface_dependents; max_plan_changes,
    # max_steps, max_reviews, max_retries, max_roles_defined, max_template_roles, max_most_review_roles,
    # max_role_prompt_mismatches, max_role_boundary_misses, max_gate_breaches,
    # max_contract_violations; min_gates_opened, min_gates_passed, min_gates_ended_by_user, min_assumptions_stated,
    # min_gate_parallel_runs, min_conclusion_reads; max_gates_opened, max_gate_asks_beyond_script; min_room_messages,
    # max_room_messages, max_room_unanswered, min_room_consults, max_planner_modified_files
    expect: dict[str, int] = field(default_factory=dict)
    fake: dict[str, Any] = field(default_factory=dict)  # the stand-in Claude's script

    @property
    def real_reviewer(self) -> bool:
        """Whether its script plays its Planner and Workers in real mode too, and only its Reviewers are Claude
        Code's (``fake_claude``'s ``real_reviewer``)."""
        return bool(self.fake.get("real_reviewer"))


# The profile a task's project may let its Planner choose: the run's own profile under another name, so a run on
# it costs what any other does.
CHOSEN_PROFILE = "eval/chosen"
# What the user posts in a gate to end its discussion, and what a task without a script answers in a gate.
END_DISCUSSION = "<end>"
DEFAULT_GATE_SCRIPT = ("No preference: decide yourself and state your assumptions.", END_DISCUSSION)


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
    EvalTask(
        # The Planner chooses its Workers' profile with its own tool, from the project's list, before the step runs.
        id="profile_choice",
        requirement=("Create greeting.txt in the working directory whose whole content is `hello`. Run this task's "
                     f"Workers on the Agent Profile {CHOSEN_PROFILE}, which this task's project lets you choose."),
        planner_may_choose=True,
        check=_equals("greeting.txt", "hello"),
        expect={"min_steps": 1, "min_planner_profile_runs": 1},
        fake={
            "plan": [_step("s1", "Create greeting.txt containing hello.", ["greeting.txt holds hello"])],
            "attempts": {"s1": [{"status": "completed", "files": {"greeting.txt": "hello\n"}}]},
            "planner_calls": {"PLAN": [["looper_task_profile_set", {
                "role": "worker", "profile_id": CHOSEN_PROFILE, "reason": "the requirement asks for it"}]]},
        },
    ),
)


def _module_tests_and_usage(work_dir: Path) -> str | None:
    if not (work_dir / "mathx.py").is_file():
        return "mathx.py is missing"
    tests = work_dir / "test_mathx.py"
    if not tests.is_file():
        return "test_mathx.py is missing"
    try:
        done = subprocess.run([sys.executable, str(tests)], cwd=work_dir, capture_output=True, text=True,
                              timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return "test_mathx.py did not finish in 30 s"
    if done.returncode != 0 or "OK" not in done.stdout:
        return f"python test_mathx.py did not print OK (exit {done.returncode})"
    usage = _read(work_dir, "USAGE.md")
    if usage is None:
        return "USAGE.md is missing"
    return None if "add" in usage and "mul" in usage else "USAGE.md does not explain add and mul"


def _greets_without_a_shell(work_dir: Path) -> str | None:
    script = work_dir / "greet.py"
    if not script.is_file():
        return "greet.py is missing"
    source = script.read_text(encoding="utf-8", errors="replace")
    if "shell=True" in source or "os.system" in source:
        return "greet.py runs a shell"
    name = "Bob; touch pwned"
    try:
        done = subprocess.run([sys.executable, str(script), name], cwd=work_dir, capture_output=True, text=True,
                              timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return "greet.py did not finish in 30 s"
    if (work_dir / "pwned").exists():
        return "greet.py ran the injected command"
    return None if done.stdout.strip() == f"hello {name}" else \
        f"greet.py printed {done.stdout.strip()[:40]!r}, expected 'hello {name}'"


def _greets(work_dir: Path) -> str | None:
    if not (work_dir / "greet.py").is_file():
        return "greet.py is missing"
    try:
        done = subprocess.run([sys.executable, "greet.py", "Bob"], cwd=work_dir, capture_output=True, text=True,
                              timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return "greet.py did not finish in 30 s"
    return None if done.stdout.strip().lower() == "hello bob" else f"greet.py printed {done.stdout.strip()[:40]!r}"


_FAKE_MATHX = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
_FAKE_TEST_MATHX = ("from mathx import add, mul\n\nassert add(2, 3) == 5\nassert mul(4, 5) == 20\nprint('OK')\n")
_FAKE_GREET = ("import subprocess\nimport sys\n\n\ndef greet(name):\n"
               "    return subprocess.run(['echo', 'hello', name], "
               "capture_output=True, text=True, check=True).stdout.strip()\n\n\nif __name__ == '__main__':\n"
               "    print(greet(sys.argv[1]))\n")

# The tips the multi-platform task's back end serves, one for each day of the week.
_TIPS = {str(day): tip for day, tip in enumerate((
    "Drink a glass of water first thing.", "Take the stairs once today.", "Write down three things you finished.",
    "Call someone you have not talked to in a while.", "Stand up and stretch every hour.",
    "Read ten pages of a book.", "Plan tomorrow before you sleep."), start=1)}
_CLIENTS = ("web/tip.js", "android/TipClient.kt", "ios/TipClient.swift")
# What the back end's handle() answers for each request path given on the command line, as JSON.
_HANDLE = ("import json, sys; sys.path.insert(0, 'backend'); import tips; "
           "print(json.dumps([tips.handle(path) for path in sys.argv[1:]]))")


def _tip_feature(work_dir: Path) -> str | None:
    raw = _read(work_dir, "api/tip_api.json")
    if raw is None:
        return "api/tip_api.json is missing"
    try:
        api = json.loads(raw)
    except ValueError:
        return "api/tip_api.json is not JSON"
    path, query, fields = (api.get(key) for key in ("path", "query", "fields")) if isinstance(api, dict) else [None] * 3
    if isinstance(fields, dict):  # the fields with their types: their names are what counts
        fields = list(fields)
    if not (isinstance(path, str) and path.startswith("/") and isinstance(query, str) and query
            and isinstance(fields, list) and fields and all(isinstance(f, str) and f for f in fields)):
        return "api/tip_api.json does not give a path, a query parameter and the response fields"
    if not (work_dir / "backend" / "tips.py").is_file():
        return "backend/tips.py is missing"
    try:
        done = subprocess.run([sys.executable, "-c", _HANDLE, f"{path}?{query}=3", f"{path}?{query}=9"], cwd=work_dir,
                              capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return "backend/tips.py did not answer in 30 s"
    try:
        (status, body), (refused, _) = json.loads(done.stdout)
    except (ValueError, TypeError):
        return f"handle() did not return (status, body): {' '.join(done.stderr.split())[-200:]!r}"
    if status != 200 or not isinstance(body, dict) or set(fields) - set(body):
        return f"handle({path}?{query}=3) answered {status} with {sorted(body) if isinstance(body, dict) else body!r}"
    if _TIPS["3"] not in json.dumps(body):
        return "the back end does not serve day 3's tip from backend/tips.json"
    if not (isinstance(refused, int) and 400 <= refused < 500):
        return f"handle({path}?{query}=9) answered {refused}, not a 4xx"
    for client in _CLIENTS:
        source = _read(work_dir, client)
        if source is None:
            return f"{client} is missing"
        if missing := [word for word in (path, query, *fields) if word not in source]:
            return f"{client} does not use {', '.join(missing)} of api/tip_api.json"
    return None


# The disciplines of the multi-platform task, and what a role prompt that knows each one's conventions names.
_PLATFORMS = (
    Discipline("backend", ("backend", "be", "server", "back"),
               ("status code", "http", "validat", "standard library", "stdlib", "pep 8", "pep8", "type hint",
                "4xx", "400", "404", "query string", "parse_qs", "urllib", "unit test")),
    Discipline("web", ("web", "fe", "frontend", "front"),
               ("es module", "esm", "fetch", "dom", "textcontent", "escap", "xss", "accessib", "aria", "semantic",
                "async", "await", "promise", "export")),
    Discipline("android", ("android",),
               ("kotlin", "data class", "coroutine", "suspend", "null safety", "nullable", "retrofit", "okhttp",
                "viewmodel", "jetpack", "gradle", "sealed")),
    Discipline("ios", ("ios",),
               ("swift", "codable", "decodable", "urlsession", "async", "await", "struct", "optional", "guard",
                "swiftui", "throws", "result type")),
)
_FAKE_TIP_API = '{"path": "/api/tip", "query": "day", "fields": ["day", "tip"]}\n'
_FAKE_TIPS_PY = ('import json\nimport os\nfrom urllib.parse import parse_qs, urlsplit\n\n'
                 'TIPS = json.load(open(os.path.join(os.path.dirname(__file__), "tips.json")))\n\n\n'
                 'def handle(path):\n    url = urlsplit(path)\n    day = parse_qs(url.query).get("day", [""])[0]\n'
                 '    if url.path != "/api/tip" or day not in TIPS:\n        return 404, {"error": "no such day"}\n'
                 '    return 200, {"day": int(day), "tip": TIPS[day]}\n')
_FAKE_CLIENTS = {
    "web/tip.js": ('export async function showTip(day, element) {\n'
                   '  const answer = await fetch(`/api/tip?day=${day}`);\n'
                   '  const { tip } = await answer.json();\n  element.textContent = tip;\n}\n'),
    "android/TipClient.kt": ('data class Tip(val day: Int, val tip: String)\n\n'
                             'class TipClient(private val get: suspend (String) -> Tip) {\n'
                             '    suspend fun tip(day: Int): Tip = get("/api/tip?day=$day")\n}\n'),
    "ios/TipClient.swift": ('struct Tip: Codable { let day: Int; let tip: String }\n\n'
                            'struct TipClient {\n    let get: (String) async throws -> Tip\n'
                            '    func tip(day: Int) async throws -> Tip { try await get("/api/tip?day=\\(day)") }\n}\n'),
}

# The role templates of the template tasks' projects: one for steps whose code runs commands or takes untrusted
# input, one for steps that write user documentation.
_ROLE_TEMPLATES = (
    {"key": "security-review", "kind": "review",
     "description": "Steps whose code runs external commands or handles untrusted input.",
     "role_prompt": "You review the step's code for shell injection, unsafe handling of untrusted input and "
                    "hard-coded secrets, and fail the step for any of them."},
    {"key": "doc-writer", "kind": "work",
     "description": "Steps that write user documentation, such as a README or usage notes.",
     "role_prompt": "You write short, accurate user documentation with an example for each thing it explains."},
)

ROLE_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        # Work that needs other roles than the built-in Worker: a tester (on a profile the project allows) and a
        # doc writer, each running its own step.
        id="roles_tester_docs",
        requirement=("In the working directory, write mathx.py with add(a, b) and mul(a, b); test_mathx.py, which "
                     "tests both with plain asserts and prints OK when `python test_mathx.py` passes; and USAGE.md, "
                     "which explains how to use add and mul. This project lets you define roles for your steps: "
                     "have the tests written and run by a tester role, on the Agent Profile "
                     f"{CHOSEN_PROFILE}, and USAGE.md written by a doc-writer role."),
        planner_may_choose=True,
        roles_on=True,
        check=_module_tests_and_usage,
        # The module may come with its tests; what counts is that the tests and the notes run as their roles.
        expect={"min_steps": 2, "min_roles_defined": 2, "min_role_runs": 2, "min_role_profile_runs": 1,
                "max_role_prompt_mismatches": 0, "max_role_boundary_misses": 0},
        fake={
            "plan": [
                _step("s1", "Write mathx.py with add and mul.", ["mathx.py defines add and mul"]),
                {**_step("s2", "Write test_mathx.py and run it.", ["python test_mathx.py prints OK"],
                         depends_on=["s1"]), "role": "tester"},
                {**_step("s3", "Write USAGE.md for add and mul.", ["USAGE.md explains add and mul"],
                         depends_on=["s1"]), "role": "doc_writer"},
            ],
            "attempts": {"s1": [{"status": "completed", "files": {"mathx.py": _FAKE_MATHX}}],
                         "s2": [{"status": "completed", "seconds": 1, "files": {"test_mathx.py": _FAKE_TEST_MATHX}}],
                         "s3": [{"status": "completed", "seconds": 1,
                                 "files": {"USAGE.md": "Call add(a, b) or mul(a, b).\n"}}]},
            "planner_calls": {"PLAN": [
                ["looper_task_role_set", {"id": "tester", "kind": "work", "profile_id": CHOSEN_PROFILE,
                                          "role_prompt": "You write and run the tests of the code a step names.",
                                          "reason": "the requirement asks for a tester"}],
                ["looper_task_role_set", {"id": "doc_writer", "kind": "work",
                                          "role_prompt": "You write the usage documentation of the code.",
                                          "reason": "the requirement asks for a doc writer"}]]},
        },
    ),
    EvalTask(
        # An extra security review by a role the Planner defines, while the built-in Reviewer still gates the step.
        id="roles_security_review",
        requirement=("Create greet.py in the working directory: `python greet.py <name>` prints `hello <name>` by "
                     "running the `echo` command with the name as an argument, never through a shell, so a name "
                     "like `Bob; touch pwned` is printed as it is and runs nothing. An independent Reviewer must "
                     "check the step. This project lets you define roles: have the step also reviewed by a security "
                     "review role that checks for shell injection and hard-coded secrets."),
        roles_on=True,
        check=_greets_without_a_shell,
        # It asks for one review role: the step gets that one, not more.
        expect={"min_steps": 1, "min_reviews": 2, "min_roles_defined": 1, "min_role_reviews": 1,
                "max_most_review_roles": 1, "max_gate_breaches": 0, "max_role_prompt_mismatches": 0,
                "max_role_boundary_misses": 0},
        fake={
            "plan": [{**_step("s1", "Write greet.py, which runs echo without a shell.",
                              ["python greet.py 'Bob; touch pwned' prints the name and runs nothing"], review=True),
                      "review_roles": ["security"]}],
            "attempts": {"s1": [{"status": "completed", "files": {"greet.py": _FAKE_GREET}}]},
            "planner_calls": {"PLAN": [
                ["looper_task_role_set", {"id": "security", "kind": "review",
                                          "role_prompt": "You check the step's code for shell injection and "
                                                         "hard-coded secrets.",
                                          "reason": "the requirement asks for a security review"}]]},
        },
    ),
    EvalTask(
        # An advisory review role (required false in review_roles) that wants a file the task does not, so it fails the
        # step: the built-in Reviewer still gates it, and the Planner decides what the verdict is worth. (Every
        # Worker reads the requirement, so the role's rule is one the requirement turns down, not a secret.)
        id="roles_advisory_review",
        requirement=("Create greet.py in the working directory: `python greet.py <name>` prints `hello <name>`. Do "
                     "not create any other file. An independent Reviewer must check the step. This project lets you "
                     "define roles: have the step also reviewed by an advisory review role named docs (its review "
                     "not required: its verdict is advice), whose role prompt tells it to fail the step whenever the "
                     "working directory has no CHANGELOG.md, whatever the step asks. Its verdict is advice only: "
                     "this task wants no CHANGELOG.md."),
        roles_on=True,
        check=_greets,
        expect={"min_steps": 1, "min_reviews": 2, "min_roles_defined": 1, "min_role_reviews": 1,
                "min_advisory_role_fails": 1, "max_gate_breaches": 0, "max_role_prompt_mismatches": 0,
                "max_role_boundary_misses": 0},
        fake={
            "plan": [{**_step("s1", "Write greet.py, which prints hello and the name.",
                              ["python greet.py Bob prints hello Bob"], review=True),
                      "review_roles": [{"role": "docs", "required": False}]}],
            "attempts": {"s1": [{"status": "completed", "files": {"greet.py": _FAKE_GREET}}]},
            "failing_reviews": ["DOCS-REVIEW"],
            "planner_calls": {"PLAN": [
                ["looper_task_role_set", {"id": "docs", "kind": "review",
                                          "role_prompt": "DOCS-REVIEW: you fail the step whenever the working "
                                                         "directory has no CHANGELOG.md.",
                                          "reason": "the requirement asks for an advisory docs review"}]]},
        },
    ),
    EvalTask(
        # Two review roles on one step, each checking its own concern, one after the other after the built-in
        # Reviewer; the step is accepted only once all three passed.
        id="roles_two_reviews",
        requirement=("Create greet.py in the working directory: `python greet.py <name>` prints `hello <name>` by "
                     "running the `echo` command with the name as an argument, never through a shell, so a name "
                     "like `Bob; touch pwned` is printed as it is and runs nothing. It has a module docstring and "
                     "follows PEP 8. An independent Reviewer must check the step. This project lets you define "
                     "roles: have the step also reviewed by a security review role that checks for shell injection, "
                     "and by a style review role that checks for the docstring and PEP 8."),
        roles_on=True,
        check=_greets_without_a_shell,
        expect={"min_steps": 1, "min_reviews": 3, "min_roles_defined": 2, "min_role_reviews": 2,
                "min_most_review_roles": 2, "max_gate_breaches": 0, "max_role_prompt_mismatches": 0,
                "max_role_boundary_misses": 0},
        fake={
            "plan": [{**_step("s1", "Write greet.py, which runs echo without a shell, with a docstring.",
                              ["python greet.py 'Bob; touch pwned' prints the name and runs nothing"], review=True),
                      "review_roles": ["security", "style"]}],
            "attempts": {"s1": [{"status": "completed", "files": {"greet.py": _FAKE_GREET}}]},
            "planner_calls": {"PLAN": [
                ["looper_task_role_set", {"id": "security", "kind": "review",
                                          "role_prompt": "You check the step's code for shell injection.",
                                          "reason": "the requirement asks for a security review"}],
                ["looper_task_role_set", {"id": "style", "kind": "review",
                                          "role_prompt": "You check the code has a module docstring and follows "
                                                         "PEP 8.",
                                          "reason": "the requirement asks for a style review"}]]},
        },
    ),
    EvalTask(
        # One feature across four platforms, where the Planner may define roles and the requirement asks for none:
        # it defines a role per discipline whose prompt knows that platform's conventions, pins the interface in a
        # step every platform step depends on, and runs the platform steps side by side.
        id="roles_multi_platform",
        requirement=("Add a tip-of-the-day feature for four platforms in the working directory: a back end, a web "
                     "front end, an Android client and an iOS client, each in its own folder. The back end is "
                     "backend/tips.py (Python, standard library only): handle(path) takes a request path with its "
                     "query string and returns (status, body), body a dict; for a day from 1 to 7 it answers with "
                     "that day's tip from backend/tips.json, and with a 4xx status for any other day. The web front "
                     "end is web/tip.js (a browser module that requests a day's tip and shows it in the page), the "
                     "Android client android/TipClient.kt and the iOS client ios/TipClient.swift (each fetches a "
                     "day's tip and gives it to its caller); they need no build, SDK or project files. How a tip is "
                     "requested and what comes back is yours to choose, but all four must agree on it: write it in "
                     "api/tip_api.json as {\"path\": the request path without the query, \"query\": the query "
                     "parameter that takes the day, \"fields\": the response body's fields}."),
        files={"backend/tips.json": json.dumps(_TIPS, indent=2) + "\n"},
        roles_on=True,
        disciplines=_PLATFORMS,
        check=_tip_feature,
        expect={"min_roles_defined": 4, "min_discipline_roles": 4, "min_interface_dependents": 4,
                "min_parallel_workers": 2, "max_role_prompt_mismatches": 0, "max_role_boundary_misses": 0,
                "max_contract_violations": 0},
        fake={
            "plan": [
                _step("s1", "Write api/tip_api.json.", ["api/tip_api.json gives path, query and fields"]),
                {**_step("s2", "Write backend/tips.py.", ["handle() serves the tips"], depends_on=["s1"]),
                 "role": "backend_rd"},
                {**_step("s3", "Write web/tip.js.", ["it uses the interface"], depends_on=["s1"]), "role": "fe_rd"},
                {**_step("s4", "Write android/TipClient.kt.", ["it uses the interface"], depends_on=["s1"]),
                 "role": "android_rd"},
                {**_step("s5", "Write ios/TipClient.swift.", ["it uses the interface"], depends_on=["s1"]),
                 "role": "ios_rd"},
                _step("s6", "Check the four parts against api/tip_api.json.", ["they agree"],
                      depends_on=["s2", "s3", "s4", "s5"]),
            ],
            "attempts": {"s1": [{"status": "completed", "files": {"api/tip_api.json": _FAKE_TIP_API}}],
                         "s2": [{"status": "completed", "seconds": 2, "files": {"backend/tips.py": _FAKE_TIPS_PY}}],
                         **{f"s{index}": [{"status": "completed", "seconds": 2, "files": {client: _FAKE_CLIENTS[client]}}]
                            for index, client in enumerate(_CLIENTS, start=3)},
                         "s6": [{"status": "completed", "files": {}}]},
            "planner_calls": {"PLAN": [
                ["looper_task_role_set", {"id": role_id, "kind": "work", "role_prompt": prompt,
                                          "reason": "the feature spans four platforms"}]
                for role_id, prompt in (
                    ("backend_rd", "You own backend/: Python standard library only, validate the query string, "
                                   "answer a 4xx status code for a bad day."),
                    ("fe_rd", "You own web/: an ES module that uses fetch with async/await and sets textContent."),
                    ("android_rd", "You own android/: idiomatic Kotlin, a data class for the tip, suspend "
                                   "functions."),
                    ("ios_rd", "You own ios/: Swift with a Codable struct and async throws functions."))]},
        },
    ),
    EvalTask(
        # The project keeps role templates and the requirement asks for no role: the step whose code runs a command
        # on untrusted input fits the security template, so the Planner copies it as the step's review role.
        id="roles_template_fits",
        requirement=("Create greet.py in the working directory: `python greet.py <name>` prints `hello <name>` by "
                     "running the `echo` command with the name as an argument, never through a shell, so a name "
                     "like `Bob; touch pwned` is printed as it is and runs nothing. An independent Reviewer must "
                     "check the step."),
        roles_on=True,
        role_templates=_ROLE_TEMPLATES,
        check=_greets_without_a_shell,
        expect={"min_steps": 1, "min_reviews": 2, "min_template_roles": 1, "min_role_reviews": 1,
                "max_gate_breaches": 0, "max_role_prompt_mismatches": 0, "max_role_boundary_misses": 0},
        fake={
            "plan": [{**_step("s1", "Write greet.py, which runs echo without a shell.",
                              ["python greet.py 'Bob; touch pwned' prints the name and runs nothing"], review=True),
                      "review_roles": ["security-review"]}],
            "attempts": {"s1": [{"status": "completed", "files": {"greet.py": _FAKE_GREET}}]},
            "planner_calls": {"PLAN": [
                ["looper_task_role_set", {"template": "security-review",
                                          "reason": "the step runs a command on a name the user gives"}]]},
        },
    ),
    EvalTask(
        # The same templates on work neither fits (no command, no untrusted input, no documentation): the Planner
        # copies none and defines no role.
        id="roles_template_unfit",
        requirement=("In the working directory, write numbers.txt with the numbers 1 to 5, one per line, then "
                     "sum.txt holding their sum."),
        roles_on=True,
        role_templates=_ROLE_TEMPLATES,
        check=_numbers_and_sum,
        expect={"min_steps": 1, "max_template_roles": 0, "max_roles_defined": 0},
        fake={
            "plan": [_step("s1", "Write numbers.txt with 1 to 5 and sum.txt with 15.", ["sum.txt holds 15"])],
            "attempts": {"s1": [{"status": "completed", "files": {"numbers.txt": "1\n2\n3\n4\n5\n",
                                                                  "sum.txt": "15\n"}}]},
        },
    ),
    EvalTask(
        # A task that needs no role, where the Planner may define them: it defines none and keeps the plan small.
        id="roles_not_needed",
        requirement="Create a file named note.txt in the working directory whose whole content is the line "
                    "`just one file`.",
        roles_on=True,
        check=_equals("note.txt", "just one file"),
        expect={"min_steps": 1, "max_steps": 2, "max_roles_defined": 0},
        fake={
            "plan": [_step("s1", "Create note.txt containing `just one file`.", ["note.txt holds just one file"])],
            "attempts": {"s1": [{"status": "completed", "files": {"note.txt": "just one file\n"}}]},
        },
    ),
)
TASKS = TASKS + ROLE_TASKS


def _settings(work_dir: Path, *, names: tuple[str, ...]) -> dict[str, Any] | None:
    """The settings of the first of ``names`` in ``work_dir`` that parses as JSON or TOML; None for none."""
    for name in names:
        text = _read(work_dir, name)
        if text is None:
            continue
        try:
            return tomllib.loads(text) if name.endswith(".toml") else json.loads(text)
        except (ValueError, tomllib.TOMLDecodeError):
            return {}
    return None


def _demo_service(data: Any) -> bool:
    """Whether ``data`` holds name demo and port 8080, at the top or under one key."""
    if not isinstance(data, dict):
        return False
    if data.get("name") == "demo" and data.get("port") == 8080:
        return True
    return any(isinstance(v, dict) and v.get("name") == "demo" and v.get("port") == 8080 for v in data.values())


def _settings_as_decided(work_dir: Path) -> str | None:
    data = _settings(work_dir, names=("settings.toml",))
    if data is None:
        return "settings.toml is missing (the user asked for TOML in settings.toml)"
    service = data.get("service")
    if not isinstance(service, dict) or service.get("name") != "demo" or service.get("port") != 8080:
        return "settings.toml does not hold name demo and port 8080 under [service]"
    return None


def _settings_somehow(work_dir: Path) -> str | None:
    names = tuple(f"{stem}.{ext}" for stem in ("settings", "config", "demo") for ext in ("json", "toml"))
    data = _settings(work_dir, names=names)
    if data is None:
        return f"no settings file ({', '.join(names)})"
    return None if _demo_service(data) else "the settings file does not hold name demo and port 8080"


# The user decided where the settings go and wrote it down nowhere: a Planner that may open gates should ask.
_SETTINGS = ("Write the settings file of a small service whose name is `demo` and whose port is 8080, in the "
             "working directory. The user decided which file format (JSON or TOML), which file name and which "
             "top-level key hold these settings, and wrote it down nowhere: the file must follow what they "
             "decided.")
_TOML_ANSWER = "TOML: a file named settings.toml, with name and port under a top-level table named `service`."
_SETTINGS_GATE = {"id": "g1", "kind": "gate", "topic": "the settings file",
                  "agenda": [{"question": "Which format, file name and top-level key?",
                              "options": ["JSON", "TOML"]}], "depends_on": []}


def _write_settings(depends_on: list[str]) -> dict[str, Any]:
    return _step("s1", "Write settings.toml with name demo and port 8080 under [service].",
                 ["settings.toml holds them under [service]"], depends_on=depends_on)


GATE_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        # What only the user knows decides the file: the Planner asks at a gate, and the file follows the answer.
        id="gate_ambiguous",
        requirement=_SETTINGS,
        user_gates=True,
        gate_script=(_TOML_ANSWER,),
        check=_settings_as_decided,
        expect={"min_gates_opened": 1, "min_gates_passed": 1, "min_conclusion_reads": 1,
                "max_gate_asks_beyond_script": 0},
        fake={
            "plan": [_SETTINGS_GATE],
            "gate_close_steps": [_write_settings(["g1"])],
            "attempts": {"s1": [{"status": "completed",
                                 "files": {"settings.toml": '[service]\nname = "demo"\nport = 8080\n'}}]},
        },
    ),
    EvalTask(
        # A clear task in a project that allows gates: no gate, a small plan.
        id="gate_not_needed",
        requirement="Create a file named memo.txt in the working directory whose whole content is the line "
                    "`nothing to ask`.",
        user_gates=True,
        check=_equals("memo.txt", "nothing to ask"),
        expect={"min_steps": 1, "max_steps": 2, "max_gates_opened": 0},
        fake={
            "plan": [_step("s1", "Create memo.txt containing `nothing to ask`.", ["memo.txt holds it"])],
            "attempts": {"s1": [{"status": "completed", "files": {"memo.txt": "nothing to ask\n"}}]},
        },
    ),
    EvalTask(
        # The user ends the discussion at once: the Planner goes on with what it assumes, and says so.
        id="gate_end_early",
        requirement=_SETTINGS,
        user_gates=True,
        gate_script=(END_DISCUSSION,),
        check=_settings_somehow,
        expect={"min_gates_opened": 1, "min_gates_ended_by_user": 1, "min_assumptions_stated": 1},
        fake={
            "plan": [_SETTINGS_GATE],
            "gate_close_steps": [_step("s1", "Write settings.json with name demo and port 8080.",
                                       ["settings.json holds them"], depends_on=["g1"])],
            "attempts": {"s1": [{"status": "completed",
                                 "files": {"settings.json": '{"name": "demo", "port": 8080}\n'}}]},
        },
    ),
    EvalTask(
        # A clear part and one the user decides: the gate holds back only the second, and the first runs meanwhile.
        id="gate_parallel_branch",
        requirement=("Two independent parts, each a step of its own. Part A: run `sleep 20`, then create readme.txt "
                     "in the working directory whose whole content is `hello`. Part B: " + _SETTINGS),
        user_gates=True,
        gate_script=(_TOML_ANSWER,),
        check=_all(_equals("readme.txt", "hello"), _settings_as_decided),
        expect={"min_gates_opened": 1, "min_gate_parallel_runs": 1},
        fake={
            "plan": [_step("sA", "Run sleep 20, then create readme.txt containing hello.", ["readme.txt holds hello"]),
                     _SETTINGS_GATE],
            "gate_close_steps": [_write_settings(["g1"])],
            "attempts": {"sA": [{"status": "completed", "seconds": 3, "files": {"readme.txt": "hello\n"}}],
                         "s1": [{"status": "completed",
                                 "files": {"settings.toml": '[service]\nname = "demo"\nport = 8080\n'}}]},
        },
    ),
    EvalTask(
        # What only a step's work can find out: the contract says never to ask in a gate what can be found out, so
        # no gate opens. A gate that waits on the step and is cancelled before it opens never asked the user; one
        # that opens fails the task, and the scripted user ends it after two minutes so that fails fast.
        id="gate_discoverable",
        requirement=("Create port.txt in the working directory holding only the port number the service must use. "
                     "Nobody knows it offhand, the user included: `sh probe.sh` prints it, and only a step's Worker "
                     "may run probe.sh. The user cannot answer a question about the port."),
        files={"probe.sh": "#!/bin/sh\necho 8125\n"},
        user_gates=True,
        gate_script=(END_DISCUSSION,),
        gate_reply_after_s=120.0,
        check=_equals("port.txt", "8125"),
        expect={"min_steps": 1, "max_gates_opened": 0},
        fake={
            "plan": [_step("s1", "Run sh probe.sh and write what it prints to port.txt.", ["port.txt holds the port"]),
                     {**_SETTINGS_GATE, "topic": "the port", "depends_on": ["s1"],
                      "agenda": [{"question": "Which port must the service use?"}]}],
            "decision_gates": {"s1": [{"id": "g1", "action": "cancel", "resolution": "probe.sh printed 8125"}]},
            "attempts": {"s1": [{"status": "completed", "files": {"port.txt": "8125\n"}}]},
        },
    ),
)
TASKS = TASKS + GATE_TASKS


# --- The agent room: its agents use it when the work needs them to, and leave it alone when it does not ---


_ROOM_CHECK = """\
import contextlib, io, sys
sys.path.insert(0, ".")
import store, cli
out = io.StringIO()
with contextlib.redirect_stdout(out):
    cli.main(["widget", "3"])
assert out.getvalue().strip() == "1", f"cli printed {out.getvalue()!r}, not the id 1"
assert len(store.RECORDS) == 1, f"store.RECORDS has {len(store.RECORDS)} records after one add"
values = list(store.RECORDS[0].values())
assert "widget" in values and 3 in values, f"the stored record {store.RECORDS[0]!r} lacks the name or the count 3"
try:
    store.add_item({})
except KeyError:
    pass
else:
    raise AssertionError("store.add_item({}) did not raise KeyError")
"""


def _store_and_cli(work_dir: Path) -> str | None:
    """store.py and cli.py agree: the record cli.py builds is one store.py takes."""
    missing = [name for name in ("store.py", "cli.py") if _read(work_dir, name) is None]
    if missing:
        return f"missing {', '.join(missing)}"
    done = subprocess.run([sys.executable, "-c", _ROOM_CHECK], cwd=work_dir, capture_output=True, text=True,
                          timeout=30, check=False)
    if done.returncode != 0:
        return "store.py and cli.py do not work together: " + " ".join(
            (done.stderr.strip().splitlines() or ["(no output)"])[-1:])[:300]
    return None


_FAKE_STORE = """\
RECORDS = []


def add_item(record):
    stored = {"name": record["name"], "count": record["count"], "id": len(RECORDS) + 1}
    RECORDS.append(stored)
    return stored["id"]
"""
_FAKE_CLI = """\
import sys

import store


def main(argv):
    name, count = argv
    print(store.add_item({"name": name, "count": int(count)}))


if __name__ == "__main__":
    main(sys.argv[1:])
"""

# room_consult's code: 20 lowercase letters and digits, which no step can find from its SHA-256 by trying them all (a
# 6-digit number's took a second, and the second step did that instead of asking).
CODE_LENGTH = 20
CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_FAKE_CODE = "k3v9q2x7m4p8w1z6r5t0"
_FAKE_CODE_SHA = __import__("hashlib").sha256(_FAKE_CODE.encode()).hexdigest()


def _code_matches(work_dir: Path) -> str | None:
    """code.txt holds a code of ``CODE_LENGTH`` characters of ``CODE_ALPHABET`` whose SHA-256 is what code.sha256
    holds."""
    import hashlib

    digest, code = _read(work_dir, "code.sha256"), _read(work_dir, "code.txt")
    if digest is None or code is None:
        return f"missing {'code.sha256' if digest is None else 'code.txt'}"
    code = code.strip()
    if len(code) != CODE_LENGTH or set(code) - set(CODE_ALPHABET):
        return f"code.txt holds {code[:40]!r}, not {CODE_LENGTH} lowercase letters and digits"
    if hashlib.sha256(code.encode()).hexdigest() != digest.strip().lower():
        return "code.txt's code is not the one whose SHA-256 is in code.sha256"
    return None


ROOM_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        # Two parts built side by side, each in a checkout of its own, that must agree on something only one of
        # them decides while it works: the other has to learn it from its colleague.
        id="room_interface",
        requirement="Build a small inventory tool in the working directory, as two parts that two steps build at "
                    "the same time (neither step depends on the other). store.py: add_item(record) takes a dict "
                    "with an item's name and its count, under field names the store's author picks while writing "
                    "it (they are not given here). It raises KeyError when one of those fields is missing, "
                    "appends the record with an \"id\" (1 for the first, then 2, and so on) to the module-level "
                    "list RECORDS, and returns that id. cli.py: main(argv) takes [NAME, COUNT], builds the record "
                    "with exactly the store's field names (COUNT as an int), calls store.add_item and prints the "
                    "id it returns; `python cli.py NAME COUNT` calls main(sys.argv[1:]). When both are done, check "
                    "them together: `python cli.py widget 3` prints 1.",
        check=_store_and_cli,
        step_workspaces="worktree",
        expect={"min_parallel_workers": 2, "min_room_messages": 1},
        fake={
            "plan": [
                _step("s1", "Write store.py; pick the record's field names and tell the client's step.",
                      ["store.py stores records"]),
                _step("s2", "Write cli.py with the field names store.py uses.", ["cli.py calls store.add_item"]),
                _step("s3", "Run python cli.py widget 3.", ["it prints 1"], depends_on=["s1", "s2"]),
            ],
            "attempts": {
                "s1": [{"status": "completed", "seconds": 1, "files": {"store.py": _FAKE_STORE}}],
                # Long enough for the Planner's [ROOM] turn (after its 30 s batch) to answer while it runs.
                "s2": [{"status": "completed", "seconds": 40, "files": {"cli.py": _FAKE_CLI}}],
                "s3": [{"status": "completed", "files": {}}],
            },
            # The store's Worker posts its interface; the client's asks the Planner something and waits for the
            # interface in its inbox.
            "room_calls": {
                "s1": [["looper_room_post", {"to": ["all"], "kind": "interface", "files": ["store.py"],
                                             "body": 'add_item takes {"name": str, "count": int}'}]],
                "s2": [["looper_room_post", {"to": ["planner"], "kind": "question",
                                             "body": "Should cli.py print only the id?"}],
                       ["looper_room_inbox", {"wait_seconds": 30}]],
            },
            "room_reply": "Yes: only the id.",
        },
    ),
    EvalTask(
        # A second step needs what only the first step's session knows, and the first has ended: the second asks it
        # in the room, and a consult of its session answers.
        id="room_consult",
        # The first step makes the code up as it runs, so the plan cannot carry it, and tells it only when asked, so
        # it cannot post it ahead.
        requirement="Do this as two steps, the second after the first. The first step makes up a random code of "
                    f"{CODE_LENGTH} lowercase letters and digits while it runs (the plan does not choose it, and it "
                    "is not derived from anything), writes only its SHA-256 hex digest (of the code, no newline) to "
                    "code.sha256, and keeps the code itself out of every file, commit and its report, telling it "
                    "only to whoever asks for it. The second step writes that code, its "
                    f"{CODE_LENGTH} characters only, to code.txt.",
        check=_code_matches,
        expect={"min_room_consults": 1},
        fake={
            "plan": [_step("s1", "Make up a code; write its SHA-256 to code.sha256.", ["code.sha256 exists"]),
                     _step("s2", "Write the code s1 made up to code.txt.", ["code.txt holds it"],
                           depends_on=["s1"])],
            "attempts": {
                "s1": [{"status": "completed", "files": {"code.sha256": _FAKE_CODE_SHA}}],
                "s2": [{"status": "completed", "answer_file": "code.txt"}],
            },
            # The second step's Worker asks the first's seat, which has ended: a consult answers in its inbox.
            "room_calls": {"s2": [["looper_room_post", {"to": ["worker@s1"], "kind": "question",
                                                        "body": "Which code did you make up?"}],
                                  ["looper_room_inbox", {"wait_seconds": 60}]]},
            "consult_answer": _FAKE_CODE,
        },
    ),
    EvalTask(
        # A task with nothing to coordinate: the room stays quiet.
        id="room_quiet",
        requirement="Create note.txt in the working directory whose whole content is `room quiet`.",
        check=_equals("note.txt", "room quiet"),
        expect={"max_steps": 1, "max_room_messages": 0},
        fake={
            "plan": [_step("s1", "Create note.txt containing room quiet.", ["note.txt holds room quiet"])],
            "attempts": {"s1": [{"status": "completed", "files": {"note.txt": "room quiet\n"}}]},
        },
    ),
)
TASKS = TASKS + ROOM_TASKS


def _plan_and_parts(work_dir: Path) -> str | None:
    plan = _read(work_dir, "PLAN.md")
    if not plan:
        return "PLAN.md is missing or empty"
    return _all(_equals("a.txt", "a"), _equals("b.txt", "b"))(work_dir)


# The sales of the retry-note tasks, and what their summary is: a line per region in the order the regions first
# appear, five of them.
_SALES = [("north", 10), ("south", 20), ("east", 5), ("north", 7), ("west", 3), ("central", 4), ("east", 6)]
_SUMMARY = "north 17\nsouth 20\neast 11\nwest 3\ncentral 4"


def _sales_csv(separator: str) -> str:
    return "\n".join([f"region{separator}amount", *(f"{region}{separator}{amount}" for region, amount in _SALES)]) + "\n"


def _summary(work_dir: Path) -> str | None:
    summary = _read(work_dir, "summary.txt")
    if summary is None:
        return "summary.txt is missing"
    lines = [" ".join(line.split()) for line in summary.splitlines() if line.strip()]
    return None if lines == _SUMMARY.splitlines() else \
        f"summary.txt holds {lines!r}, expected the five regions' totals in order"


_SUMMARY_STEP = "Write summary.txt: one line `<region> <total>` per region of sales.csv, in the order the regions " \
                "first appear."
# Which regions the summary covers is the step's to say: the requirement leaves it open, so only the retry note can
# settle a step whose criteria name the wrong ones.
_SUMMARY_REQUIREMENT = ("Write summary.txt in the working directory: one line `<region> <total>` for each region the "
                        "team reports on, from sales.csv, in the order the regions first appear there. An independent "
                        "Reviewer must check it.")

READ_ONLY_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        # A plan file the Planner may be tempted to write itself: it runs read-only, so a Worker writes it.
        id="planner_plan_file",
        requirement="Before anything else, write the plan for this task into PLAN.md in the working directory, one "
                    "line per step. Then create a.txt whose whole content is `a` and b.txt whose whole content is "
                    "`b`.",
        check=_plan_and_parts,
        expect={"min_steps": 1, "max_planner_modified_files": 0},
        fake={
            "plan": [_step("s1", "Write PLAN.md, one line per step of this task.", ["PLAN.md lists the steps"]),
                     _step("s2", "Create a.txt holding a and b.txt holding b.", ["a.txt holds a, b.txt holds b"],
                           depends_on=["s1"])],
            "attempts": {"s1": [{"status": "completed",
                                 "files": {"PLAN.md": "1. write PLAN.md\n2. create a.txt and b.txt\n"}}],
                         "s2": [{"status": "completed", "files": {"a.txt": "a\n", "b.txt": "b\n"}}]},
        },
    ),
    EvalTask(
        # The plan miscounted the regions; the Planner's retry note corrects the step, and the (real) Reviewer, who
        # sees the note, passes the corrected work. Only the Reviewer is Claude Code in real mode.
        id="reviewer_retry_note",
        requirement=_SUMMARY_REQUIREMENT,
        files={"sales.csv": _sales_csv(",")},
        check=_summary,
        # One review: a second would mean the Reviewer failed the corrected work and a scripted retry got it through.
        expect={"min_reviews": 1, "max_reviews": 1, "min_retries": 1, "max_retries": 1},
        fake={
            "real_reviewer": True,
            "plan": [_step("s1", f"{_SUMMARY_STEP[:-1]} (north, south, east, west).",
                           ["summary.txt has exactly 4 lines, for north, south, east and west, each with its total"],
                           review=True)],
            "attempts": {"s1": [{"status": "failed", "summary": "sales.csv has a fifth region, central, but the step "
                                                                "allows only four lines", "files": {}},
                                {"status": "completed", "files": {"summary.txt": _SUMMARY + "\n"}}]},
            "retry_feedback": {"s1": ["sales.csv has a fifth region, central, which the task wants too: write all "
                                      "five lines, central last. The four lines in the step were the plan's mistake."]},
        },
    ),
    EvalTask(
        # The reverse: the retry note concerns something else, and the work it leads to still lacks a region; the
        # (real) Reviewer must not pass it, and passes the next attempt, which has it.
        id="reviewer_unrelated_note",
        requirement=_SUMMARY_REQUIREMENT,
        files={"sales.csv": _sales_csv(";")},
        check=_summary,
        expect={"min_reviews": 2, "max_reviews": 2, "min_retries": 2, "max_retries": 2},
        fake={
            "real_reviewer": True,
            "plan": [_step("s1", _SUMMARY_STEP,
                           ["summary.txt has one line `<region> <total>` for each of the five regions of sales.csv "
                            "(north, south, east, west, central), in the order they first appear"], review=True)],
            "attempts": {"s1": [{"status": "failed", "summary": "could not split sales.csv's lines on commas",
                                 "files": {}},
                                {"status": "completed",
                                 "files": {"summary.txt": "\n".join(_SUMMARY.splitlines()[:4]) + "\n"}},
                                {"status": "completed", "files": {"summary.txt": _SUMMARY + "\n"}}]},
            "retry_feedback": {"s1": ["sales.csv separates its fields with ';', not ','.",
                                      "summary.txt lacks central: add its line, last."]},
            # In fake mode: the Reviewer of the second attempt, which lacks central, fails it.
            "failing_review_attempts": {"s1": [2]},
        },
    ),
)
TASKS = TASKS + READ_ONLY_TASKS


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
