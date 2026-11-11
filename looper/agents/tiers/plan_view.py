"""Render a full plan or the changes since its last successful presentation."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


PLAN_FULL_EVERY_TURNS = 20


@dataclass(frozen=True)
class PlanItem:
    step_id: str
    label: str
    state: str
    text: str


@dataclass(frozen=True)
class PlanShown:
    items: Mapping[str, str]
    progress: tuple[str, ...]
    turns_since_full: int


@dataclass(frozen=True)
class PlanViewResult:
    lines: list[str]
    shown: PlanShown | None
    mode: str
    reason: str


def render(items: Sequence[PlanItem], progress: Sequence[str], shown: PlanShown | None,
           *, full_reason: str | None, max_chars: int | None = None) -> PlanViewResult:
    """Build a view and stage its baseline; the caller commits it after success."""
    current = {item.step_id: item.text for item in items}
    current_progress = tuple(progress)
    full = ["Plan:", *(item.text for item in items), *(["", *progress] if progress else [])]
    reason = full_reason
    if reason is None and shown is None:
        reason = "first view"
    if reason is None and shown.turns_since_full >= PLAN_FULL_EVERY_TURNS:
        reason = "periodic"

    changes: list[str] = []
    if reason is None:
        assert shown is not None
        groups: dict[str, list[str]] = {}
        for item in items:
            groups.setdefault(item.state, []).append(item.label)
        changes = [
            f"Plan (changes since the full plan you were given {shown.turns_since_full + 1} turn(s) ago; "
            'a step not listed under "Changed or new" is as it was then):',
            "Steps by status: " + "; ".join(f"{state}: {', '.join(labels)}" for state, labels in groups.items()),
        ]
        changed = [item.text for item in items if shown.items.get(item.step_id) != item.text]
        changes.extend(["Changed or new:", *changed] if changed else ["No step changed."])
        removed = [step_id for step_id in shown.items if step_id not in current]
        if removed:
            changes.append("Removed: " + ", ".join(removed))
        if current_progress != shown.progress:
            changes.extend(["", *progress])
        elif progress:
            changes.extend(["", "Progress and findings: unchanged since then."])
        if len("\n".join(changes)) > len("\n".join(full)) / 2:
            reason = "large change"

    if reason is not None:
        lines, mode, turns = full, "full", 0
    else:
        assert shown is not None
        lines, mode, turns, reason = changes, "changes", shown.turns_since_full + 1, "changes"
    staged = PlanShown(current, current_progress, turns)
    if max_chars is not None:
        text = "\n".join(lines)
        if len(text) > max_chars:
            return PlanViewResult([text[:max_chars - 1] + "…"], None, mode, "clipped")
    return PlanViewResult(lines, staged, mode, reason)
