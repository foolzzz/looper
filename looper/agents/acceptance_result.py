"""Code-written task evidence and channel counts, read from the same durable measurements."""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from looper.db import step_check_runs
from looper.db.step_check_runs import CheckState, ErrorKind
from .acceptance_gate import current_evidence
from .project_execution import AcceptanceChecksMode

logger = logging.getLogger(__name__)
SECTION_HEADING = "## Evidence"


class ResultState(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    STALE = "stale"
    NOT_RUN = "not run"

    @classmethod
    def of(cls, row: Mapping[str, Any] | None) -> ResultState:
        if row is None or row["state"] == CheckState.RUNNING:
            return cls.NOT_RUN
        if row["state"] == CheckState.PASSED and row.get("protected_changed"):
            return cls.FAILED
        return cls(row["state"])


@dataclass(frozen=True)
class CheckCounts:
    counts: Mapping[tuple[ResultState, bool], int] = field(default_factory=dict)
    # Notices read the exact serialized line. They never parse stored result text to recover counts.
    recorded_line: str | None = None

    def line(self) -> str:
        if self.recorded_line is not None:
            return self.recorded_line
        parts = []
        for state in ResultState:
            for required in (True, False):
                count = self.counts.get((state, required), 0)
                if state is ResultState.PASSED:
                    if not required:
                        continue
                    count += self.counts.get((state, False), 0)
                if count:
                    parts.append(f"{count} {'' if required else 'advisory '}{state.value}")
        return "Checks: " + ", ".join(parts) if parts else ""


@dataclass(frozen=True)
class ResultEvidence:
    section: str
    counts: CheckCounts

    def payload(self) -> dict[str, str]:
        return {"section": self.section, "line": self.counts.line()}

    @classmethod
    def from_payload(cls, value: Any) -> ResultEvidence | None:
        if not isinstance(value, Mapping):
            return None
        section, line = value.get("section"), value.get("line")
        if not isinstance(section, str) or not isinstance(line, str):
            return None
        return cls(section, CheckCounts(recorded_line=line))


def _work_steps(steps):
    return [step for step in steps if step.gate is None and step.wait is None and step.status != "skipped"]


async def summarize(task_id: str, steps: Sequence[Any]) -> ResultEvidence | None:
    work = _work_steps(steps)
    checked = {step.step_id: step for step in work if step.checks}
    if not checked:
        return None
    history = await step_check_runs.list_runs(task_id)
    current = current_evidence(row for row in history if row["step_id"] in checked
        and row["attempt"] == checked[row["step_id"]].retries + 1
        and row["check_id"] in {check.id for check in checked[row["step_id"]].checks})
    if not current:
        return None  # Saved definitions alone must not change a disabled project's result.
    rows = {(row["step_id"], row["check_id"]): row for row in current}
    counts: Counter[tuple[ResultState, bool]] = Counter()
    lines = [SECTION_HEADING]
    for step in checked.values():
        for check in step.checks:
            row = rows.get((step.step_id, check.id))
            state = ResultState.of(row)
            counts[state, check.required] += 1
            detail = state.value
            if state is ResultState.ERROR:
                detail += f" ({ErrorKind(row['error_kind']).value})"
            if row is not None and state is not ResultState.NOT_RUN:
                if not row.get("guarded") or not row.get("head_sha"):
                    detail += ", unguarded (report_only)"
                if row.get("head_sha"):
                    detail += f" at {row['head_sha'][:12]}"
            advisory = "" if check.required else ", advisory"
            lines.append(f"- {step.label} {check.id} (criterion {check.criterion}{advisory}): {detail}")
    uncovered = sum(len(step.acceptance_criteria) - len({check.criterion for check in step.checks}) for step in work)
    if uncovered:
        lines.append(f"{uncovered} criteria have no check.")
    return ResultEvidence("\n".join(lines), CheckCounts(counts))


async def for_plan(agent, plan) -> ResultEvidence | None:
    try:
        return await summarize(agent._task_id, plan.steps)
    except Exception:
        if (agent._execution.acceptance_checks is AcceptanceChecksMode.OFF
                or not any(step.checks for step in _work_steps(plan.steps))):
            return None
        logger.warning("Acceptance evidence summary unavailable", exc_info=True)
        await agent._activity("error", "Acceptance evidence summary unavailable")
        return ResultEvidence(SECTION_HEADING + "\nEvidence could not be read when the task ended; "
                              "see the step cards.", CheckCounts())
