"""Pi residents use disposable JSON subprocesses and owner-local session files."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ApiProtocol, AuthKind
from looper.agents.runtimes.per_turn import PerTurnSession
from looper.agents.runtimes.pi import PiRuntime
from looper.agents.runtimes.pricing import ModelPrice

PI = PiRuntime()
PRICE = ModelPrice(1, 2, 0.1, 1.25)
TOKENS = Usage(input_tokens=120, output_tokens=10, cache_read_tokens=80, cache_creation_tokens=4)
TURN = Usage(cost_usd=PRICE.cost(TOKENS), **TOKENS.to_dict())

_FAKE_PI = """
import json, os, pathlib, sys, time, uuid
args = sys.argv[1:]
def flag(name):
    return args[args.index(name) + 1] if name in args else None
assert '-p' in args and flag('--mode') == 'json'
assert not any(x in args for x in ('--resume', '--session-id', '--continue', '--no-session'))
directory = pathlib.Path(flag('--session-dir'))
assert os.environ['PI_CODING_AGENT_SESSION_DIR'] == str(directory)
message = sys.stdin.read()  # No output until EOF: every turn must close stdin.
home = pathlib.Path(os.environ['HOME'])
with (home / 'argv.jsonl').open('a') as log:
    log.write(json.dumps({'pid': os.getpid(), 'args': args, 'message': message}) + '\\n')
if message == 'no-session':
    sys.exit(1)
path = pathlib.Path(flag('--fork') or flag('--session')) if flag('--fork') or flag('--session') else None
if path:
    assert path.is_absolute() and path.parent == directory and path.is_file()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    session_id = rows[0]['id']
    history = [row['text'] for row in rows[1:]]
    if flag('--fork'):
        assert '--session' not in args
        assert set(flag('--tools').split(',')) == {'read', 'grep', 'find', 'ls', 'mcp__looper__*'}
        session_id = str(uuid.uuid4())
        rows[0].update(id=session_id, parentSession=str(path), cwd=os.getcwd())
        path = directory / (session_id + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\\n' for row in rows))
else:
    session_id = str(uuid.uuid4())
    path = directory / ('2026-10-20T00-00-00-000Z_' + session_id + '.jsonl')
    path.write_text(json.dumps({'type': 'session', 'id': session_id}) + '\\n')
    path.chmod(0o600)
    history = []
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'session', 'id': session_id, 'cwd': os.getcwd()})
if message == 'crash':
    sys.exit(7)
if message == 'hang':
    time.sleep(60)
history.append(message)
with path.open('a') as saved:
    saved.write(json.dumps({'text': message}) + '\\n')
prompt = pathlib.Path(flag('--append-system-prompt')).read_text()
for stop in ('toolUse', 'stop'):
    emit({'type': 'message_end', 'message': {'role': 'assistant',
          'stopReason': 'error' if message == 'error' else stop,
          'usage': {'input': 60, 'output': 5, 'cacheRead': 40, 'cacheWrite': 2,
                                     'cost': {'total': 999}},
          'content': [{'type': 'text', 'text': json.dumps({'prompt': prompt, 'history': history})}]}})
