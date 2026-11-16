"""A stand-in ``grok agent stdio``: the ACP side of Grok 1.0.62 as ``.lead`` probes recorded it.

The body (run through ``fake_claude``) keeps sessions in ``$GROK_HOME/sessions/<cwd>/<id>/summary.json`` across
processes and writes one line per start to ``<runtime_root>/.local/grok/acp-argv.jsonl``. What a prompt does is
chosen by its text (the stand-in is told, not guessed):

- ``TOOL``: one response that runs a shell command, then one that answers;
- ``SLEEPER``: a tool call that starts a ``sleep`` in a session of its own (``setsid``), writes its pid to
  ``sleeper.pid`` beside ``acp-argv.jsonl``, then waits for ``session/cancel`` as ``HANG`` does (a cancel it
  ignores leaves the sleeper running, as a real tool Grok did not wait for would);
- ``LOOP``: model responses in a row (a tool call each) until ``session/cancel``, which it logs to ``cancels.log``;
- ``ORPHAN``: starts a long ``sleep`` that inherits stdout, then dies;
- ``HANG``: thinks, then waits for ``session/cancel`` and ends with ``cancelled``; ``QUIET`` the same, writing
  nothing first;
- ``DEAF``: thinks, then ignores ``session/cancel`` (never answers the prompt);
- ``CRASH``: streams a chunk, then dies;
- ``BADRESP``: responses to the prompt with neither ``result`` nor ``error``, or both, then the answer;
- ``GARBAGE``: lines that are not JSON objects between the notifications;
- ``ASK``: asks the client for a permission (a request of the agent's own) and answers with the outcome;
- ``ERROR``: answers ``session/prompt`` with a JSON-RPC error;
- ``STEER_TOOL``, ``STEER_REFUSE``, ``STEER_FINAL``, ``STEER_LATE``, ``STEER_EMPTY`` and ``STEER_TOOL_ONLY``:
  pause at explicit test gates to separate
  interjection acceptance from a tool result and the start or continuation of model output;
- anything else: answers with the first line of Looper's prompt (from the rules file), the message and the turn.

``FAKE_ACP_DEAD`` makes the process exit at once (a Grok with no ``agent stdio``), ``FAKE_ACP_NO_LOAD`` leaves
``loadSession`` out of ``initialize``, ``FAKE_ACP_SILENT`` never answers ``initialize``. A ``stall`` file beside
``acp-argv.jsonl`` makes it read nothing more once it has a session.
``FAKE_ACP_VERSION`` and ``FAKE_ACP_INTERJECT_CODE`` control the no-model capability probe.
"""

