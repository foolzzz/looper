"""A task-end listener a test leaves behind must not reach the next test (the autouse fixture in conftest.py)."""
from looper.db import tasks


def test_a_test_registers_a_listener_and_does_not_remove_it():
    async def listener(task_id):
        return None

    tasks.task_ended_listeners.append(listener)
    assert len(tasks.task_ended_listeners) == 1


def test_the_next_test_starts_with_no_listeners():
    assert tasks.task_ended_listeners == []
