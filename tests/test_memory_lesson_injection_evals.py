"""Lesson evals are opt-in paired scenarios, separate from the existing default set."""
from evals.prompt_regression import tasks


def test_lesson_scenarios_are_explicit_on_off_pairs():
    names = ["memory_lessons_use", "memory_lessons_use_off", "memory_lessons_wrong", "memory_lessons_wrong_off"]
    selected = tasks.select(names)
    assert {task.id for task in selected} == set(names)
    assert not set(names) & {task.id for task in tasks.select(None)}
    for on, off in zip(selected[::2], selected[1::2]):
        assert on.lesson_mode == "on" and off.lesson_mode == "off"
        assert on.lesson_seeds == off.lesson_seeds
        assert on.files == off.files