FAKE_ACP = """\
import json, os, sys, time, urllib.parse
args = sys.argv[1:]
if '--version' in args:
    print(os.environ.get('FAKE_ACP_VERSION', 'grok 1.0.77'))
    sys.exit(0)
home = os.environ['GROK_HOME']
root = os.environ.get('FAKE_ACP_ROOT', os.path.dirname(os.path.dirname(home)))
open(os.path.join(root, 'acp-argv.jsonl'), 'a').write(json.dumps(
    {'home': home, 'user_home': os.environ['HOME'], 'args': args, 'pid': os.getpid(), 'cwd': os.getcwd(),
     'key': os.environ.get('XAI_API_KEY')}) + '\\n')
if os.environ.get('FAKE_ACP_DEAD'):
    print('error: unrecognized subcommand', file=sys.stderr)
    sys.exit(2)
assert 'agent' in args and args[-1] == 'stdio', args
group = os.path.join(home, 'sessions', urllib.parse.quote(os.getcwd(), safe=''))
state = {'session': None, 'turns': 0}

def out(message):
    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)

def update(kind, **fields):
    out({'method': 'session/update', 'params': {'sessionId': state['session'], 'update': {'sessionUpdate': kind, **fields}}})

def extension(kind, **fields):
    out({'method': '_x.ai/session_notification', 'params': {'sessionId': state['session'], 'update': {'sessionUpdate': kind, **fields}}})

def chunk(text, thought=False):
    update('agent_thought_chunk' if thought else 'agent_message_chunk', content={'type': 'text', 'text': text})

def completed(inp, cached, out_tokens):
    extension('response_completed', usage={'input_tokens': inp, 'output_tokens': out_tokens,
              'cache_read_input_tokens': cached, 'cache_creation_input_tokens': 0, 'reasoning_tokens': 1})

def save():
    path = os.path.join(group, state['session'])
    os.makedirs(path, exist_ok=True)
    json.dump({'turns': state['turns']}, open(os.path.join(path, 'summary.json'), 'w'))

def rules_line():
    text = open(os.path.join(home, 'rules', 'looper.md')).read()
    return text.partition('\\n\\n---\\n\\n')[2].splitlines()[0]

def usage(calls, inp, cached, out_tokens, ticks):
    row = {'inputTokens': inp, 'outputTokens': out_tokens, 'totalTokens': inp + out_tokens, 'cachedReadTokens': cached,
           'cacheCreationTokens': 0, 'reasoningTokens': 1, 'modelCalls': calls, 'apiDurationMs': 100, 'costUsdTicks': ticks}
    return {**row, 'modelUsage': {'grok-4.7-build': row}, 'numTurns': calls}

def finish(request, reason, tokens):
    out({'id': request, 'result': {'stopReason': reason, '_meta': {'sessionId': state['session'], 'usage': tokens}}})

def stall():
    if os.path.exists(os.path.join(root, 'stall')):  # reads no more: a prompt written to it blocks
        time.sleep(600)

def prompt(request, text):
    text = os.environ.get('FAKE_ACP_STEER_SCENARIO', text)
    state['turns'] += 1
    save()
    if text.startswith('STEER_'):
        import select
        # A fake tool remains in progress while interjections are read. Explicit
        # test gates separate acceptance, tool completion and the next response.
        tool = text not in ('STEER_FINAL', 'STEER_LATE')
        if tool or text == 'STEER_LATE':
            completed(10, 0, 1)
            update('tool_call', toolCallId='steer-tool', title='run_terminal_command', rawInput={'command': 'wait for input'})
            update('tool_call_update', toolCallId='steer-tool', status='in_progress')
            if text == 'STEER_LATE':
                update('tool_call_update', toolCallId='steer-tool', status='completed',
                       rawOutput={'type': 'Bash', 'output': [], 'exit_code': 0})
                chunk('last response already started')
        else:
            chunk(os.environ.get('FAKE_ACP_STEER_RESULT', 'last response already started'))
        open(os.path.join(root, 'steer-ready'), 'w').close()
        accepted = False
        while not accepted:
            pending = json.loads(sys.stdin.readline())
            open(os.path.join(root, 'steer-requests.jsonl'), 'a').write(json.dumps(pending) + '\\n')
            if pending['method'] == 'session/cancel':
                finish(request, 'cancelled', usage(1, 100, 0, 3, 50000000))
                return
            assert pending['method'] == '_x.ai/interject'
            params = pending['params']
            assert params['sessionId'] == state['session'] and isinstance(params['text'], str)
            if text == 'STEER_REFUSE':
                out({'id': pending['id'], 'error': {'code': -32602, 'message': 'interjection refused'}})
            else:
                result = {'status': 'queued'}
                if os.environ.get('FAKE_ACP_INTERJECT_FLAT') != '1':
                    result = {'result': result}
                out({'id': pending['id'], 'result': result})
                out({'method': '_x.ai/session/interjection', 'params': params})
            accepted = True
        open(os.path.join(root, 'steer-accepted'), 'w').close()
        while not os.path.exists(os.path.join(root, 'steer-tool.finish')):
            if select.select([sys.stdin], [], [], 0.01)[0]:
                line = sys.stdin.readline()
                if not line:
                    sys.exit(0)
                pending = json.loads(line)
                open(os.path.join(root, 'steer-requests.jsonl'), 'a').write(json.dumps(pending) + '\\n')
                if pending['method'] == 'session/cancel':
                    finish(request, 'cancelled', usage(1, 100, 0, 3, 50000000))
                    return
        if tool:
            update('tool_call_update', toolCallId='steer-tool', status='completed',
                   rawOutput={'type': 'Bash', 'output': [], 'exit_code': 0})
            open(os.path.join(root, 'steer-tool.done'), 'w').close()
            while not os.path.exists(os.path.join(root, 'steer-model.start')):
                time.sleep(0.01)
            if text == 'STEER_EMPTY':
                chunk('')
            elif text == 'STEER_TOOL_ONLY':
                completed(10, 0, 1)
                update('tool_call', toolCallId='next-tool', title='read_file', rawInput={'path': 'example.txt'})
                update('tool_call_update', toolCallId='next-tool', status='completed',
                       rawOutput={'type': 'ReadFile', 'content': 'example'})
            else:
                chunk(os.environ.get('FAKE_ACP_STEER_RESULT', 'next model response'))
        elif text == 'STEER_LATE':
            chunk(' tail of the same response')
        if text != 'STEER_TOOL_ONLY':
            completed(10, 0, 1)
        finish(request, 'end_turn', usage(2 if tool else 1, 100, 0, 3, 50000000))
        return
    if 'ERROR' in text:
        out({'id': request, 'error': {'code': -32603, 'message': 'model unavailable'}})
        return
    if 'BADRESP' in text:  # responses that are neither a result nor an error, or both, before the real one
        print(json.dumps({'jsonrpc': '2.0', 'id': request}), flush=True)
        print(json.dumps({'jsonrpc': '2.0', 'id': request, 'result': {'stopReason': 'end_turn'}, 'error': {'code': 1}}), flush=True)
    if 'GARBAGE' in text:
        print('this is not json', flush=True)
        print('[1, 2, 3]', flush=True)
        print(json.dumps({'id': request, 'result': {'stopReason': 'end_turn'}}), flush=True)  # no "jsonrpc": 2.0
    if 'ORPHAN' in text:
        import subprocess
        subprocess.Popen(['sleep', '20'])
        print('SECRET-STDERR', file=sys.stderr, flush=True)
        os._exit(3)
    if 'SLEEPER' in text:
        import subprocess
        sleeper = subprocess.Popen(['sleep', '60'], start_new_session=True)
        open(os.path.join(root, 'sleeper.pid'), 'w').write(str(sleeper.pid))
        completed(10, 0, 1)
        update('tool_call', toolCallId='s1', title='run_terminal_command', rawInput={'command': 'sleep 60'})
        update('tool_call_update', toolCallId='s1', status='in_progress', rawOutput={'type': 'Bash', 'output': []})
        sys.stdout.flush()
        for line in sys.stdin:
            if json.loads(line).get('method') == 'session/cancel':
                finish(request, 'cancelled', usage(1, 100, 0, 3, 50000000))
                return
        sys.exit(0)
    if 'LOOP' in text:
        for n in range(1, 40):
            completed(10, 0, 1)
            update('tool_call', toolCallId='c%d' % n, title='read_file', rawInput={})
            update('tool_call_update', toolCallId='c%d' % n, status='completed', rawOutput={'type': 'ReadFile'})
            time.sleep(0.05)
            sys.stdout.flush()
        for line in sys.stdin:
            if json.loads(line).get('method') == 'session/cancel':
                open(os.path.join(root, 'cancels.log'), 'a').write('cancel\\n')
                finish(request, 'cancelled', usage(1, 100, 0, 3, 50000000))
                return
    if 'HANG' in text or 'QUIET' in text:
        if 'HANG' in text:
            chunk('thinking...', thought=True)
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get('method') == 'session/cancel':
                finish(request, 'cancelled', usage(1, 100, 0, 3, 50000000))
                return
        sys.exit(0)
    if 'DEAF' in text:
        chunk('thinking...', thought=True)
        for line in sys.stdin:
            pass
        sys.exit(0)
    if 'CRASH' in text:
        chunk('about to ')
        print('SECRET-STDERR', file=sys.stderr, flush=True)
        os._exit(3)
    if 'ASK' in text:
        out({'id': 'perm-1', 'method': 'session/request_permission', 'params': {'sessionId': state['session'], 'toolCall': {'toolCallId': 'x'}, 'options': []}})
        outcome = json.loads(sys.stdin.readline())
        chunk('permission outcome: ' + json.dumps(outcome.get('result')))
        completed(100, 40, 5)
        finish(request, 'end_turn', usage(1, 140, 40, 5, 200000000))
        return
    if 'TOOL' in text:
        chunk('I will run it. ', thought=True)
        completed(100, 40, 20)
        update('tool_call', toolCallId='call-1', title='run_terminal_command', rawInput={'command': 'echo hi'},
               _meta={'x.ai/tool': {'name': 'run_terminal_command', 'kind': 'execute'}})
        update('tool_call_update', toolCallId='call-1', status='in_progress', rawOutput={'type': 'Bash', 'output': []})
        update('tool_call_update', toolCallId='call-1', status='completed', content=[{'type': 'content', 'content': {'type': 'text', 'text': 'hi\\n'}}],
               rawOutput={'type': 'Bash', 'output': [104, 105, 10], 'exit_code': 0, 'command': 'echo hi'})
        chunk('it said hi')
        completed(60, 140, 7)
        finish(request, 'end_turn', usage(2, 340, 180, 27, 400000000))
        return
    answer = f'{rules_line()} | {text} | turn {state["turns"]}'
    for piece in (answer[:6], answer[6:]):
        chunk(piece)
    completed(60, 40, 5)
    finish(request, 'end_turn', usage(1, 100, 40, 5, 100000000))

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method, request = message.get('method'), message.get('id')
    open(os.path.join(root, 'acp-requests.jsonl'), 'a').write(json.dumps(message) + '\\n')
    if method == 'initialize':
        if os.environ.get('FAKE_ACP_SILENT'):
            time.sleep(600)
        capabilities = {} if os.environ.get('FAKE_ACP_NO_LOAD') else {'loadSession': True}
        out({'id': request, 'result': {'protocolVersion': (True if os.environ.get('FAKE_ACP_PROTOCOL') == 'true' else int(os.environ.get('FAKE_ACP_PROTOCOL', 1))), 'agentCapabilities': capabilities}})
    elif method == 'session/new':
        os.makedirs(group, exist_ok=True)
        state['session'] = '0199a000-0000-7000-8000-%012d' % (len(os.listdir(group)) + 1)
        save()
        out({'id': request, 'result': {'sessionId': state['session']}})
        stall()
    elif method == 'session/load':
        session = message['params']['sessionId']
        path = os.path.join(group, session, 'summary.json')
        if os.environ.get('FAKE_ACP_LOAD_ERROR'):
            out({'id': request, 'error': {'code': -32000, 'message': 'SECRET-LOAD-TEXT busy'}})
            continue
        if not os.path.exists(path):
            out({'id': request, 'error': {'code': -32603, 'message': 'Path not found.', 'data': {'code': 'FS_NOT_FOUND'}}})
            continue
        state['session'], state['turns'] = session, json.load(open(path))['turns']
        out({'id': request, 'result': {}})
        stall()
    elif method == 'session/prompt':
        prompt(request, message['params']['prompt'][0]['text'])
    elif method == '_x.ai/interject':
        code = int(os.environ.get('FAKE_ACP_INTERJECT_CODE', '-32602'))
        reason = 'session not found: ' + message['params']['sessionId'] if code == -32602 else None
        out({'id': request, 'error': {'code': code,
             'message': os.environ.get('FAKE_ACP_INTERJECT_MESSAGE', 'Invalid params' if code == -32602 else 'method not found'),
             'data': json.loads(os.environ['FAKE_ACP_INTERJECT_DATA']) if 'FAKE_ACP_INTERJECT_DATA' in os.environ else reason}})
"""
