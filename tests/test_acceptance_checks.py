"""Typed acceptance checks at the Planner protocol boundary; nothing executes them."""
import json

import pytest

from looper.agents.project_execution import AcceptanceChecksMode as Mode, ProjectExecution
from looper.agents.tiers.planner_protocol import (
    DecisionContext, PlanView, ProtocolError, ResultKind, parse_chat, parse_decision, parse_plan, reply_contract,
)


def fenced(value):
    return "```json\n" + json.dumps(value) + "\n```"


def step(checks=None):
    value = {"id": "s1", "instruction": "Build a report", "acceptance_criteria": ["report works", "empty state"],
             "depends_on": [], "review": True}
    if checks is not None:
        value["checks"] = checks
    return value


def check(**fields):
    return {"id": "c1", "criterion": 1, "type": "command", "command": "  printf '$VALUE'\n", **fields}


def plan(checks, mode=Mode.LOCAL):
    return parse_plan(fenced({"steps": [step(checks)]}), acceptance_checks=mode).steps[0]


def test_typed_defaults_preserve_command_bytes_and_artifact_shape():
    command, artifact = plan([check(), check(id="c2", type="artifact", artifact=["out/empty.png"],
                                           required=False, protect=["tests/report.py"], timeout_seconds=1800)]).checks
    assert command.command == "  printf '$VALUE'\n"
    assert command.criterion == 1 and command.type.value == "command"
    assert command.required is True and command.network is False and command.timeout_seconds == 300
    assert command.protect == ()
    assert artifact.artifact == ("out/empty.png",) and artifact.required is False
    assert artifact.row()["artifact"] == ["out/empty.png"]
    assert "artifact" not in command.row()
    assert len(plan([check(id=f"c{i}") for i in range(12)]).checks) == 12
    assert plan([check(command="x" * 2000, timeout_seconds=1)]).checks[0].timeout_seconds == 1


@pytest.mark.parametrize("changes,match", [
    ({"id": ""}, "id"), ({"id": 2}, "id"), ({"criterion": True}, "criterion"),
    ({"criterion": 0}, "criterion"), ({"criterion": 3}, "criterion"), ({"criterion": 1.5}, "criterion"),
    ({"type": "http"}, "type"), ({"command": " "}, "command"), ({"command": 1}, "command"),
    ({"command": "x" * 2001}, "2000"), ({"timeout_seconds": 0}, "timeout_seconds"),
    ({"timeout_seconds": 1801}, "timeout_seconds"), ({"timeout_seconds": True}, "timeout_seconds"),
    ({"timeout_seconds": 1.5}, "timeout_seconds"), ({"required": 1}, "required"),
    ({"network": "false"}, "network"), ({"network": True}, "network"),
    ({"unknown": True}, "unknown"), ({"artifact": []}, "artifact"),
    ({"protect": ["/tmp/test"]}, "protect"), ({"protect": ["test/../file"]}, "protect"),
    ({"protect": "test"}, "protect"), ({"protect": [""]}, "protect"),
    ({"type": "artifact"}, "artifact"), ({"type": "artifact", "artifact": []}, "artifact"),
    ({"type": "artifact", "artifact": ["../out"]}, "artifact"),
    ({"type": "artifact", "artifact": ["/out"]}, "artifact"),
    ({"type": "artifact", "artifact": ["a", "b", "c", "d"]}, "3"),
    ({"type": "artifact", "artifact": [1]}, "artifact"),
])
def test_invalid_check_is_a_repairable_protocol_error(changes, match):
    with pytest.raises(ProtocolError, match=match):
        plan([check(**changes)])


@pytest.mark.parametrize("missing", ["id", "criterion", "type", "command"])
def test_missing_required_check_key(missing):
    value = check()
    del value[missing]
    with pytest.raises(ProtocolError, match=missing):
        plan([value])


@pytest.mark.parametrize("checks", [{}, None, [None], [check(), check()], [check(id=f"c{i}") for i in range(13)]])
def test_invalid_check_list(checks):
    value = step([])
    value["checks"] = checks
    with pytest.raises(ProtocolError, match="checks"):
        parse_plan(fenced({"steps": [value]}), acceptance_checks=Mode.LOCAL)


