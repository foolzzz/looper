"""Pure selection and acceptance rules for code-measured step evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from looper.db.step_check_runs import CheckState
from .acceptance_checks import Check

Version = tuple[str | None, str | None]


def same_version(measured: Version, current: Version) -> bool:
    """Whether two measurements describe the same source. The content hash covers every tracked and untracked file,
    so committing it (a review freeze) keeps the version; HEAD decides only without a content hash."""
    if measured[1] is not None and current[1] is not None:
        return measured[1] == current[1]
    return measured == current


@dataclass(frozen=True)
class Gate:
    passed: bool
    failed: tuple[str, ...] = ()
    errored: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    rechecks_used: int = 0
    stale_reruns_used: int = 0
    current: tuple[Mapping[str, Any], ...] = ()
    head: Version = (None, None)


def current_evidence(runs: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Select the last run in the latest attempt for each (step, check)."""
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in runs:
        key = (row["step_id"], row["check_id"])
        previous = latest.get(key)
        if previous is None or (row["attempt"], row["run"]) > (previous["attempt"], previous["run"]):
            latest[key] = row
    return tuple(latest.values())


def gate(checks: Sequence[Check], current: Iterable[Mapping[str, Any]], head: Version) -> Gate:
    """A step passes exactly when all required checks passed on its current version.

    ``current`` belongs to one step. Counters are supplied by the caller from
    durable orchestration state, rather than guessed from the number of runs.
    """
    rows = tuple(current)
    latest = {row["check_id"]: row for row in rows}
    failed, errored, stale = [], [], []
    for check in checks:
        if not check.required:
            continue
        row = latest.get(check.id)
        if row is None:
            failed.append(check.id)
            continue
        state = row["state"]
        measured = (row.get("head_sha"), row.get("content_sha"))
        matches = same_version(measured, head)
        if state == CheckState.ERROR:
            errored.append(check.id)
        elif state == CheckState.STALE or not matches:
            stale.append(check.id)
        elif state != CheckState.PASSED or row.get("protected_changed"):
            failed.append(check.id)
    return Gate(not (failed or errored or stale), tuple(failed), tuple(errored), tuple(stale),
                current=rows, head=head)
