"""The Planner's vault closeout contract and its best-effort boundaries, without a database."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.tiers import planner, planner_protocol as protocol
from looper.agents.tiers.journal import JournalEntry


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agent = planner.PlannerAgent(task_id="task-1", runtime_root=tmp_path, ops=object())
    agent._token = "planner.test"
    agent._read_execution = AsyncMock(return_value=SimpleNamespace(vault_on=True))
    agent._activity = AsyncMock()
    agent._ask = AsyncMock(return_value="kept the build quirk")
    config = SimpleNamespace(writable=("codex-mem",), rules_file="codex-mem/AGENTS.md",
                             folders=lambda: "codex-mem/")
    monkeypatch.setattr(planner.vault, "active", AsyncMock(return_value=config))
    monkeypatch.setattr(planner.vault, "rules_text", AsyncMock(return_value=("Keep one topic per note.", None)))
    agent._cap_reached = AsyncMock(return_value=None)
    plan = planner.TaskPlan(plan_id="plan-1", goal_id="goal-1", requirement="build", work_dir=str(tmp_path))
    plan.journal.append(JournalEntry("s1", "done", "build needs make gen first"))
    return agent, plan, config


def test_vault_reply_is_one_line_and_clipped() -> None:
    assert protocol.TurnKind.VAULT.value == "VAULT"
    assert protocol.parse_vault('```json\n{"vault": "kept a note\\nwith the fix"}\n```') == "kept a note with the fix"
    assert len(protocol.parse_vault("```json\n" + json.dumps({"vault": "x" * 400}) + "\n```")) == 300


@pytest.mark.parametrize("reply", [{}, {"vault": ""}, {"vault": None}, {"vault": "ok", "lessons": "no"}])
def test_vault_reply_rejects_missing_empty_and_extra_fields(reply) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_vault("```json\n" + json.dumps(reply) + "\n```")


def test_vault_message_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, plan, config = _agent(tmp_path, monkeypatch)
    text = agent._vault_message(plan, "completed", config, "Keep one topic per note.")
    assert text.splitlines()[0] == (
        "[VAULT] The task completed. Keep what later work should know in the operator's knowledge vault, "
        "following its rules below: search before you add, read a note before you change it (pass its sha256; "
        "null for a new note), and write only under codex-mem/ with looper_vault_write. Paths are relative "
        "to the vault; Looper commits each write. Write nothing when nothing will still hold. Do not change "
        "any other files.")
    assert "The vault's rules (codex-mem/AGENTS.md):\nKeep one topic per note." in text
    assert "build needs make gen first" in text
    assert text.endswith('```json\n{"vault": "one line: what you wrote, or why nothing"}\n```')
    config.rules_file = None
    fallback = agent._vault_message(plan, "failed: build broke", config, None)
    assert fallback.startswith("[VAULT] The task failed: build broke.")
    assert "The vault has no rules file: keep notes short, one topic each, and update a note rather than add a near-duplicate." in fallback


def test_vault_closeout_asks_once_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, plan, _ = _agent(tmp_path, monkeypatch)
    asyncio.run(agent._vault_closeout(plan, "completed"))
    agent._ask.assert_awaited_once()
    assert agent._ask.await_args.args[0] is protocol.TurnKind.VAULT
    agent._activity.assert_awaited_once_with("phase", "Vault closeout: kept the build quirk")


@pytest.mark.parametrize("ending", ["closeout", "fail_plan"])
@pytest.mark.parametrize("failed_turn", [False, True])
def test_repeated_endings_ask_for_vault_closeout_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str, failed_turn: bool,
) -> None:
    agent, plan, _ = _agent(tmp_path, monkeypatch)
    if failed_turn:
        agent._ask.side_effect = planner.PlannerTurnError("the vault turn failed")
    agent._bus = SimpleNamespace(publish=AsyncMock())
    agent._save = AsyncMock()
    monkeypatch.setattr(planner, "update_task_phase", AsyncMock())

    async def finish_twice() -> None:
        for _ in range(2):
            if ending == "fail_plan":
                await agent._fail_plan(plan, reason="build broke")
            else:
                await agent._vault_closeout(plan, "completed")

    asyncio.run(finish_twice())
    agent._ask.assert_awaited_once()
    assert agent._ask.await_args.args[0] is protocol.TurnKind.VAULT


def test_a_restarted_tasks_new_planner_gets_its_own_vault_closeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, plan, _ = _agent(tmp_path, monkeypatch)
    asyncio.run(first._vault_closeout(plan, "failed: build broke"))
    restarted, _, _ = _agent(tmp_path, monkeypatch)
    asyncio.run(restarted._vault_closeout(plan, "completed"))
    first._ask.assert_awaited_once()
    restarted._ask.assert_awaited_once()


@pytest.mark.parametrize("skip", ["no_ops", "no_token", "project_off", "not_configured", "read_only"])
def test_vault_closeout_skips_without_write_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       skip: str) -> None:
    agent, plan, config = _agent(tmp_path, monkeypatch)
    if skip == "no_ops":
        agent._ops = None
    elif skip == "no_token":
        agent._token = None
    elif skip == "project_off":
        agent._read_execution.return_value.vault_on = False
    elif skip == "not_configured":
        planner.vault.active.return_value = None
    else:
        config.writable = ()
    asyncio.run(agent._vault_closeout(plan, "completed"))
    agent._ask.assert_not_awaited()
    agent._activity.assert_not_awaited()


def test_vault_closeout_skips_an_empty_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, plan, _ = _agent(tmp_path, monkeypatch)
    plan.journal.clear()
    asyncio.run(agent._vault_closeout(plan, "completed"))
    agent._ask.assert_not_awaited()
    agent._activity.assert_awaited_once_with("phase", "No vault closeout: the task journal is empty")


def test_vault_closeout_skips_at_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, plan, _ = _agent(tmp_path, monkeypatch)
    agent._cap_reached.return_value = 2.5
    asyncio.run(agent._vault_closeout(plan, "completed"))
    agent._ask.assert_not_awaited()
    agent._activity.assert_awaited_once_with(
        "error", "Warning: no vault closeout: the task has spent its $2.5 cap (task_budget_usd)")


def test_vault_closeout_warns_and_uses_fallback_when_rules_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, plan, _ = _agent(tmp_path, monkeypatch)
    planner.vault.rules_text.return_value = (None, "rules missing")
    asyncio.run(agent._vault_closeout(plan, "completed"))
    assert "The vault has no rules file:" in agent._ask.await_args.args[1]
    assert agent._activity.await_args_list[0].args == ("error", "Warning: rules missing")


@pytest.mark.parametrize("failure", ["lookup", "turn", "unexpected"])
def test_vault_closeout_failure_is_only_a_clipped_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              failure: str) -> None:
    agent, plan, _ = _agent(tmp_path, monkeypatch)
    error = RuntimeError("x" * 700)
    if failure == "lookup":
        planner.vault.active.side_effect = error
    else:
        agent._ask.side_effect = planner.PlannerTurnError("x" * 700) if failure == "turn" else error
    asyncio.run(agent._vault_closeout(plan, "failed: build broke"))
    kind, summary = agent._activity.await_args.args
    assert kind == "error" and summary.startswith("Warning:") and len(summary) <= 500
