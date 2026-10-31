"""Real keeper processes expose the same Claude channel as directly owned runs."""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import shutil
import string
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import keeper, proc_tree
from looper.agents.kept_run import KeeperLost, KeptRun, KeptRunState, KeptStdin, KeptStream
from looper.agents.keeper.client import KeeperClient, KeeperGone
from looper.agents.keeper.protocol import ErrorCode, KeeperError
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.runtimes.channel import RunStartError
from looper.agents.tiers.worker import ProcessRun
from tests.fake_claude import fake_claude
from tests.runtime_model_helpers import builtin_launch

ROOT = Path(__file__).resolve().parents[1]
ECHO = """\
import json, sys
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'kept-test'}), flush=True)
for line in sys.stdin:
    turn = json.loads(line)
    print(json.dumps({'type': 'assistant', 'message': {'content': turn['message']['content']}}), flush=True)
"""


def exercise(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, 35))


async def eventually(predicate: Callable, timeout: float = 8):
    async with asyncio.timeout(timeout):
        while not (result := predicate()):
            await asyncio.sleep(0.02)
        return result


def live(pid: int) -> bool:
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True,
                            text=True, check=False, timeout=5)
    assert result.returncode in (0, 1), result.stderr
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


def short_directory() -> Path:
    # This checkout leaves just four bytes before the socket filename's limit.
    # A normal pytest temporary path is too long for a Unix socket on macOS.
    for suffix in random.sample(string.ascii_letters + string.digits, 62):
        directory = ROOT / f'.k{suffix}'
        if not directory.exists():
            assert len(os.fsencode(directory / keeper.SOCKET)) <= keeper.SOCKET_PATH_MAX
            return directory
    raise AssertionError('no short keeper test directory is available')


async def clean_directory(directory: Path) -> None:
    state = keeper.read_json(directory / keeper.STATE) or {}
    child, started = state.get('child_pid'), state.get('child_started')
    if child and started and live(child):
        await proc_tree.end_orphan(child, started, label='kept-run test cleanup')
    pid, started = state.get('keeper_pid'), state.get('keeper_started')
    if pid and started and live(pid) and proc_tree._start_time(pid) == started:
        os.kill(pid, signal.SIGTERM)
        await eventually(lambda: not live(pid), timeout=10)
    shutil.rmtree(directory, ignore_errors=True)
    if child:
        assert not live(child), 'test left the Claude child running'
    if pid:
        assert not live(pid), 'test left the keeper running'


@asynccontextmanager
async def opened(tmp_path: Path, body: str = ECHO, *, on_stale=None) -> AsyncIterator[KeptRun]:
    directory = short_directory()
    executable = fake_claude(tmp_path / 'claude', body)
    handle = KeptRun(CLAUDE_CODE, [executable], launch=builtin_launch(), cwd=tmp_path,
                     session_dir=None, label='kept-run test', run_dir=directory,
                     runtime_root=tmp_path / 'runtime', epoch=1, on_stale=on_stale or (lambda reason: None))
    try:
        await handle.launch()
        yield handle
    finally:
        await handle.end()
        await clean_directory(directory)


def test_a_kept_run_streams_the_same_events_as_a_process_run(tmp_path: Path) -> None:
    async def run() -> None:
        executable = fake_claude(tmp_path / 'direct-claude', ECHO)
        direct = ProcessRun(CLAUDE_CODE, [executable], launch=builtin_launch(), cwd=tmp_path,
                            session_dir=None, label='direct-run test')

        async def turns(handle):
            received = asyncio.create_task(collect(handle))
            await handle.channel.start('first prompt')
            assert await handle.channel.send('second turn', uuid='turn-two')
            handle.channel.end_input()
            return await received, await handle.wait()

        async def collect(handle):
            return [event async for event in handle.channel.events()]

        await direct.launch()
        try:
            expected = await turns(direct)
        finally:
            await direct.end()
        async with opened(tmp_path) as kept:
            assert await turns(kept) == expected
            assert expected[1] == 0
            assert [event.get('message', {}).get('content') for event in expected[0]][1:] == [
                'first prompt', 'second turn']
            assert kept.command == [str(tmp_path / 'claude')]
            manifest = keeper.read_json(kept.run_dir / keeper.MANIFEST)
            assert manifest['record_id'] is None

    exercise(run())


