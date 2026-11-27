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
