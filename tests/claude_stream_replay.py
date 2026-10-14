"""A stand-in resident ``claude`` that replays a recorded Claude Code session turn by turn.

The ``background_*`` fixtures in ``fixtures/claude_stream`` are Claude Code
2.1.285 (haiku) sessions run with Looper's resident flags and
``--replay-user-messages``, trimmed to public-safe fields. A ``{"stdin":
<uuid>}`` line marks where the recording wrote a user message with that uuid.
The stand-in reads one message from its stdin there and replays the events up
to the next mark, with the recorded message's uuid (and the text of its
replay) swapped for the message it was sent. Like Claude, it then idles until
its stdin closes.
"""

from __future__ import annotations

from pathlib import Path

from fake_claude import fake_claude

FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"

_REPLAY = """\
import json as _json, sys as _sys, uuid as _uuid
_mapped = {{}}   # a recorded message's uuid -> the uuid of the message this stand-in was sent
_sent_text = {{}}  # that uuid -> the text it was sent
def _swap(value):
    if isinstance(value, str):
        return _mapped.get(value, value)
    if isinstance(value, list):
        return [_swap(item) for item in value]
    if isinstance(value, dict):
        return {{key: _swap(item) for key, item in value.items()}}
    return value
for _line in open({path!r}, encoding='utf-8'):
    _event = _json.loads(_line)
    if 'stdin' in _event:
        _received = _sys.stdin.readline()
        if not _received:
            _sys.exit(0)
        _message = _json.loads(_received)
        _mine = _message.get('uuid') or str(_uuid.uuid4())  # Claude names a message without one itself
        _mapped[_event['stdin']] = _mine
        _sent_text[_mine] = _message['message']['content']
        continue
    _event = _swap(_event)
    if _event.get('isReplay') and _event.get('uuid') in _sent_text:
        _event['message']['content'] = _sent_text[_event['uuid']]
    print(_json.dumps(_event), flush=True)
_sys.stdin.read()
"""


def replay_body(fixture: str) -> str:
    """Python statements that replay ``fixtures/claude_stream/<fixture>.jsonl`` on stdout."""
    return _REPLAY.format(path=str(FIXTURES / f"{fixture}.jsonl"))


def replay_script(directory: Path, fixture: str) -> str:
    """An executable stand-in in ``directory`` that replays ``fixture``; returns its path."""
    return fake_claude(directory / f"replay-{fixture}", replay_body(fixture))