def test_off_rejects_even_empty_checks_and_names_project_settings():
    with pytest.raises(ProtocolError, match="acceptance_checks.*off.*Settings"):
        plan([], Mode.OFF)
    assert ProjectExecution.from_row(None).acceptance_checks is Mode.OFF
    assert not parse_plan(fenced({"steps": [step()]})).steps[0].checks


@pytest.mark.parametrize("kind", ["gate", "wait", "review"])
def test_checks_only_belong_to_work_steps(kind):
    with pytest.raises(ProtocolError, match="checks.*work"):
        parse_plan(fenced({"steps": [{**step([check()]), "kind": kind}]}), acceptance_checks=Mode.LOCAL)


def test_a_step_without_criteria_cannot_have_a_check():
    with pytest.raises(ProtocolError, match="criterion"):
        parse_plan(fenced({"steps": [{**step([check()]), "acceptance_criteria": []}]}),
                   acceptance_checks=Mode.LOCAL)


def test_network_requires_both_project_and_check_permission():
    assert plan([check(network=True)], Mode.NETWORK).checks[0].network is True
    assert plan([check()], Mode.NETWORK).checks[0].network is False


@pytest.mark.parametrize("action", ["add_steps", "replan"])
def test_chat_and_decision_plan_changes_share_the_check_validator(action):
    view = PlanView(started={}, pending={"old": ()}, acceptance_checks=Mode.LOCAL)
    reply = parse_chat(fenced({"reply": "Updated", "action": action, "steps": [step([check()])]}), view)
    assert reply.steps[0].checks[0].id == "c1"
    context = DecisionContext(step="old", result=ResultKind.WORK, succeeded=True, review_required=False, retries=0)
    reply = parse_decision(fenced({"decision": "replan", "steps": [step([check()])]}), context, view)
    assert reply.steps[0].checks[0].criterion == 1


def test_enabled_contract_adds_only_the_rfc_section():
    from looper.agents.acceptance_checks import PROTOCOL_SECTION

    base = reply_contract()
    assert reply_contract(acceptance_checks=Mode.OFF) == base
    assert reply_contract(acceptance_checks=Mode.LOCAL) == base + PROTOCOL_SECTION
    assert reply_contract(acceptance_checks=Mode.NETWORK) == base + PROTOCOL_SECTION
    assert "一个标准能用命令量的，写成检查，绑到它的序号上" in PROTOCOL_SECTION


def test_project_permission_reaches_real_planner_entry_points(tmp_path):
    from looper.agents.tiers.planner import PlannerAgent

    agent = PlannerAgent(task_id="task-checks", runtime_root=tmp_path)
    text = fenced({"steps": [step([check()])]})
    with pytest.raises(ProtocolError, match="acceptance_checks.*off"):
        agent._plan_parser()(text)
    agent._execution = ProjectExecution.from_row({"acceptance_checks": "local"})
    assert agent._plan_parser()(text).steps[0].checks[0].id == "c1"
    view = agent._checked(PlanView(started={}, pending={}))
    assert parse_chat(fenced({"reply": "Added", "action": "add_steps", "steps": [step([check()])]}),
                      view).steps[0].checks[0].id == "c1"


def test_enabled_contract_example_and_paragraph_are_verbatim_from_rfc():
    from pathlib import Path
    from looper.agents.acceptance_checks import PROTOCOL_SECTION

    rfc = (Path(__file__).resolve().parents[1] / "docs/architecture/rfcs/executable-acceptance-v0.zh-CN.md").read_text()
    section = rfc.split("## 6. ", 1)[1].split("## 7. ", 1)[0]
    example = "```json" + section.split("```json", 1)[1].split("```", 1)[0] + "```"
    paragraph = next(line.removeprefix("> ") for line in section.splitlines() if line.startswith("> "))
    assert PROTOCOL_SECTION == "\n\n" + example + "\n\n" + paragraph + "\n"
