"""Read-only explorers: their context, role contract and checked evidence report.

Invalid reports retain bounded original text. A structural check verifies the
shape and local evidence paths, not whether a finding's claim is true.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from .agent_settings import AgentSettings
from .knowledge import AgentKnowledge
from .system_prompt import (SystemPrompt, boundaries_section, compose_system_prompt, is_recommended,
                            prompt_layers)

EXPLORER_TIMEOUT_SEC = 300
EXPLORER_MAX_TURNS = 40
EXPLORER_BUDGET_USD = .50
EXPLORER_TEXT_MAX = 12_000

EXPLORER_ROLE_PROMPT = """You are a read-only explorer for a Looper task. Investigate the brief within its
scope, read the local code and give concise findings backed by file evidence.
Do not change files, run shell commands, access the web or delegate work.
Separate observed facts from uncertainty; report unanswered questions."""

EXPLORER_CONTRACT = """## Explorer report (required; code checks its structure)
End your final message with one JSON object and nothing after it:
{"summary":"...","findings":[{"claim":"...","evidence":{"path":"relative/file","lines":[1,3]}}],"open_questions":[],"confidence":"high"}
- summary: non-empty text, at most 2,000 characters.
- findings: at most 40 findings, each with a non-empty claim (at most 2,000
  characters) and evidence. Evidence path is relative to the working directory
  and must exist inside it. Optional lines is [first, last], positive integers
  in ascending order, inclusive.
- open_questions: at most 20 non-empty strings, at most 1,000 characters each.
- confidence: high, medium or low. The check verifies shape and evidence paths,
  not the truth of a claim. Do not invent evidence."""


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def build_explorer_prompt(
    settings: AgentSettings, *, project: Mapping[str, Any] | None = None,
    task_id: str | None = None, requirement: str = "", step: Mapping[str, Any] | None = None,
    knowledge: AgentKnowledge | None = None, role_config: Mapping[str, Any] | None = None,
) -> SystemPrompt:
    """Compose an explorer's harness, project, task, role and final contract."""
    config = role_config or {}
    role = str(config.get("system_prompt") or EXPLORER_ROLE_PROMPT).strip()
    known = knowledge or AgentKnowledge()
    task = "## Task\n" + requirement[:32_000]
    if step:
        task += f"\nParent step: {step.get('id', '')}\n{str(step.get('instruction') or '')[:32_000]}"
    custom = settings.profile.custom_prompt
    text = compose_system_prompt(
        "", harness=settings.harness.text, custom_prompt=custom,
        project_name=str((project or {}).get("name") or ""),
        background=str((project or {}).get("background") or ""), knowledge=known.prompt_section,
        task=f"{task}\n\n{role}\n\n{boundaries_section(settings.boundaries.text)}",
        contract=EXPLORER_CONTRACT,
    )
    layers = prompt_layers(
        role_id="explorer", role=role,
        recommended=is_recommended(role, str(config.get("default_prompt") or EXPLORER_ROLE_PROMPT)),
        contract_name="EXPLORER_CONTRACT", contract=EXPLORER_CONTRACT,
        project=project, task_kind="explore", task_id=task_id,
        step_id=str(step.get("id") or "") if step else None,
        task=task, harness=settings.harness, knowledge=known, custom_prompt=custom,
    )
    layers["boundaries"] = settings.boundaries.record()
    return SystemPrompt(text, layers)


def _last_object(text: str) -> Any:
    decoder = json.JSONDecoder()
    found = None
    start = text.find("{")
    while start != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict) and text[end:].strip() in ("", "```"):
            found = value
        start = text.find("{", end)
    return found


def _text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _valid_report(value: Any, cwd: Path) -> bool:
    if not isinstance(value, dict) or set(value) != {"summary", "findings", "open_questions", "confidence"}:
        return False
    if not _text(value["summary"], 2_000) or value["confidence"] not in tuple(c.value for c in Confidence):
        return False
    questions, findings = value["open_questions"], value["findings"]
    if not isinstance(questions, list) or len(questions) > 20 or not all(_text(q, 1_000) for q in questions):
        return False
    if not isinstance(findings, list) or len(findings) > 40:
        return False
    root = cwd.resolve()
    for item in findings:
        if not isinstance(item, dict) or set(item) != {"claim", "evidence"} or not _text(item["claim"], 2_000):
            return False
        evidence = item["evidence"]
        if not isinstance(evidence, dict) or set(evidence) - {"path", "lines"} or not _text(evidence.get("path"), 1_000):
            return False
        path = Path(evidence["path"])
        if path.is_absolute():
            return False
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root) or not resolved.exists():
            return False
        if "lines" in evidence:
            lines = evidence["lines"]
            if (not isinstance(lines, list) or len(lines) != 2 or
                    not all(type(line) is int for line in lines) or not 1 <= lines[0] <= lines[1]):
                return False
    return True


def parse_explorer_result(output: str, cwd: Path) -> dict[str, Any]:
    """Check the final report; preserve original text with an unverified marker on failure."""
    try:
        value = _last_object(output)
        if _valid_report(value, cwd):
            return {"verified": True, **value}
    except (OSError, ValueError, RuntimeError, RecursionError):
        # Broken paths, symlink loops and malformed or excessively nested JSON
        # are unverified output, not a reason to lose the explorer's response.
        pass
    return {"verified": False, "summary": "未通过结构检查", "text": output[:EXPLORER_TEXT_MAX]}
