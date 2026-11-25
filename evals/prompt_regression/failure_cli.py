"""Inject fixture runtime failures, then execute the requested CLI unchanged.

The error attempts consume no model calls. Every attempt after the configured
errors, and every other role, runs the delegated executable with its original
arguments. This wrapper never generates a successful model answer.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import sys
import uuid


def inject(script, prompt, directory):
    role = ('reviewer' if re.search(r'^The step under review:', prompt, re.M) else
            'worker' if re.search(r'^Your step:', prompt, re.M) else None)
    errors = (script.get('_engine_failures') or {}).get(role, [])
    if not errors:
        return False
    state_path = directory / '.engine-failure-fixture.json'
    with state_path.open('a+', encoding='utf-8') as state_file:
        fcntl.flock(state_file, fcntl.LOCK_EX)
        state_file.seek(0)
        text = state_file.read()
        state = json.loads(text) if text else {}
        attempt = state.get(role, 0)
        if attempt >= len(errors):
            return False
        state[role] = attempt + 1
        state_file.seek(0)
        state_file.truncate()
        json.dump(state, state_file)
    if role == 'worker' and attempt == 0 and script.get('_engine_residual'):
        (directory / 'receipt.txt').write_text('applied once\n', encoding='utf-8')
    session = str(uuid.uuid4())
    for event in ({'type': 'system', 'subtype': 'init', 'session_id': session},
                  {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                   'session_id': session, 'api_error': errors[attempt],
                   'api_error_status': 401 if errors[attempt] == 'authentication_error' else 503,
                   'api_error_type': 'authentication_error' if errors[attempt] == 'authentication_error' else 'api_error',
                   'num_turns': 1, 'total_cost_usd': 0, 'result': 'Synthetic runtime failure'}):
        print(json.dumps(event), flush=True)
    return True


def main(argv):
    scenarios_path, raw_delegate, *args = argv
    scenarios = json.loads(Path(scenarios_path).read_text(encoding='utf-8'))
    directory = Path.cwd()
    script = scenarios.get(os.path.realpath(directory), {})
    prompt = ''
    for flag in ('--append-system-prompt-file', '--system-prompt-file'):
        if flag in args:
            prompt = Path(args[args.index(flag) + 1]).read_text(encoding='utf-8')
            break
    if script and inject(script, prompt, directory):
        return 0
    delegate = json.loads(raw_delegate)
    os.execv(delegate[0], [*delegate, *args])


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
