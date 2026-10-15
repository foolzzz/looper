"""A stand-in Manager ``claude`` that calls Looper's tools over MCP, and the endpoint it reaches.

``mcp_manager_claude`` writes a fake ``claude`` that reads the ``--mcp-config``
the Manager launched it with, starts the ``looper`` server from it (``looper
mcp-server``) and, for each user turn, makes that turn's scripted calls. In a
call's arguments, ``<confirmation>`` stands for the confirmation id the note
before the message names, and ``<task id>`` for the last task id an answer
gave. Its reply is the calls' answers as JSON (a call Looper did not answer as
its error text). It saves its argv
(``argv.json``), the tools it was given (``tools.json``) and each user turn
(``turns.jsonl``) in its directory.

``manager_endpoint`` gives a Manager its ``ManagerOps`` and serves them on a
free local port, as ``looper serve`` does.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fake_claude import fake_claude

_BODY = """\
import json, os, re, subprocess, sys
args = sys.argv[1:]
open('argv.json', 'w').write(json.dumps(args))
config = json.load(open(args[args.index('--mcp-config') + 1]))['mcpServers']['looper']
server = subprocess.Popen([config['command'], *config['args']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          env={**os.environ, **config['env']}, text=True)
ids = iter(range(1, 10000))
def rpc(method, params):
    server.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': next(ids), 'method': method, 'params': params}) + '\\n')
    server.stdin.flush()
    return json.loads(server.stdout.readline())
rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'stand-in'}})
listed = rpc('tools/list', {})
open('tools.json', 'w').write(json.dumps([tool['name'] for tool in (listed.get('result') or {}).get('tools', [])]))
last_task = ''
for turn, line in zip(TURNS, sys.stdin):
    content = json.loads(line)['message']['content']
    open('turns.jsonl', 'a').write(json.dumps(content) + '\\n')
    named = re.search(r'confirmation_id (c-[0-9a-f]+)', content)
    answers = []
    for name, arguments in turn:
        text = json.dumps(arguments).replace('<confirmation>', named.group(1) if named else '')
        result = rpc('tools/call', {'name': name, 'arguments': json.loads(text.replace('<task id>', last_task))})
        text = result['result']['content'][0]['text']
        try:
            answer = json.loads(text)
        except ValueError:  # Looper did not answer: the forwarder says why
            answer = {'status': 'error', 'text': text}
        last_task = ((answer.get('result') or {}).get('task_id') if isinstance(answer.get('result'), dict)
                     else None) or last_task
        answers.append(answer)
    reply = json.dumps(answers, ensure_ascii=False)
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)
    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': reply}]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'sess-1',
                      'result': reply, 'total_cost_usd': 0.0}), flush=True)
"""


def mcp_manager_claude(directory: Path, turns: list[list[tuple[str, dict[str, Any]]]]) -> str:
    """A stand-in ``claude`` in ``directory`` whose user turns make ``turns``' calls, in order."""
    return fake_claude(directory / "fake-manager-claude", f"TURNS = {turns!r}\n{_BODY}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def manager_endpoint(manager: Any, bus: Any, task_router: Any, root: Path) -> AsyncIterator[Any]:
    """``manager.ops`` acting on ``bus`` and ``task_router``, behind an ``AgentHTTPServer`` on a free port; yields
    the ops."""
    from looper.agents.channels.web import WebChannelAdapter
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.ops.service import ManagerOps, Platform

    port = _free_port()
    ops = ManagerOps(Platform(bus=bus, task_router=task_router, manager=manager), url=f"http://127.0.0.1:{port}")
    manager.ops = ops
    server = AgentHTTPServer(host="127.0.0.1", port=port, web_adapter=WebChannelAdapter(), task_router=task_router,
                             supervisor=SimpleNamespace(bus=bus), manager=manager, runtime_root=root,
                             manager_ops=ops)
    await server.start()
    try:
        yield ops
    finally:
        await server.stop()
