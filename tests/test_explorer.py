"""The explorer's bounded report, evidence checks and independent role defaults."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest


def _report(**changes):
    return {"summary": "The parser checks paths.",
            "findings": [{"claim": "Paths are checked.", "evidence": {"path": "parser.py", "lines": [1, 3]}}],
            "open_questions": [], "confidence": "high", **changes}


def test_explorer_parses_the_last_complete_report(tmp_path: Path) -> None:
    from looper.agents.explorer import parse_explorer_result

    (tmp_path / "parser.py").write_text("one\ntwo\nthree\n")
    result = parse_explorer_result('An earlier example: {}\n```json\n' + json.dumps(_report()) + '\n```', tmp_path)
    assert result == {"verified": True, **_report()}


@pytest.mark.parametrize("changes", [
    {"summary": "x" * 2001}, {"confidence": "certain"}, {"open_questions": "unknown"},
    {"findings": [{"claim": "missing", "evidence": {"path": "missing.py"}}]},
    {"findings": [{"claim": "escape", "evidence": {"path": "../outside.py"}}]},
    {"findings": [{"claim": "bad line", "evidence": {"path": "parser.py", "lines": [3, 1]}}]},
    {"findings": [{"claim": "bad line", "evidence": {"path": "parser.py", "lines": [True, 3]}}]},
])
def test_unverified_report_keeps_original_text(tmp_path: Path, changes: dict) -> None:
    from looper.agents.explorer import parse_explorer_result

    (tmp_path / "parser.py").write_text("one\ntwo\nthree\n")
    output = json.dumps(_report(**changes))
    result = parse_explorer_result(output, tmp_path)
    assert result["verified"] is False
    assert result["text"] == output
    assert "未通过结构检查" in result["summary"]


def test_symlink_evidence_must_stay_in_working_directory(tmp_path: Path) -> None:
    from looper.agents.explorer import parse_explorer_result

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside")
    (repo / "parser.py").symlink_to(outside)
    assert parse_explorer_result(json.dumps(_report()), repo)["verified"] is False


def test_malformed_result_is_clipped_and_not_repaired(tmp_path: Path) -> None:
    from looper.agents.explorer import EXPLORER_TEXT_MAX, parse_explorer_result

    result = parse_explorer_result("broken " * EXPLORER_TEXT_MAX, tmp_path)
    assert result["verified"] is False
    assert len(result["text"]) <= EXPLORER_TEXT_MAX
    assert parse_explorer_result('{"summary": "old"}\n{"summary":', tmp_path)["verified"] is False


def test_explorer_has_own_profile_budget_and_inherits_harness() -> None:
    from looper.agents.agent_settings import AgentType, Layer, LayerValues, resolve
    from looper.agents.profile_registry import BUILTIN_PROFILE

    role = AgentType.EXPLORER
    settings = resolve(role, {(Layer.SYSTEM, None): LayerValues(
        harness_prompt="Shared harness", profile=BUILTIN_PROFILE, run_budget_usd=50)})
    assert settings.profile.name == "claude-code/sonnet-5-5"
    assert settings.max_budget_usd == .50
    assert settings.harness.text == "Shared harness"
    changed = resolve(role, {(Layer.ROLE, role): LayerValues(profile=BUILTIN_PROFILE, run_budget_usd=.30)})
    assert changed.profile is BUILTIN_PROFILE
    assert changed.max_budget_usd == .30


def test_explorer_profile_selection_refuses_unenforced_read_only() -> None:
    from looper.agents.profile_registry import BUILTIN_PROFILE, RegistryError, RuntimeSpec, check_roles
    from looper.agents.runtimes import Capability

    profile = replace(BUILTIN_PROFILE, runtime=RuntimeSpec("rt-cursor", "cursor", "cursor-agent", "",
                                                         frozenset({Capability.RUN})))
    with pytest.raises(RegistryError, match="read-only"):
        check_roles(profile, ["explorer"])


def test_explorer_role_seed_preserves_existing_role_definitions(monkeypatch) -> None:
    from looper.agents.explorer import EXPLORER_ROLE_PROMPT
    from looper.agents.serve import seed_role_configs
    from looper.db import role_configs

    seen = []

    async def capture(defaults, **kwargs):
        seen.extend(defaults)

    monkeypatch.setattr(role_configs, "seed_defaults", capture)
    asyncio.run(seed_role_configs())
    explorer = next(row for row in seen if row["role"] == "explorer")
    assert explorer["system_prompt"] == EXPLORER_ROLE_PROMPT
    assert explorer["timeout_seconds"] == 300
    assert explorer["max_turns"] == 40


def test_explorer_prompt_has_context_and_contract_last() -> None:
    from looper.agents.agent_settings import AgentType, resolve
    from looper.agents.explorer import EXPLORER_CONTRACT, EXPLORER_ROLE_PROMPT, build_explorer_prompt

    prompt = build_explorer_prompt(resolve(AgentType.EXPLORER, {}),
                                  project={"id": "project-example", "name": "Example", "background": "Project facts"},
                                  task_id="task-example", requirement="Check the parser",
                                  step={"id": "step-one", "instruction": "Read parsing"})
    assert prompt.text.endswith(EXPLORER_CONTRACT)
    assert prompt.text.index("Project facts") < prompt.text.index("Check the parser")
    assert prompt.text.index("Check the parser") < prompt.text.index(EXPLORER_ROLE_PROMPT)
    assert "step-one" in prompt.text and "Read parsing" in prompt.text
    assert prompt.layers["task"]["step_id"] == "step-one"
    assert prompt.layers["contract"]["name"] == "EXPLORER_CONTRACT"