def test_partial_lines_reach_the_parser_whole(tmp_path: Path) -> None:
    body = """\
import json, os, time
line = json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'partial'}).encode() + b'\\n'
for part in (line[:10], line[10:25], line[25:]):
    os.write(1, part)
    time.sleep(.04)
os.write(1, json.dumps({'type': 'assistant', 'message': {'content': 'no newline'}}).encode())
"""

    async def run() -> None:
        async with opened(tmp_path, body) as handle:
            events = [event async for event in handle.channel.events()]
            assert events == [
                {'type': 'system', 'subtype': 'init', 'session_id': 'partial'},
                {'type': 'assistant', 'message': {'content': 'no newline'}},
            ]
            assert await handle.wait() == 0

    exercise(run())


def test_a_first_prompt_larger_than_one_mebibyte_is_written_in_pieces(tmp_path: Path) -> None:
    async def run() -> None:
        async with opened(tmp_path) as handle:
            received = asyncio.create_task(_collect(handle))
            prompt = 'x' * (1024 * 1024 + 37)
            await handle.channel.start(prompt)
            handle.channel.end_input()
            events = await received
            assert events[-1]['message']['content'] == prompt
            assert await handle.wait() == 0
            stored = (handle.run_dir / keeper.STDIN).read_bytes()
            assert json.loads(stored)['message']['content'] == prompt

    exercise(run())


async def _collect(handle):
    return [event async for event in handle.channel.events()]


def test_end_ends_the_tree(tmp_path: Path) -> None:
    body = """\
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
with open('tree.json', 'w') as target:
    json.dump({'parent': os.getpid(), 'grandchild': child.pid}, target)
time.sleep(60)
"""

    async def run() -> None:
        async with opened(tmp_path, body) as handle:
            tree = await eventually(lambda: keeper.read_json(tmp_path / 'tree.json'))
            assert all(live(pid) for pid in tree.values())
            await handle.end()
            assert await handle.wait() < 0
            assert all(not live(pid) for pid in tree.values())
            await handle.end()
            assert handle.state is KeptRunState.ENDED

    exercise(run())


def test_a_lost_keeper_fails_only_that_run(tmp_path: Path) -> None:
    async def run() -> None:
        async with opened(tmp_path, 'import time; time.sleep(60)') as handle:
            state = keeper.read_json(handle.run_dir / keeper.STATE)
            child = handle.pid
            os.kill(state['keeper_pid'], signal.SIGKILL)
            with pytest.raises(KeeperLost, match="the run's keeper process ended unexpectedly"):
                await asyncio.wait_for(handle.wait(), 2)
            await eventually(lambda: not live(child))
            assert handle.state is KeptRunState.LOST

    exercise(run())


def test_a_fenced_run_sends_nothing(tmp_path: Path) -> None:
    async def run() -> None:
        async with opened(tmp_path) as handle:
            await handle.channel.start('accepted')
            before = (handle.run_dir / keeper.STDIN).read_bytes()
            handle.fence()
            await handle.channel.send('must not be sent')
            handle.channel.end_input()
            await handle.end()
            await asyncio.sleep(.05)
            assert (handle.run_dir / keeper.STDIN).read_bytes() == before
            assert live(handle.pid)
            assert handle.state is KeptRunState.FENCED

    exercise(run())


def test_error_output_reads_the_stderr_log(tmp_path: Path) -> None:
    body = "import sys; sys.stderr.buffer.write(b'first\\xff' + b'a' * (100 * 1024 - 10) + b'last'); raise SystemExit(7)"

    async def run() -> None:
        async with opened(tmp_path, body) as handle:
            assert await handle.wait() == 7
            assert await handle.error_output() == ('first\ufffd' + 'a' * (65536 - 6))

    exercise(run())


