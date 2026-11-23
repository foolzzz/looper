"""Server-owned acceptance metadata kept with a Worker's attempt, never taken from its report."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import step_workspaces


@dataclass(frozen=True)
class AcceptanceBaseline:
    attempt: int
    head_sha: str | None

    def row(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "head_sha": self.head_sha}

    @classmethod
    def from_result(cls, result: Mapping[str, Any] | None, attempt: int) -> AcceptanceBaseline | None:
        value = (result or {}).get("acceptance_baseline")
        if not isinstance(value, Mapping) or type(value.get("attempt")) is not int or value["attempt"] != attempt:
            return None
        head = value.get("head_sha")
        if head is not None and not isinstance(head, str):
            return None
        return cls(attempt, head)


async def capture_baseline(work_dir: str, attempt: int, previous: Mapping[str, Any] | None, *,
                           same_attempt: bool, workspace_base: str | None = None) -> AcceptanceBaseline:
    if same_attempt:
        # A restarted Worker may already have edited or committed files. Its current HEAD is no baseline.
        return AcceptanceBaseline.from_result(previous, attempt) or AcceptanceBaseline(attempt, workspace_base)
    if workspace_base:
        return AcceptanceBaseline(attempt, workspace_base)
    repo = await step_workspaces.inspect(work_dir)
    return AcceptanceBaseline(attempt, repo.head if isinstance(repo, step_workspaces.Repo) else None)


def worker_result(result: Mapping[str, Any], previous: Mapping[str, Any] | None, attempt: int) -> dict[str, Any]:
    report = {key: value for key, value in result.items() if key != "acceptance_baseline"}
    if baseline := AcceptanceBaseline.from_result(previous, attempt):
        report["acceptance_baseline"] = baseline.row()
    return report
