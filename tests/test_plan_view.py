"""Full and incremental plan presentations keep every step's current status."""
from dataclasses import replace

import pytest

from looper.agents.tiers.plan_view import PLAN_FULL_EVERY_TURNS, PlanItem, render


def items() -> list[PlanItem]:
    states = ["verified", "pending", "gate: pending", "wait pending", "pending"]
    return [PlanItem(f"step-{label}", label, state,
                     f"- {label} [{state}] " + "Detailed instructions. " * 30
                     + ("\n  summary: Completed and checked." if state == "verified" else ""))
            for label, state in zip("abcde", states)]


PROGRESS = ["Progress and findings:", "- The dependency was checked."]
CHANGE_HEADER = ('Plan (changes since the previous turn\'s message; steps not listed in full are unchanged '
                 'since then, with current status below):')


def test_first_view_keeps_full_lines_and_progress() -> None:
    plan = items()
    result = render(plan, PROGRESS, None, full_reason=None)
    expected = "Plan:\n" + "\n".join(item.text for item in plan) + "\n\n" + "\n".join(PROGRESS)
    assert "\n".join(result.lines) == expected
    assert result.mode == "full" and result.reason == "first view"
    assert result.shown.items == {item.step_id: item.text for item in plan}
    assert result.shown.progress == tuple(PROGRESS)
    assert result.shown.turns_since_full == 0


def test_change_lists_all_statuses_but_only_changed_item_and_removed_id() -> None:
    plan = items()
    shown = render(plan, PROGRESS, None, full_reason=None).shown
    changed = replace(plan[1], state="dispatched", text=plan[1].text.replace("pending", "dispatched"))
    result = render([plan[0], changed, *plan[2:4]], PROGRESS, shown, full_reason=None)
    assert result.mode == "changes" and result.reason == "changes"
    assert result.lines == [
        CHANGE_HEADER,
        "Steps by status: verified: a; dispatched: b; gate: pending: c; wait pending: d",
        "Changed or new:", changed.text, "Removed: step-e",
    ]
    assert result.shown.turns_since_full == 1
    assert result.shown.items[changed.step_id] == changed.text
    assert "step-e" not in result.shown.items


def test_groups_preserve_first_state_and_label_order() -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    result = render(plan, [], shown, full_reason=None)
    assert result.lines[1] == "Steps by status: verified: a; pending: b, e; gate: pending: c; wait pending: d"
    assert result.lines[-1] == "No step changed."
    assert result.shown.progress == ()


def test_large_change_sends_full_view() -> None:
    plan = items()
    shown = render(plan, PROGRESS, None, full_reason=None).shown
    changed = [replace(item, text=item.text + " Replanned.") for item in plan]
    result = render(changed, PROGRESS, shown, full_reason=None)
    assert result.mode == "full" and result.reason == "large change"
    assert result.lines == ["Plan:", *(item.text for item in changed), "", *PROGRESS]
    assert result.shown.turns_since_full == 0


@pytest.mark.parametrize("reason", ["new session", "new process", "no compaction events"])
def test_explicit_full_reason_takes_precedence(reason: str) -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    result = render(plan, [], shown, full_reason=reason)
    assert result.mode == "full" and result.reason == reason
    assert render(plan, [], None, full_reason=reason).reason == reason


def test_twenty_change_views_are_followed_by_a_full_view() -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    for n in range(1, PLAN_FULL_EVERY_TURNS + 1):
        result = render(plan, [], shown, full_reason=None)
        assert result.mode == "changes"
        assert result.lines[0] == CHANGE_HEADER
        shown = result.shown
    result = render(plan, [], shown, full_reason=None)
    assert result.mode == "full" and result.reason == "periodic"
    assert result.shown.turns_since_full == 0