def test_a_keeper_survives_the_kill_of_its_starters_process_group(tmp_path: Path) -> None:
    async def run() -> None:
        directory = short_directory()
        executable = fake_claude(tmp_path / 'claude', 'import time; time.sleep(60)')
        script = f"""\
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from looper.agents.kept_run import KeptRun
from looper.agents.keeper.client import KeeperClient, KeeperGone
from looper.agents.keeper.protocol import ErrorCode, KeeperError
from looper.agents.runtimes import CLAUDE_CODE
from tests.runtime_model_helpers import builtin_launch
async def main():
    handle = KeptRun(CLAUDE_CODE, [{executable!r}], launch=builtin_launch(), cwd=Path({str(tmp_path)!r}), session_dir=None,
                     label='starter test', run_dir=Path({str(directory)!r}), runtime_root=Path({str(tmp_path / 'runtime')!r}),
                     epoch=1, on_stale=lambda reason: None)
    await handle.launch()
    Path({str(tmp_path / 'ready')!r}).touch()
    await asyncio.sleep(60)
asyncio.run(main())
"""
        starter = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=ROOT,
                                                       start_new_session=True, stdout=asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.PIPE)
        try:
            await eventually(lambda: (tmp_path / 'ready').exists())
            state = keeper.read_json(directory / keeper.STATE)
            assert os.getpgid(state['keeper_pid']) == state['keeper_pid']
            assert os.getpgid(state['child_pid']) == state['child_pid']
            assert os.getsid(state['keeper_pid']) == state['keeper_pid']
            assert os.getsid(state['child_pid']) == state['child_pid']
            os.killpg(starter.pid, signal.SIGKILL)
            await starter.wait()
            assert live(state['keeper_pid'])
            assert live(state['child_pid'])
        finally:
            if starter.returncode is None:
                os.killpg(starter.pid, signal.SIGKILL)
                await starter.wait()
            await clean_directory(directory)

    exercise(run())


def test_kept_stream_buffers_partial_lines_and_reports_idle() -> None:
    async def run() -> None:
        stream = KeptStream()
        reading = asyncio.create_task(stream.read(100))
        await stream.idle.wait()
        stream.feed(0, b'partial')
        await asyncio.sleep(0)
        assert not reading.done()
        stream.feed(7, b' line\ntrailing')
        assert await reading == b'partial line\n'
        assert stream.delivered == 13
        assert not stream.idle.is_set()
        stream.feed_eof(21)
        assert await stream.read(100) == b'trailing'
        assert await stream.read(100) == b''
        assert stream.delivered == 21

    exercise(run())


def test_kept_stream_pause_buffers_until_resume() -> None:
    async def run() -> None:
        stream = KeptStream()
        stream.pause()
        stream.feed(0, b'one\ntwo\n')
        stream.feed_eof(8)
        reading = asyncio.create_task(stream.read(100))
        await stream.idle.wait()
        assert not reading.done()
        stream.resume()
        assert await reading == b'one\ntwo\n'
        assert await stream.read(100) == b''
        assert stream.delivered == 8

    exercise(run())


@pytest.mark.parametrize('eof', [False, True])
def test_kept_stream_rejects_noncontiguous_offsets(eof: bool) -> None:
    stream = KeptStream()
    stream.feed(0, b'abc')
    with pytest.raises((ValueError, RuntimeError)):
        stream.feed_eof(2) if eof else stream.feed(4, b'def\n')


def test_a_run_directory_cannot_be_reused(tmp_path: Path) -> None:
    async def run() -> None:
        async with opened(tmp_path) as first:
            second = KeptRun(CLAUDE_CODE, first.command, launch=builtin_launch(), cwd=tmp_path,
                             session_dir=None, label='duplicate', run_dir=first.run_dir,
                             runtime_root=tmp_path / 'runtime', epoch=1, on_stale=lambda reason: None)
            with pytest.raises(RunStartError):
                await second.launch()
            second.release()
            assert (first.run_dir / keeper.MANIFEST).is_file()
            assert live(first.pid)

    exercise(run())


