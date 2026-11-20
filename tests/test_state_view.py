"""The Manager's state block (``state_view.render``): whole, unchanged, or the changes since the last whole block."""

from __future__ import annotations

from looper.agents.tiers.state_view import StateView, render, rev_of

PROJECTS = ["Projects:", *(f"  - project-{i} (default directory ~/p{i}): " + f"what project {i} is about " * 3
                           for i in range(10)), ""]
READY = ["Ready tasks (created, not started; newest first):", "  - task-aaaaaaaaaaaa (project project-1): build a", ""]
STATE = [*PROJECTS, *READY]


def _whole(lines=STATE):
    return render(lines, None, session="sess-1", whole_reason=None)


def test_no_whole_block_yet_sends_it_whole_under_its_rev():
    rendered = _whole()
    assert rendered.view is StateView.WHOLE and rendered.reason == "no whole block yet"
    assert rendered.lines == [f"Looper state (rev {rev_of(STATE)}):", *STATE]
    assert (rendered.shown.rev, rendered.shown.lines, rendered.shown.session) == (rev_of(STATE), tuple(STATE), "sess-1")


def test_a_whole_reason_sends_it_whole_even_when_nothing_changed():
    shown = _whole().shown
    rendered = render(STATE, shown, session="sess-1", whole_reason="new process")
    assert rendered.view is StateView.WHOLE and rendered.reason == "new process"
    assert rendered.lines[0] == f"Looper state (rev {shown.rev}):"


def test_nothing_changed_is_one_line():
    shown = _whole().shown
    rendered = render(STATE, shown, session="sess-1", whole_reason=None)
    assert rendered.view is StateView.UNCHANGED
    assert rendered.lines == [f"Looper state: unchanged since rev {shown.rev}", ""]
    assert rendered.shown == shown


def test_a_small_change_lists_the_lines_under_their_section():
    shown = _whole().shown
    changed = [*PROJECTS, READY[0], "  - task-bbbbbbbbbbbb (project project-2): build b", *READY[1:]]
    rendered = render(changed, shown, session="sess-1", whole_reason=None)
    assert rendered.view is StateView.CHANGES
    assert rendered.lines == [
        f"Looper state: changes since rev {shown.rev} (+ added, - removed):",
        " Ready tasks (created, not started; newest first):",
        "+  - task-bbbbbbbbbbbb (project project-2): build b",
        "",
    ]
    assert rendered.shown == shown  # the next turn still compares with the whole block


def test_successive_changes_are_each_against_the_whole_block():
    shown = _whole().shown
    first = [*PROJECTS, READY[0], "  - task-bbbbbbbbbbbb (project project-2): build b", *READY[1:]]
    second = [*first[:-2], ""]  # task-aaaaaaaaaaaa started meanwhile
    rendered = render(second, render(first, shown, session="sess-1", whole_reason=None).shown,
                      session="sess-1", whole_reason=None)
    assert rendered.lines == [
        f"Looper state: changes since rev {shown.rev} (+ added, - removed):",
        " Ready tasks (created, not started; newest first):",
        "-  - task-aaaaaaaaaaaa (project project-1): build a",
        "+  - task-bbbbbbbbbbbb (project project-2): build b",
        "",
    ]


def test_a_new_section_is_added_with_its_heading():
    shown = _whole().shown
    gate = ["Open gates (waiting for the user's answer; newest first):", "  - task task-aaaaaaaaaaaa, gate g1", ""]
    rendered = render([*STATE, *gate], shown, session="sess-1", whole_reason=None)
    assert rendered.view is StateView.CHANGES
    assert rendered.lines[1:] == [f"+{gate[0]}", f"+{gate[1]}", ""]


def test_many_changed_lines_send_a_new_whole_block():
    shown = _whole().shown
    changed = [PROJECTS[0], *(line.replace("about", "for") for line in PROJECTS[1:5]), *PROJECTS[5:], *READY]
    rendered = render(changed, shown, session="sess-1", whole_reason=None)
    assert rendered.view is StateView.WHOLE and rendered.reason == "many lines changed"
    assert rendered.rev == rev_of(changed) != shown.rev
    assert rendered.lines[0] == f"Looper state (rev {rendered.rev}):" and rendered.shown.lines == tuple(changed)


def test_a_long_change_note_sends_a_new_whole_block():
    short = ["Projects:", *(f"  - p{i} (no default directory)" for i in range(10)), ""]
    shown = _whole(short).shown
    # Two changed lines of eleven, but the one added is longer than the rest of the block together.
    changed = [*short[:-2], "  - p9 (no default directory): " + "a long description " * 20, ""]
    rendered = render(changed, shown, session="sess-1", whole_reason=None)
    assert rendered.view is StateView.WHOLE and rendered.reason == "long change note"
