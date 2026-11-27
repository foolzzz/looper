"""The serve smoke test's failure output keeps the evidence and drops the polling that would bury it."""
from tests.test_failure_handling_serve_db import failure_log


def test_failure_log_drops_polls_and_keeps_router_and_store_lines_from_before_the_tail():
    poll = '2026-11-27 10:50:20,343 looper.agents.http_server INFO api client=operator op=tasks.detail status=ok ms=3'
    text = '\n'.join([
        '2026-11-27 10:49:00 looper.agents.tiers.task ERROR task router error handling m-1',
        'Traceback (most recent call last):',
        '2026-11-27 10:49:01 looper.agents.tiers.task INFO task t-1 stopped',
        *[poll.replace('tasks.detail', 'tasks.holds' if i % 2 else 'tasks.detail') for i in range(2000)],
        '2026-11-27 10:50:30 looper.agents.http_server INFO api client=operator op=confirm status=ok ms=2'])
    shown = failure_log(text)
    assert len(shown) <= 12200 and 'op=tasks.detail' not in shown and 'op=tasks.holds' not in shown
    assert 'task router error handling m-1' in shown and 'Traceback' in shown
    assert shown.rstrip().endswith('op=confirm status=ok ms=2')
    assert failure_log('only an ordinary line') == 'only an ordinary line'


POLL = '2026-11-27 10:50:20,343 looper.agents.http_server INFO api client=operator op=tasks.detail status=ok ms=3'


def test_failure_log_keeps_failing_and_slow_polls():
    failing = '2026-11-27 10:50:21 looper.agents.http_server WARNING api client=operator op=tasks.detail status=error ms=3'
    slow = '2026-11-27 10:50:22 looper.agents.http_server WARNING api slow client=operator op=tasks.holds status=ok db_ms=900'
    shown = failure_log('\n'.join([POLL, failing, slow, POLL]))
    assert failing in shown and slow in shown
    assert POLL not in shown


def test_failure_log_keeps_the_first_router_error_when_many_stop_lines_follow():
    router = '2026-11-27 10:49:00 looper.agents.tiers.task ERROR task router error handling m-1'
    stops = [f'2026-11-27 10:49:{i % 60:02d} looper.agents.tiers.task INFO TASK_STOP t-{i} ' + 'x' * 80 for i in range(500)]
    shown = failure_log('\n'.join([*stops[:3], router, *stops[3:]]))
    assert router in shown
    assert len(shown) <= 12200


def test_failure_log_keeps_the_exception_line_after_a_traceback():
    text = '\n'.join([
        'Traceback (most recent call last):',
        '  File "a.py", line 1, in f',
        '    g()',
        'KeyError: swallowed-key',
        *[f'2026-11-27 10:49:00 INFO TASK_STOP t-{i} ' + 'x' * 80 for i in range(500)]])
    shown = failure_log(text)
    assert 'KeyError: swallowed-key' in shown.split('--- end of the log ---')[0]
