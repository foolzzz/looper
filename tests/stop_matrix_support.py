"""What the stop matrices share: the stand-in processes of each runtime (real pipes, real process trees), the
observers of a turn, and the checks on how a turn ended. ``tests/test_stop_matrix.py`` runs them under the runtimes'
sessions, ``tests/test_manager_stop_matrix.py`` under the Manager."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.claude_cli import USER_STOP, CapStop, user_stop_result
from looper.agents.run_usage import Usage

BOUND = 8.0  # every stopped turn ends within this (the graces are cut to fractions of a second)
BIG = "x" * (4 << 20)  # a message no pipe takes whole: its write blocks while the process reads nothing


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return pid in proc_tree._snapshot()


async def _until(check: Any, timeout: float = BOUND) -> None:
    async def poll() -> None:
        while not check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


class Seen:
    """``on_event`` of one turn: what it got, what came after its turn returned (``late``), and, once ``stop`` (the
    turn's ``Stop``) was asked, what it still got (``after_stop``). With ``hold_at``, it holds the first event of that
    type until ``release`` is set."""

    def __init__(self, hold_at: str | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False
        self.late: list[dict[str, Any]] = []
        self.stop: Any = None
        self.after_stop: list[dict[str, Any]] = []
        self.hold_at = hold_at
        self.holding, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, event: dict[str, Any]) -> None:
        (self.late if self.closed else self.events).append(event)
        if self.stop is not None and self.stop.asked and not self.closed:
            self.after_stop.append(event)
        if self.hold_at is not None and event.get("type") == self.hold_at and not self.holding.is_set():
            self.holding.set()
            await self.release.wait()


class Stop:
    """The Manager's ``stop=`` hook: once the user asked, the turn's result is ``user_stop_result``."""

    def __init__(self) -> None:
        self.asked = False

    def __call__(self) -> CapStop | None:
        return CapStop(user_stop_result(), Usage()) if self.asked else None


def _stopped(turn: Any) -> bool:
    """The turn ended as the user's stop, as the Manager reads it (``ManagerAgent._run_turn``)."""
    return turn.stopped is not None or bool((turn.result_event or {}).get(USER_STOP))


def _answered(turn: Any) -> bool:
    return not _stopped(turn) and turn.result_event is not None and not turn.result_event.get("is_error")


# --- One process per turn: Codex, OMP, pi, DSH (``per_turn.PerTurnSession``) ---

_PRELUDE = r'''
import json, os, subprocess, sys, time
DIR = os.environ['STOP_MATRIX_DIR']
args = sys.argv[1:]
def put(name, text, how='a'):
    with open(os.path.join(DIR, name), how) as f:
        f.write(text)
mode = open(os.path.join(DIR, 'mode')).read().strip()
put('pids', '%d\n' % os.getpid())
def child():
    # A tool's process leaves for a session of its own (setsid): the group of the process does not hold it.
    code = 'import time; time.sleep(60)'
    if mode == 'tool':
        code = (
            "import os, subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
            "with open(os.path.join(os.environ['STOP_MATRIX_DIR'], 'pids'), 'a') as f:\n"
            "    f.write(str(g.pid) + '\\n')\n"
            "time.sleep(60)\n"
        )
    c = subprocess.Popen([sys.executable, '-c', code], start_new_session=mode == 'tool')
    put('pids', '%d\n' % c.pid)
def wait():
    child()
    put('reached', mode, 'w')
    end = time.time() + 60
    while time.time() < end:
        if mode == 'streaming':
            say('tick')  # it goes on writing: nothing of it may reach the turn once the stop is asked
        time.sleep(0.05)
    sys.exit(0)
def emit(event):
    print(json.dumps(event), flush=True)
'''

_BODY = r'''
if mode == 'blocked':
    wait()  # its message is never read
text = sys.stdin.read()
put('turns.jsonl', json.dumps({'args': args, 'sid': SID, 'size': len(text)}) + '\n')
if mode == 'silent':
    wait()
opening()
if mode == 'streaming':
    say('half an answer')
    wait()
if mode == 'slow':  # streams, then answers once there is a ``go`` file (a reply nothing stops)
    say('half an answer')
    put('reached', mode, 'w')
    while not os.path.exists(os.path.join(DIR, 'go')):
        time.sleep(0.02)
if mode == 'tool':
    tool()
    wait()
answer('answered: ' + text[:20])
if mode == 'done':
    wait()
'''

_CODEX = r'''
SID = args[args.index('resume') + 1] if 'resume' in args else 'thread-1'
def opening():
    emit({'type': 'thread.started', 'thread_id': SID})
    emit({'type': 'turn.started'})
def say(t):
    emit({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': t}})
def tool():
    emit({'type': 'item.started', 'item': {'id': 'i2', 'type': 'command_execution', 'command': 'sleep 60',
          'aggregated_output': '', 'status': 'in_progress'}})
def answer(t):
    say(t)
    emit({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0,
          'cache_write_input_tokens': 0, 'output_tokens': 1, 'reasoning_output_tokens': 0}})
'''

_OMP = r'''
SID = args[args.index('--resume') + 1] if '--resume' in args else 'omp-1'
USAGE = {'input': 10, 'output': 1, 'cacheRead': 4, 'cacheWrite': 2}
def opening():
    emit({'type': 'session', 'version': 3, 'id': SID, 'cwd': os.getcwd()})
def message(stop, content):
    emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': stop, 'model': 'm', 'usage': USAGE,
          'content': content}})
def say(t):
    message('toolUse', [{'type': 'text', 'text': t}])
def tool():
    message('toolUse', [{'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'sleep 60'}}])
def answer(t):
    message('stop', [{'type': 'text', 'text': t}])
    emit({'type': 'agent_end', 'messages': [], 'isTerminal': True, 'yielded': True})
'''

_PI = r'''
directory = args[args.index('--session-dir') + 1]
if '--session' in args:
    SID = json.loads(open(args[args.index('--session') + 1]).readline())['id']
else:
    SID = 'pi-1'
    with open(os.path.join(directory, SID + '.jsonl'), 'w') as f:
        f.write(json.dumps({'type': 'session', 'id': SID}) + '\n')
USAGE = {'input': 10, 'output': 1, 'cacheRead': 4, 'cacheWrite': 2}
def opening():
    emit({'type': 'session', 'id': SID, 'cwd': os.getcwd()})
def message(stop, content):
    emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': stop, 'model': 'm', 'usage': USAGE,
          'content': content}})
def say(t):
    message('toolUse', [{'type': 'text', 'text': t}])
def tool():
    message('toolUse', [{'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'sleep 60'}}])
def answer(t):
    message('stop', [{'type': 'text', 'text': t}])
    emit({'type': 'agent_settled', 'aborted': False})
'''

_DSH = r'''
SID = args[args.index('--session-id') + 1] if '--session-id' in args else 'dsh-1'
def opening():
    emit({'type': 'session', 'sessionId': SID, 'cwd': os.getcwd()})
def say(t):
    emit({'type': 'text', 'text': t})
def tool():
    emit({'type': 'tool_call', 'callId': 'c1', 'tool': 'bash', 'input': {'command': 'sleep 60'}})
def answer(t):
    emit({'type': 'status', 'phase': 'step_end', 'turn': 1, 'step': 1,
          'usage': {'inputTokens': 10, 'outputTokens': 1, 'cacheReadTokens': 0, 'cacheWriteTokens': 0}})
    emit({'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}})
    emit({'type': 'final', 'text': t})
'''


def _per_turn_parts(tmp_path: Path, runtime: str) -> tuple[Any, list[str], dict[str, str], Path, Any]:
    """The runtime's resident session, its command, its environment, the stand-in's directory, and the launch
    the command was made from."""
    directory = tmp_path / "matrix"
    directory.mkdir(exist_ok=True)
    body = {"codex": _CODEX, "omp": _OMP, "pi": _PI, "dsh": _DSH}[runtime]
    executable = fake_claude(tmp_path / runtime, _PRELUDE + body + _BODY)
    if runtime == "codex":
        from test_codex_runtime import CODEX, _launch, _resident_command

        launch = _launch(executable=executable)
        command, session = _resident_command(tmp_path / "runtime", executable, "prompt"), \
            CODEX.resident_session("manager")
    elif runtime == "omp":
        from test_omp_runtime import OMP, _launch, _resident_command

        launch = _launch(executable=executable)
        command, session = _resident_command(tmp_path / "runtime", executable, "prompt"), \
            OMP.resident_session("manager")
    elif runtime == "pi":
        from test_pi_runtime import PI, _launch, _provider

        prompt = tmp_path / ".local" / "prompts" / "prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("Looper prompt")
        launch = _launch(_provider())
        command, session = PI.resident_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch), \
            PI.resident_session("manager")
    else:
        from test_dsh_runtime import _run_launch, _run_prompt_file, runtimes

        dsh = runtimes.adapter("dsh")
        launch = _run_launch(tmp_path, role="manager")
        command = dsh.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)
        session = dsh.resident_session("manager")
    return session, command, {**launch.environment(), "STOP_MATRIX_DIR": str(directory)}, directory, launch