@pytest.mark.parametrize(('error', 'expected', 'message'), [
    (KeeperError(ErrorCode.OFFSET_MISMATCH, 'bad offset'), RuntimeError, 'keeper stdin offset mismatch'),
    (KeeperError(ErrorCode.CHILD_EXITED, 'exited'), BrokenPipeError, 'keeper child exited'),
    (KeeperGone('gone'), ConnectionResetError, 'gone'),
])
def test_kept_stdin_maps_rejected_and_lost_writes(error, expected, message) -> None:
    async def run() -> None:
        client = SimpleNamespace(stdin_write=AsyncMock(side_effect=error))
        stdin = KeptStdin(client, lambda: True, Mock())
        stdin.write(b'prompt')
        with pytest.raises(expected, match=message):
            await stdin.drain()
        assert stdin.inflight is None
        assert stdin.offset == 0

    exercise(run())


def test_kept_stdin_chunks_writes_and_tracks_inflight_drain() -> None:
    async def run() -> None:
        writes, release, writing = [], asyncio.Event(), asyncio.Event()

        async def write(piece, *, expect_offset):
            writes.append((piece, expect_offset))
            writing.set()
            await release.wait()
            return expect_offset + len(piece)

        client = SimpleNamespace(stdin_write=write, stdin_close=AsyncMock())
        stdin = KeptStdin(client, lambda: True, Mock())
        payload = b'x' * (1024 * 1024 + 3)
        stdin.write(payload)
        draining = asyncio.create_task(stdin.drain())
        await writing.wait()
        assert stdin.inflight is not None
        assert stdin.inflight is not draining
        release.set()
        await draining
        assert [len(piece) for piece, _ in writes] == [1024 * 1024, 3]
        assert [offset for _, offset in writes] == [0, 1024 * 1024]
        assert b''.join(piece for piece, _ in writes) == payload
        assert stdin.offset == len(payload)
        assert stdin.inflight is None
        stdin.close()
        stdin.close()
        await asyncio.sleep(0)
        assert stdin.is_closing()
        client.stdin_close.assert_awaited_once_with()

    exercise(run())


def test_a_new_controller_fences_the_old_run_without_ending_its_child(tmp_path: Path) -> None:
    async def run() -> None:
        reasons = []
        async with opened(tmp_path, on_stale=reasons.append) as handle:
            client = KeeperClient()
            try:
                await client.connect(handle.run_dir / keeper.SOCKET)
                await client.initialize(epoch=2, looper_version='test')
                await eventually(lambda: reasons)
                assert reasons == ['a keeper reported a newer control plane']
                assert handle.state is KeptRunState.FENCED
                assert live(handle.pid)
                before = (handle.run_dir / keeper.STDIN).read_bytes()
                await handle.channel.send('old controller cannot send')
                assert (handle.run_dir / keeper.STDIN).read_bytes() == before
            finally:
                await client.close()
                await client.wait_closed()

    exercise(run())


@pytest.mark.parametrize('connected', [False, True], ids=['default', 'during-initialize'])
def test_a_failed_keeper_launch_releases_its_directory(tmp_path: Path, monkeypatch, connected: bool) -> None:
    async def run() -> None:
        directory = short_directory()
        handle = KeptRun(CLAUDE_CODE, [str(tmp_path / 'absent-claude')], launch=builtin_launch(),
                         cwd=tmp_path, session_dir=None, label='cannot-start test', run_dir=directory,
                         runtime_root=tmp_path / 'runtime', epoch=1, on_stale=lambda reason: None)
        if connected:
            # Hold spawn until the real socket has an initialize request in flight.
            allowed = tmp_path / 'spawn-allowed'
            boot_argv = keeper.boot_argv

            def delayed_boot(*args):
                command = boot_argv(*args)
                setup = f"""
import asyncio
from pathlib import Path
from looper.agents import proc_tree
original_spawn = proc_tree.spawn
async def delayed_spawn(*args, **kwargs):
    while not Path({str(allowed)!r}).exists():
        await asyncio.sleep(0.01)
    return await original_spawn(*args, **kwargs)
proc_tree.spawn = delayed_spawn
"""
                command[3] = command[3].replace('; sys.exit(', '\n' + setup + '\nsys.exit(')
                return command

            initialize = handle._client.initialize

            async def initialize_before_failure(**kwargs):
                request = asyncio.create_task(initialize(**kwargs))
                await asyncio.sleep(0)
                allowed.touch()
                return await request

            monkeypatch.setattr(keeper, 'boot_argv', delayed_boot)
            monkeypatch.setattr(handle._client, 'initialize', initialize_before_failure)
        try:
            with pytest.raises(RunStartError, match='absent-claude'):
                await handle.launch()
            assert not live(handle._keeper_process.pid)
            assert not directory.exists()
        finally:
            await handle.end()
            await clean_directory(directory)

    exercise(run())


