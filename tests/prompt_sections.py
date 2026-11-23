"""The Planner's default model-facing text, frozen section by section against one baseline.

The baseline (``fixtures/planner_prompt_sections.json``) keeps, for each rendering a test checks, the heading and
the sha256 of every section: a section starts at a Markdown heading line (``## ...``) or a reply-contract block
(``[PLAN]: ...``), and the text before the first one is a section too. A rendering must still have each frozen
section, byte for byte and in the same order; a whole section another feature adds between them is allowed, while
any other byte change lands in a frozen section and fails. An intended change to the Planner's default text is one
edit here: run the checks with ``UPDATE_PLANNER_SECTIONS=1`` and review which sections the diff names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

BASELINE = Path(__file__).parent / "fixtures" / "planner_prompt_sections.json"
_START = re.compile(r"## .*|\[[A-Z_]+\]:")


def sections(text: str) -> list[list[str]]:
    """``[heading, sha256]`` for each section of ``text``, in order; the heading of the text before the first
    section start is ``""``, and a contract block's heading is its ``[NAME]``."""
    found: list[list[str]] = []
    heading, lines = "", []
    for line in text.splitlines(keepends=True):
        start = _START.match(line)
        if start and (lines or found):
            found.append([heading, hashlib.sha256("".join(lines).encode()).hexdigest()])
            lines = []
        if start:
            heading = start.group(0).rstrip(":") if line.startswith("[") else line.rstrip("\n")
        lines.append(line)
    found.append([heading, hashlib.sha256("".join(lines).encode()).hexdigest()])
    return found


def assert_sections(text: str, frozen: list[list[str]], *, label: str) -> None:
    """``text`` keeps every ``[heading, sha256]`` in ``frozen``, in that order.

    A whole section added between the frozen ones is allowed. A changed byte, a missing section or a reorder fails,
    naming the section.
    """
    actual = sections(text)
    at, problems = 0, []
    for heading, digest in frozen:
        found = next((i for i in range(at, len(actual)) if actual[i][0] == heading), None)
        if found is None:
            problems.append(f"missing or moved: {heading or '(start)'}")
            continue
        if actual[found][1] != digest:
            problems.append(f"changed: {heading or '(start)'}")
        at = found + 1
    assert not problems, f"{label}: " + "; ".join(problems)


def assert_frozen(name: str, text: str) -> None:
    """``text`` keeps every section frozen for ``name``, unchanged and in order; added sections are allowed."""
    actual = sections(text)
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    if os.environ.get("UPDATE_PLANNER_SECTIONS") == "1":
        baseline[name] = actual
        BASELINE.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
    assert_sections(text, baseline[name], label=name)


def planner_prompt() -> Any:
    """The Planner's system prompt for a fixed plan, settings and harness, with every optional feature off."""
    from looper.agents.agent_settings import Harness, Layer
    from looper.agents.knowledge import EMPTY_KNOWLEDGE
    from looper.agents.tiers.planner import PlannerAgent, PlannerSettings, TaskPlan

    return PlannerAgent(task_id="task-baseline", runtime_root=Path("."))._system_prompt(
        TaskPlan("p", "g", "Build the project", ""), PlannerSettings("Plan carefully.", 60, 0, 0),
        EMPTY_KNOWLEDGE, Harness("Work carefully.", Layer.BUILTIN))