# Historical statistics are diagnostic, never this turn's spending.
emit({'type': 'session_stats', 'tokens': {'input': 1000000}, 'cost': 999})
emit({'type': 'agent_settled', 'aborted': False})
"""


def _launch() -> Launch:
    provider = ProviderSpec('test-provider', 'test', auth=AuthKind.API_KEY,
                            protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}),
                            base_url='https://pi.example.invalid/v1', api_key='test-key')
    profile = AgentProfile('test-pi', 'pi/test', RuntimeSpec('rt-pi', 'pi', 'pi'), provider, 'selected', 'high')
    return Launch(AgentSettings(profile=profile))


def _command(root: Path, executable: str, prompt: str = 'prompt A') -> list[str]:
    path = root / '.local' / 'prompts' / 'prompt.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt)
    return PI.resident_command(executable, cwd=root, prompt_file=path, launch=_launch())


def _sessions(root: Path, owner: str = 'manager') -> Path:
    return root / '.local' / 'pi' / 'sessions' / owner


def _runs(command: list[str]) -> list[dict[str, Any]]:
    path = Path(command[1].removeprefix('HOME=')) / 'argv.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _save(directory: Path, session_id: str, stamp: str = 'old') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{stamp}_{session_id}.jsonl'
    path.write_text(json.dumps({'type': 'session', 'id': session_id}) + '\n')
    return path


async def _ignore(event: dict[str, Any]) -> None:
    pass


async def _start(session: Any, command: list[str], root: Path, **kwargs: Any) -> None:
    await session.start(command, cwd=str(root), signature=tuple(command), env=PI.environment(_launch()),
                        price=lambda model, usage: PRICE.cost(usage) if model == 'selected' else None, **kwargs)


def test_consult_forks_a_finished_seat_with_history_and_read_only_tools(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)

    async def run() -> None:
        seat = PI.resident_session('worker-seat')
        await _start(seat, command, tmp_path)
        turn = await seat.run_turn('seat history', _ignore)
        await seat.stop()
        source = next(_sessions(tmp_path, 'worker-seat').glob('*.jsonl'))
        before, modified = source.read_bytes(), source.stat().st_mtime_ns
        fork_dir = tmp_path / 'consult'
        consult = PI.consult_command(
            executable, cwd=tmp_path, prompt_file=tmp_path / '.local' / 'prompts' / 'prompt.md',
            launch=replace(_launch(), consult=True), session_id=turn.result_event['session_id'],
            session_dir=source.parent, fork_dir=fork_dir)
        process = await asyncio.create_subprocess_exec(
            *consult, cwd=tmp_path, env=PI.environment(_launch()), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await PI.write_turn(process.stdin, 'consult question')
            events = [event async for event in PI.parse(process.stdout, model='selected')]
            assert await process.wait() == 0, (await process.stderr.read()).decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        result = next(event for event in events if event['type'] == 'result')
        assert not result['is_error']
        assert result['session_id'] != turn.result_event['session_id']
        assert json.loads(result['result'])['history'] == ['seat history', 'consult question']
        assert source.read_bytes() == before and source.stat().st_mtime_ns == modified

    asyncio.run(asyncio.wait_for(run(), 10))


def test_each_turn_gets_a_new_pid_and_resumes_the_exact_session_after_restart(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    first = _command(tmp_path, executable)

    async def run() -> tuple[list[Any], list[dict[str, Any]], Usage, list[str]]:
        session = PI.resident_session('planner-task-1')
        assert isinstance(session, PerTurnSession)
        events: list[dict[str, Any]] = []

        async def record(event: dict[str, Any]) -> None:
            events.append(event)

        await _start(session, first, tmp_path)
        assert session.started and session.pid is None
        one = await session.run_turn('hello\n世界', record)
        two = await session.run_turn('again', record)
        assert session.pid is None and not session.stale(tuple(first))
        saved, totals = one.result_event['session_id'], session.totals
        await session.stop()
        assert not session.started
        second = _command(tmp_path, executable, 'prompt B') + PI.resume_args(saved)
        assert first != second
        restarted = PI.resident_session('planner-task-1')
        await _start(restarted, second, tmp_path, totals=totals)
        three = await restarted.run_turn('third', record)
        return [one, two, three], events, restarted.totals, second

    turns, events, totals, second = asyncio.run(asyncio.wait_for(run(), 10))
    assert all(turn.session_started and not turn.result_event['is_error'] for turn in turns)
    assert len({turn.result_event['session_id'] for turn in turns}) == 1
    payloads = [json.loads(turn.result_event['result']) for turn in turns]
    assert [p['history'] for p in payloads] == [
        ['hello\n世界'], ['hello\n世界', 'again'], ['hello\n世界', 'again', 'third']]
    assert 'prompt A' in payloads[0]['prompt'] and 'prompt B' in payloads[2]['prompt']
    assert [turn.usage for turn in turns] == [TURN] * 3
    assert totals == TURN.plus(TURN).plus(TURN)
    assert [e['usage']['input_tokens'] for e in events if e['type'] == 'usage'] == [60] * 6
    runs = _runs(first)
    assert len(runs) == 3 and len({r['pid'] for r in runs}) == 3
    directory = _sessions(tmp_path, 'planner-task-1')
    exact = next(directory.glob('*.jsonl'))
    assert directory.stat().st_mode & 0o777 == 0o700
    for index, run in enumerate(runs):
        args = run['args']
        assert args[args.index('--session-dir') + 1] == str(directory)
        assert not {'--resume', '--session-id', '--no-session'} & set(args)
        if index:
            assert args[args.index('--session') + 1] == str(exact)
        else:
            assert '--session' not in args
    assert first[first.index('--append-system-prompt') + 1] != second[second.index('--append-system-prompt') + 1]


@pytest.mark.parametrize('case', ['missing', 'ambiguous', 'partial', 'symlink', 'other-owner',
                                  'wrong-header', 'invalid-header'])
def test_unresolvable_saved_id_starts_no_session_or_process(tmp_path: Path, case: str) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)
    directory = _sessions(tmp_path)
    if case == 'ambiguous':
        _save(directory, 'saved', 'one')
        _save(directory, 'saved', 'two')
    elif case == 'partial':
        _save(directory, 'saved-longer')
    elif case in ('wrong-header', 'invalid-header'):
        path = _save(directory, 'saved')
        path.write_text('{invalid\n' if case == 'invalid-header' else
                        json.dumps({'type': 'session', 'id': 'someone-else'}) + '\n')
    elif case in ('symlink', 'other-owner'):
        other = _save(_sessions(tmp_path, 'planner-other'), 'saved')
        if case == 'symlink':
            directory.mkdir()
            (directory / other.name).symlink_to(other)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*.jsonl')}

    async def run() -> Any:
        session = PI.resident_session('manager')
        await _start(session, command + PI.resume_args('saved'), tmp_path, totals=TURN)
        turn = await session.run_turn('hello', _ignore)
        assert session.stale(tuple(command + PI.resume_args('saved')))
        assert session.totals == TURN
        return turn

    turn = asyncio.run(asyncio.wait_for(run(), 10))
    assert (turn.result_event, turn.session_started, turn.exit_code, turn.usage) == (None, False, 1, Usage())
    assert not _runs(command)
    assert {p: p.read_bytes() for p in tmp_path.rglob('*.jsonl')} == before


@pytest.mark.parametrize('message,started,code', [('crash', True, 7), ('no-session', False, 1)])
def test_an_unfinished_turn_is_stale_and_does_not_prune_or_add_usage(
    tmp_path: Path, message: str, started: bool, code: int,
) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)
    old = _save(_sessions(tmp_path), 'old-session')

    async def run() -> Any:
        session = PI.resident_session('manager')
        await _start(session, command, tmp_path)
        turn = await session.run_turn(message, _ignore)
        assert session.stale(tuple(command)) and session.totals == Usage()
        return turn

    turn = asyncio.run(asyncio.wait_for(run(), 10))
    assert (turn.result_event, turn.session_started, turn.exit_code) == (None, started, code)
    assert old.exists()


def test_only_a_successful_new_session_prunes_its_owners_old_sessions(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)

    async def run() -> None:
        manager, planner = PI.resident_session('manager'), PI.resident_session('planner-task-1')
        await _start(manager, command, tmp_path)
        one = await manager.run_turn('one', _ignore)
        saved = one.result_event['session_id']
        old = _save(_sessions(tmp_path), 'old-session')
        await _start(planner, command, tmp_path)
        await planner.run_turn('planner', _ignore)
        planner_files = {p: p.read_bytes() for p in _sessions(tmp_path, 'planner-task-1').iterdir()}
        await manager.run_turn('two', _ignore)  # A resumed success must not prune.
        assert old.exists()
        await manager.stop()
        await _start(manager, command, tmp_path)
        new = await manager.run_turn('new', _ignore)
        assert new.result_event['session_id'] != saved
        assert not old.exists()
        assert len(list(_sessions(tmp_path).iterdir())) == 1
        assert {p: p.read_bytes() for p in planner_files} == planner_files

    asyncio.run(asyncio.wait_for(run(), 10))


def test_an_error_result_preserves_old_sessions_but_still_counts_its_requests(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)
    old = _save(_sessions(tmp_path), 'old-session')

    async def run() -> None:
        session = PI.resident_session('manager')
        await _start(session, command, tmp_path)
        turn = await session.run_turn('error', _ignore)
        assert turn.session_started and turn.result_event['is_error']
        assert turn.usage == session.totals == TURN
        assert old.exists()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_a_session_deleted_between_turns_does_not_silently_start_a_new_one(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)

    async def run() -> None:
        session = PI.resident_session('manager')
        await _start(session, command, tmp_path)
        await session.run_turn('one', _ignore)
        next(_sessions(tmp_path).glob('*.jsonl')).unlink()
        turn = await session.run_turn('two', _ignore)
        assert not turn.session_started and turn.result_event is None
        assert session.stale(tuple(command)) and session.totals == TURN
        assert len(_runs(command)) == 1

    asyncio.run(asyncio.wait_for(run(), 10))


def test_write_turn_closes_stdin_and_refuses_a_second_prompt(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'echo', 'import sys\nprint(sys.stdin.read(), end="")\n')

    async def run() -> None:
        process = await asyncio.create_subprocess_exec(executable, stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE)
        assert process.stdin is not None and process.stdout is not None
        try:
            await PI.write_turn(process.stdin, 'hello\n世界', uuid='unused')
            with pytest.raises(RuntimeError):
                await PI.write_turn(process.stdin, 'second')
            assert (await process.stdout.read()).decode() == 'hello\n世界'
            assert await process.wait() == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_cancellation_reaps_the_per_turn_process_and_marks_it_stale(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / 'pi', _FAKE_PI)
    command = _command(tmp_path, executable)

    async def run() -> None:
        session = PI.resident_session('manager')
        await _start(session, command, tmp_path)
        ready = asyncio.Event()

        async def record(event: dict[str, Any]) -> None:
            if event.get('subtype') == 'init':
                ready.set()

        task = asyncio.create_task(session.run_turn('hang', record))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            pid = session.pid
            assert pid is not None
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert session.pid is None and session.stale(tuple(command))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(asyncio.wait_for(run(), 15))
