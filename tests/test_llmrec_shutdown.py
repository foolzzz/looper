"""Recorder ownership across signals, parent death and planned handover."""

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import time

import pytest

from looper.agents import proc_tree
from looper.agents.llmrec import STATE_FILE, process_alive
from looper.agents.llmrec.control import ProxyControl


# A serve stand-in uses the real controller and proxy, without a database or agents.
STAND_IN = '''
import asyncio, signal, sys
from pathlib import Path
from looper.agents.llmrec import control
boot = control.boot_argv
def isolated_boot(*args):
    argv = boot(*args)
    argv[3] = argv[3].replace('from looper.agents.llmrec.__main__ import main;',
        'from unittest.mock import AsyncMock; '
        'from looper.agents.llmrec import __main__ as process; '
        'process.init_pool = AsyncMock(side_effect=OSError()); '
        'from looper.agents.llmrec.__main__ import main;')
    if sys.argv[2] == 'blocked_shutdown':
        argv[3] = argv[3].replace('process.init_pool =',
            'process.proxy.Proxy.close = lambda self, **kw: process.asyncio.Event().wait(); process.init_pool =')
    return argv
control.boot_argv = isolated_boot
async def run():
    owner = control.ProxyControl(Path(sys.argv[1]), '', enabled=True)
    try:
        await owner._start()
        deadline = asyncio.get_running_loop().time() + 30
        while not await owner._health():
            assert asyncio.get_running_loop().time() < deadline, 'proxy not ready'
            await asyncio.sleep(.05)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
        print('ready', flush=True)
        await stopped.wait()
    finally:
        await owner.close(handover=sys.argv[2] == 'handover')
asyncio.run(run())
'''


@pytest.fixture
def stand_in(tmp_path):
    processes, identities = [], []

    def start(mode='stop'):
        child = subprocess.Popen([sys.executable, '-c', STAND_IN, str(tmp_path), mode],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(child)
        ready, _, _ = select.select([child.stdout], [], [], 60)
        assert ready, 'serve stand-in did not become ready'
        line = child.stdout.readline()
        assert line == 'ready\n', child.communicate(timeout=5)[1]
        state = json.loads((tmp_path / STATE_FILE).read_text())
        identities.append(state)
        return child, state

    yield start
    for child in processes:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    path = tmp_path / STATE_FILE
    if path.exists():
        identities.append(json.loads(path.read_text()))
    for state in identities:
        if process_alive(state['pid'], state['start_time']):
            os.kill(state['pid'], signal.SIGKILL)
            _wait_gone(state)


def _wait_gone(state, timeout=7):
    deadline = time.monotonic() + timeout
    while process_alive(state['pid'], state['start_time']):
        assert time.monotonic() < deadline, 'proxy outlived its owner'
        time.sleep(.05)


@pytest.mark.parametrize('sig,mode', [
    (signal.SIGTERM, 'stop'), (signal.SIGINT, 'stop'), (signal.SIGKILL, 'stop'),
    (signal.SIGKILL, 'blocked_shutdown'),
])
def test_proxy_exits_with_serve_parent(stand_in, sig, mode):
    parent, state = stand_in(mode)
    started = time.monotonic()
    parent.send_signal(sig)
    if sig == signal.SIGKILL:
        _wait_gone(state)  # The parent must not need to be reaped for its proxy to exit.
    code = parent.wait(timeout=8)
    assert code == (-signal.SIGKILL if sig == signal.SIGKILL else 0), parent.communicate(timeout=2)[1]
    _wait_gone(state)
    assert time.monotonic() - started < 8


@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGKILL])
def test_planned_handover_preserves_proxy_until_new_owner_stops_it(stand_in, sig):
    parent, state = stand_in('handover')
    parent.terminate()
    assert parent.wait(timeout=8) == 0
    # More than one owner-watch interval after the original parent disappears.
    time.sleep(1.5)
    assert proc_tree._start_time(state['pid']) == state['start_time']
    replacement, adopted = stand_in()
    assert (adopted['pid'], adopted['port']) == (state['pid'], state['port'])
    replacement.send_signal(sig)
    code = replacement.wait(timeout=8)
    assert code == (-signal.SIGKILL if sig == signal.SIGKILL else 0)
    _wait_gone(state)


def test_close_reaps_child_that_never_published_state(tmp_path, caplog):
    async def run():
        owner = ProxyControl(tmp_path, '', enabled=True)
        child = await asyncio.create_subprocess_exec(sys.executable, '-c',
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); signal.pause()",
            stdout=asyncio.subprocess.PIPE)
        owner._children[child.pid] = child
        try:
            assert await asyncio.wait_for(child.stdout.readline(), 3) == b'ready\n'
            await owner.close(grace_s=.1)
            assert child.returncode == -signal.SIGKILL
            assert not owner._children
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()
    asyncio.run(asyncio.wait_for(run(), 5))
    assert 'killed' in caplog.text
