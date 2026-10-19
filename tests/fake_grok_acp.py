"""A stand-in ``grok agent stdio``: the ACP side of Grok 1.0.62 as ``.lead`` probes recorded it.

The body (run through ``fake_claude``) keeps sessions in ``$GROK_HOME/sessions/<cwd>/<id>/summary.json`` across
processes and writes one line per start to ``<runtime_root>/.local/grok/acp-argv.jsonl``. What a prompt does is
chosen by its text (the stand-in is told, not guessed):

- ``TOOL``: one response that runs a shell command, then one that answers;
- ``HANG``: thinks, then waits for ``session/cancel`` and ends with ``cancelled``;
- ``CRASH``: streams a chunk, then dies;
- ``GARBAGE``: lines that are not JSON objects between the notifications;
- ``ASK``: asks the client for a permission (a request of the agent's own) and answers with the outcome;
- ``ERROR``: answers ``session/prompt`` with a JSON-RPC error;
- anything else: answers with the first line of Looper's prompt (from the rules file), the message and the turn.

``FAKE_ACP_DEAD`` makes the process exit at once (a Grok with no ``agent stdio``), ``FAKE_ACP_NO_LOAD`` leaves
``loadSession`` out of ``initialize``, ``FAKE_ACP_SILENT`` never answers ``initialize``.
"""

FAKE_ACP = """\
import json, os, sys, time, urllib.parse
args = sys.argv[1:]
home = os.environ['GROK_HOME']
root = os.path.dirname(os.path.dirname(home))
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

def prompt(request, text):
    state['turns'] += 1
    save()
    if 'ERROR' in text:
        out({'id': request, 'error': {'code': -32603, 'message': 'model unavailable'}})
        return
    if 'GARBAGE' in text:
        print('this is not json', flush=True)
        print('[1, 2, 3]', flush=True)
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
    if 'CRASH' in text:
        chunk('about to ')
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
    if method == 'initialize':
        if os.environ.get('FAKE_ACP_SILENT'):
            time.sleep(600)
        capabilities = {} if os.environ.get('FAKE_ACP_NO_LOAD') else {'loadSession': True}
        out({'id': request, 'result': {'protocolVersion': 1, 'agentCapabilities': capabilities}})
    elif method == 'session/new':
        os.makedirs(group, exist_ok=True)
        state['session'] = '0199a000-0000-7000-8000-%012d' % (len(os.listdir(group)) + 1)
        save()
        out({'id': request, 'result': {'sessionId': state['session']}})
    elif method == 'session/load':
        session = message['params']['sessionId']
        path = os.path.join(group, session, 'summary.json')
        if not os.path.exists(path):
            out({'id': request, 'error': {'code': -32603, 'message': 'Path not found.'}})
            continue
        state['session'], state['turns'] = session, json.load(open(path))['turns']
        out({'id': request, 'result': {}})
    elif method == 'session/prompt':
        prompt(request, message['params']['prompt'][0]['text'])
"""