@pytest.mark.parametrize('starting', [False, True, 'timeout'])
def test_fencing_during_end_preserves_the_child_and_run_directory(tmp_path: Path, starting: bool | str) -> None:
    async def run() -> None:
        directory = tmp_path / 'run'
        directory.mkdir()
        (directory / 'must-survive').touch()
        handle = KeptRun(CLAUDE_CODE, ['claude'], launch=builtin_launch(), cwd=tmp_path,
                         session_dir=None, label='fence race', run_dir=directory,
                         runtime_root=tmp_path / 'runtime', epoch=1, on_stale=lambda reason: None)
        entered, resume = asyncio.Event(), asyncio.Event()

        async def ending(**kwargs):
            entered.set()
            await resume.wait()
            if not starting:
                raise KeeperGone('fence aborted the socket')
            if starting == 'timeout':
                raise TimeoutError
            return 0

        handle._owns_dir = True
        handle.state = KeptRunState.STARTING if starting else KeptRunState.ATTACHED
        handle._client = SimpleNamespace(_writer=None, end=ending, close=AsyncMock(), wait_closed=AsyncMock())
        handle._end_orphan = AsyncMock()
        if starting:
            handle._keeper_process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(), wait=ending)
        ending_task = asyncio.create_task(handle.end())
        await entered.wait()
        handle.fence()
        resume.set()
        await ending_task
        assert handle.state is KeptRunState.FENCED
        handle._end_orphan.assert_not_awaited()
        if starting:
            handle._keeper_process.kill.assert_not_called()
        handle.release()
        assert (directory / 'must-survive').exists()

    exercise(run())


def test_losing_the_connection_during_end_does_not_leave_wait_blocked(tmp_path: Path) -> None:
    async def run() -> None:
        async with opened(tmp_path, 'import time; time.sleep(60)') as handle:
            async def gone(**kwargs):
                await handle._client.close()
                await handle._client.wait_closed()
                raise KeeperGone('connection lost before child/end reply')

            handle._client.end = gone
            await handle.end()
            assert not live(handle.pid)
            with pytest.raises(KeeperLost):
                await asyncio.wait_for(handle.wait(), 2)

    exercise(run())


def test_a_drain_completion_can_be_awaited_while_its_caller_keeps_running() -> None:
    async def run() -> None:
        writing, release, caller_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def write(piece, *, expect_offset):
            writing.set()
            await release.wait()
            return expect_offset + len(piece)

        stdin = KeptStdin(SimpleNamespace(stdin_write=write), lambda: True, Mock())

        async def caller():
            stdin.write(b'hello')
            await stdin.drain()
            await caller_release.wait()

        task = asyncio.create_task(caller())
        try:
            await writing.wait()
            inflight = stdin.inflight
            assert inflight is not None
            release.set()
            await asyncio.wait_for(asyncio.shield(inflight), 1)
            assert not task.done()
            assert stdin.inflight is None
        finally:
            release.set()
            caller_release.set()
            await task

    exercise(run())


def test_normal_wait_finishes_callbacks_and_keeper_before_release(tmp_path: Path) -> None:
    async def run() -> None:
        async with opened(tmp_path) as handle:
            received = asyncio.create_task(_collect(handle))
            await handle.channel.start('finish')
            handle.channel.end_input()
            await received
            assert await handle.wait() == 0
            assert handle._keeper_process.returncode == 0
            assert handle._client._dispatcher_task.done()
            assert handle._client._dispatcher_task.exception() is None
            handle.release()
            assert not handle.run_dir.exists()
            await asyncio.sleep(0)
            assert handle._client._dispatcher_task.exception() is None

    exercise(run())
