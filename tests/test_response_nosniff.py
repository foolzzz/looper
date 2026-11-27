"""JSON and SSE responses say they are not to be sniffed; the ACP preflight runs its probe isolated."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import http_server
from looper.agents.runtimes import acp_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
NOSNIFF = "X-Content-Type-Options: nosniff"


def test_json_responses_carry_nosniff_with_and_without_extra_headers() -> None:
    for headers in (None, {"Cache-Control": "no-store"}):
        head = http_server._json_response("404 Not Found", {"status": "error"}, headers=headers).split(b"\r\n\r\n")[0]
        assert NOSNIFF.encode() in head.split(b"\r\n")
        assert head.count(b"X-Content-Type-Options") == 1


def _stream_heads(source: str) -> list[tuple[int, str]]:
    """(line, text) of each hand-written response head for a JSON or event-stream body."""
    heads = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
        else:
            continue
        if text.startswith("HTTP/1.1 ") and ("text/event-stream" in text or "application/json" in text):
            heads.append((node.lineno, text))
    return heads


def test_the_head_scan_finds_a_head_without_nosniff() -> None:
    assert _stream_heads('x = b"HTTP/1.1 200 OK\\r\\nContent-Type: text/event-stream\\r\\n\\r\\n"') == [
        (1, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")]
    assert not _stream_heads('x = "HTTP/1.1 200 OK\\r\\nContent-Type: text/html\\r\\n"')
    assert _stream_heads('x = f"HTTP/1.1 {s}\\r\\nContent-Type: application/json\\r\\n" "A: b\\r\\n"')


def test_every_hand_written_json_and_sse_head_carries_nosniff() -> None:
    found, missing = 0, {}
    for path in sorted((REPO_ROOT / "looper").rglob("*.py")):
        for line, text in _stream_heads(path.read_text()):
            found += 1
            if NOSNIFF not in text:
                missing[f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"] = text.split("\r\n")[1]
    assert found >= 4  # the config export, the chat stream, the activity stream and the operator chat stream
    assert not missing, f"send {NOSNIFF} on JSON and event-stream responses: {missing}"


def test_the_acp_preflight_probe_runs_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(acp_policy.subprocess, "run", run)
    policy = SimpleNamespace(scratch=tmp_path, wrap=lambda command: command, environment=lambda env: dict(env))
    assert acp_policy._preflight(policy) is False
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == sys.executable and Path(argv[0]).is_absolute()
    assert argv[1:3] == ["-I", "-c"]
    json.loads(argv[-1])
