"""The fixed task set.

Each task is a requirement the real Planner gets, the files its working
directory starts with, an optional chat message sent to the Planner once the
plan exists, a check of the delivered files, and the flow it must show (at
least so many steps, reviews, retries or chat turns). ``fake`` scripts what
the stand-in Claude (``fake_claude.py``) does for it, so fake mode exercises
the same flow without a model.
"""

from __future__ import annotations

import json
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
    chat: str | None = None  # sent to the task's Planner once its plan exists
    expect: dict[str, int] = field(default_factory=dict)  # min_steps, min_reviews, min_retries, min_chat_turns
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
        expect={"min_steps": 2},
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
