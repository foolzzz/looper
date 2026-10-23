"""Task templates: typed inputs, their checks, patterns kept off the event loop, and the rendered requirement."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from looper.agents import task_templates as tt
from looper.agents.system_prompt import TASK_REQUIREMENT_LIMIT
from looper.agents.task_templates import (
    InputKind, PatternTimeout, TaskTemplate, TemplateError, TemplateInput,
    check_patterns, check_requirement, check_template_fields, check_values, match_patterns, parse_inputs,
    render_requirement,
)


def run(coro):
    return asyncio.run(coro)


def item(**kw):
    return {"key": "pr_url", "label": "PR", **kw}


def test_parse_inputs_round_trip():
    got = parse_inputs([item(kind="url", required=False, pattern="x+", hint="h"), {"key": "n", "label": " N "}])
    assert got == (TemplateInput("pr_url", "PR", InputKind.URL, False, "x+", "h"),
                   TemplateInput("n", "N", InputKind.TEXT, True, "", ""))


@pytest.mark.parametrize("raw,field", [
    ("[]", "inputs must be a list"),
    ([item(key=f"k{i}") for i in range(11)], "at most"),
    ([item(), item()], "pr_url"),
    ([item(key="Bad")], "key"),
    ([item(label="  ")], "label"),
    ([item(kind="file")], "kind"),
    ([item(extra=1)], "extra"),
    ([item(pattern="a" * 301)], "pattern"),
    ([item(hint="a" * 201)], "hint"),
])
def test_parse_inputs_refuses(raw, field):
    with pytest.raises(TemplateError, match=field):
        parse_inputs(raw)


def test_check_patterns():
    with pytest.raises(TemplateError, match="input pr_url: pattern does not compile"):
        run(check_patterns(parse_inputs([item(pattern="(")])))
    start = time.monotonic()
    with pytest.raises(TemplateError, match="too slow"):
        run(check_patterns(parse_inputs([item(pattern="(a+)+$")])))
    assert time.monotonic() - start < tt.PATTERN_TIMEOUT_SEC + 1
    run(check_patterns(parse_inputs([item(pattern=r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")])))


def test_match_patterns_does_not_block_loop():
    async def main():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        with pytest.raises(PatternTimeout) as exc:
            await match_patterns([("(a+)+$", "a" * 40 + "!")], timeout=1.0)
        stop = True
        await task
        return ticks, exc.value.index

    ticks, index = run(main())
    assert ticks >= 50 and index == 0


def test_a_cancelled_match_kills_its_subprocess(monkeypatch):
    started = []
    create = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await create(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    async def main():
        task = asyncio.create_task(match_patterns([("(a+)+$", "a" * 40 + "!")], timeout=30.0))
        while not started:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return started[0].returncode

    assert run(main()) is not None


def test_match_patterns_answers_and_empty():
    assert run(match_patterns([])) == []
    got = run(match_patterns([("a+", "aa"), ("a+", "b"), ("(", "x")]))
    assert got[:2] == [True, False] and isinstance(got[2], str)


def test_check_requirement_ok():
    check_requirement("Review {{pr_url}}", parse_inputs([item()]))


@pytest.mark.parametrize("text", ["{{nope}}", "{{pr_url", "{{ pr_url }}", "pr_url}}"])
def test_check_requirement_refused(text):
    with pytest.raises(TemplateError):
        check_requirement(text, parse_inputs([item()]))


def template(inputs, requirement="", **kw):
    return TaskTemplate("m", "k", "Tpl", requirement=requirement, inputs=parse_inputs(inputs), **kw)


@pytest.mark.parametrize("inputs,raw,match", [
    ([item(hint="the URL")], {}, r"PR \(the URL\).*ask the user"),
    ([item()], {"pr_url": "  "}, "ask the user"),
    ([item()], {"zzz": "x"}, "no input 'zzz'"),
    ([item()], {"pr_url": 3}, "strings"),
    ([item(kind="url")], {"pr_url": "ftp://x"}, "URL"),
    ([item(kind="url")], {"pr_url": "github.com/a/b/pull/1"}, "URL"),
    ([item(pattern=r"\d+", hint="digits")], {"pr_url": "abc"}, "must look like digits"),
    ([item(pattern=r"(a+)+$")], {"pr_url": "a" * 40 + "!"}, "took too long"),
])
def test_check_values_refused(inputs, raw, match):
    with pytest.raises(TemplateError, match=match):
        run(check_values(template(inputs), raw))


def test_check_values_ok():
    t = template([item(), {"key": "opt", "label": "Opt", "required": False}])
    assert run(check_values(t, {"pr_url": "  x  "})) == {"pr_url": "x"}
    assert run(check_values(t, {"pr_url": "x", "opt": "y"})) == {"pr_url": "x", "opt": "y"}
    assert run(check_values(template([]), None)) == {}


def test_render_requirement():
    t = template([item()], "Review {{pr_url}}")
    assert render_requirement(t, {"pr_url": "u"}, " go ") == "Review u\n\ngo"
    assert render_requirement(template([]), {}, " go ") == "go"
    t2 = template([{"key": "first", "label": "F"}, {"key": "second", "label": "S"}], "A {{first}} B {{second}}")
    assert render_requirement(t2, {"first": "{{second}}", "second": "X"}, "") == "A {{second}} B X"
    with pytest.raises(TemplateError, match="shorten"):
        render_requirement(template([]), {}, "x" * (TASK_REQUIREMENT_LIMIT + 1))


def test_check_template_fields():
    check_template_fields(name="n", project_id="p", role_templates=("security", "qa"))
    with pytest.raises(TemplateError, match="security.*twice"):
        check_template_fields(name="n", project_id="p", role_templates=("security", "security"))
    with pytest.raises(TemplateError, match="set project_id"):
        check_template_fields(name="n", role_templates=("qa",))
    with pytest.raises(TemplateError):
        check_template_fields(name="a\nb")
    with pytest.raises(TemplateError):
        check_template_fields(name="n", workspace_mode="x")


def test_from_row_and_snapshot():
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {"manager_id": "m", "key": "k", "name": "Tpl", "description": "", "project_id": "p", "work_dir": "",
           "workspace_mode": None, "requirement": "R {{pr_url}}", "planner_instructions": "pi",
           "planner_profile_id": None, "worker_profile_id": "w1", "reviewer_profile_id": None,
           "role_templates": ["qa"], "inputs": [{"key": "pr_url", "label": "PR", "kind": "url", "required": True,
                                                 "pattern": "", "hint": ""}], "updated_at": when}
    t = TaskTemplate.from_row(row)
    assert dict(t.profiles) == {"worker": "w1"} and t.role_templates == ("qa",)
    assert t.inputs[0].kind is InputKind.URL
    roles = [{"role_key": "qa", "kind": "x", "profile_id": None, "role_prompt": "", "harness": "", "items": [],
              "template_key": "qa"}]
    assert t.snapshot({"pr_url": "u"}, roles) == {
        "key": "k", "name": "Tpl", "updated_at": when.isoformat(),
        "inputs": [{"key": "pr_url", "label": "PR", "value": "u"}],
        "planner_instructions": "pi", "roles": roles}
    assert t.snapshot({})["inputs"] == [] and t.snapshot({})["roles"] == []
