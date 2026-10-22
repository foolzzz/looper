"""The Planner's task section shows the task template its task came from, and only then."""

from __future__ import annotations

from looper.agents.system_prompt import plan_task_section

BASE = dict(task_id="t1", requirement="Review it.", project_name="P", work_dir="/w")
TEMPLATE = {"name": "PR review",
            "inputs": [{"key": "pr_url", "label": "Pull request link", "value": "https://github.com/o/r/pull/7"}],
            "planner_instructions": "Read the diff with gh."}


def test_no_template_is_todays_text() -> None:
    assert plan_task_section(**BASE, template=None) == plan_task_section(**BASE)
    assert plan_task_section(**BASE, template={"name": " "}) == plan_task_section(**BASE)


def test_the_template_lines_come_in_order_before_the_requirement() -> None:
    lines = plan_task_section(**BASE, template=TEMPLATE).split("\n")
    wanted = ["Template: PR review (set by the operator for this kind of task)", "Template inputs:",
              "- Pull request link: https://github.com/o/r/pull/7",
              "Template instructions (follow them; they are the operator's):", "Read the diff with gh."]
    start = lines.index(wanted[0])
    assert lines[start:start + 5] == wanted and lines[start + 5] == "Requirement:"


def test_without_instructions_or_inputs_those_lines_are_left_out() -> None:
    text = plan_task_section(**BASE, template={**TEMPLATE, "planner_instructions": "  "})
    assert "Template instructions" not in text and "Template inputs:" in text
    text = plan_task_section(**BASE, template={"name": "PR review", "inputs": []})
    assert "Template: PR review" in text and "Template inputs:" not in text