def test_new_and_reused_labels_are_compared_by_id() -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    replacement = replace(plan[1], step_id="replacement")
    result = render([plan[0], replacement, *plan[2:]], [], shown, full_reason=None)
    assert result.mode == "changes"
    assert result.lines[2:4] == ["Changed or new:", replacement.text]
    assert "Removed: step-b" in result.lines


def test_same_id_with_new_summary_is_changed() -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    updated = replace(plan[0], text=plan[0].text + "\n  summary: Additional evidence.")
    result = render([updated, *plan[1:]], [], shown, full_reason=None)
    assert result.mode == "changes"
    assert result.lines[2:] == ["Changed or new:", updated.text]


def test_only_new_progress_entries_are_sent_and_remembered() -> None:
    plan = items()
    shown = render(plan, PROGRESS, None, full_reason=None).shown
    progress = [PROGRESS[0], "- The next dependency is ready.", PROGRESS[1]]
    result = render(plan, progress, shown, full_reason=None)
    assert result.mode == "changes"
    assert result.lines[2:] == ["No step changed.", "", "New progress:", progress[1]]
    assert result.shown.progress == tuple(progress)
    cleared = render(plan, [], result.shown, full_reason=None)
    assert cleared.lines[2:] == ["No step changed."]
    assert cleared.shown.progress == ()
    assert render(plan, [], cleared.shown, full_reason=None).lines[-1] == "No step changed."


@pytest.mark.parametrize("initial", [True, False])
def test_clipped_view_does_not_establish_a_baseline(initial: bool) -> None:
    plan = items()
    shown = None if initial else render(plan, PROGRESS, None, full_reason=None).shown
    complete = render(plan, PROGRESS, shown, full_reason=None)
    result = render(plan, PROGRESS, shown, full_reason=None, max_chars=100)
    assert "\n".join(result.lines) == "\n".join(complete.lines)[:99] + "…"
    assert result.shown is None
    assert result.reason == "clipped" and result.mode == complete.mode


def test_exact_character_budget_preserves_baseline() -> None:
    plan = items()
    complete = render(plan, PROGRESS, None, full_reason=None)
    result = render(plan, PROGRESS, None, full_reason=None, max_chars=len("\n".join(complete.lines)))
    assert result == complete


@pytest.mark.parametrize(("index", "state"), [(2, "gate: passed"), (3, "wait ready")])
def test_gate_and_wait_transitions_change_only_their_item(index: int, state: str) -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    updated = replace(plan[index], state=state, text=plan[index].text.replace(plan[index].state, state))
    plan[index] = updated
    result = render(plan, [], shown, full_reason=None)
    assert result.mode == "changes"
    assert result.lines[0] == CHANGE_HEADER
    assert result.lines[2:] == ["Changed or new:", updated.text]
    next_view = render(plan, [], result.shown, full_reason=None)
    assert next_view.lines[0] == CHANGE_HEADER
    assert next_view.lines[-1] == "No step changed."
    assert next_view.shown.turns_since_full == 2


def test_consecutive_change_views_list_only_changes_since_previous_message() -> None:
    plan = items()
    shown = render(plan, [], None, full_reason=None).shown
    for index in (1, 4):
        plan[index] = replace(plan[index], state="verified", text=plan[index].text.replace("pending", "verified"))
        result = render(plan, [], shown, full_reason=None)
        assert result.mode == "changes"
        assert result.lines[0] == CHANGE_HEADER
        assert result.lines[2:] == ["Changed or new:", plan[index].text]
        assert f"verified: {', '.join(item.label for item in plan if item.state == 'verified')}" in result.lines[1]
        shown = result.shown


def test_new_progress_preserves_duplicate_entry_counts_without_repeating_old_entries() -> None:
    plan = items()
    shown = render(plan, PROGRESS, None, full_reason=None).shown
    result = render(plan, [*PROGRESS, PROGRESS[1]], shown, full_reason=None)
    assert result.lines[2:] == ["No step changed.", "", "New progress:", PROGRESS[1]]