def _per_turn(tmp_path: Path, runtime: str) -> tuple[Any, list[str], dict[str, str], Path]:
    """The runtime's resident session, its command, its environment, and the stand-in's directory."""
    return _per_turn_parts(tmp_path, runtime)[:4]


def _pids(directory: Path) -> list[int]:
    path = directory / "pids"
    return [int(line) for line in path.read_text().split()] if path.exists() else []


# What the stand-in does with each user message is told by the message, ``mode:<window>``, or by a ``mode`` file
# (the Manager's message is its own text): ``obey`` after the window makes it
# answer an interrupt of a running turn (a control response, then the turn's error result; the tool it started,
# a ``setsid`` sleeper, runs on: an obeyed stop signals nothing); else it is deaf to the interrupt. A ``stall`` file
# makes it stop reading its input after the message it is answering.
_CLAUDE = r"""
import json, os, subprocess, sys, threading, time
DIR = os.environ['STOP_MATRIX_DIR']
def put(name, text, how='a'):
    with open(os.path.join(DIR, name), how) as f:
        f.write(text)
def out(event):
    print(json.dumps(event), flush=True)
put('pids', '%d\n' % os.getpid())
state = {'running': False, 'tool': None, 'obey': False}
def child():
    c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)  # setsid
    put('pids', '%d\n' % c.pid)
    return c
def result(text, ok=True):
    state['running'] = False
    out({'type': 'result', 'subtype': 'success' if ok else 'error_during_execution', 'is_error': not ok,
         'result': text, 'total_cost_usd': 0.01, 'session_id': 's1', 'num_turns': 1})
def run(text):
    # What to do is told by a ``mode`` file in the directory, else by the message: ``mode:<window> [obey]``.
    path = os.path.join(DIR, 'mode')
    spec = open(path).read() if os.path.exists(path) else text.rpartition('mode:')[2] if 'mode:' in text else 'answer'
    words = spec.split() or ['answer']
    mode = words[0]
    state['obey'], state['running'] = 'obey' in words, True
    put('turns.jsonl', json.dumps({'mode': mode, 'size': len(text)}) + '\n')
    if mode == 'silent':
        put('reached', mode, 'w')
        return
    out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
    if mode == 'streaming':
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta',
             'delta': {'type': 'text_delta', 'text': 'half an answer'}}})
        put('reached', mode, 'w')
        return
    if mode == 'tool':
        out({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
             {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'sleep 60'}}]}})
        state['tool'] = child()
        put('reached', mode, 'w')
        return
    out({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'answered'}]}})
    result('answered ' + mode)
for raw in sys.stdin:
    message = json.loads(raw)
    if message['type'] == 'control_request':
        if state['obey']:
            out({'type': 'control_response', 'response': {'subtype': 'success',
                 'request_id': message['request_id']}})
            if state['running']:
                for _ in range(5):  # it writes on for a moment after it answered: none of it reaches the turn
                    out({'type': 'stream_event', 'event': {'type': 'content_block_delta',
                         'delta': {'type': 'text_delta', 'text': 'late text'}}})
                    time.sleep(0.02)
                result('', ok=False)
        continue
    run(message['message']['content'])
    if os.path.exists(os.path.join(DIR, 'stall')):
        put('reached', 'blocked', 'w')
        time.sleep(60)
"""
