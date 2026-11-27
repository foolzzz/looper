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


def _constant_text(node: ast.AST) -> str | None:
    """The text of a string or bytes expression built from literals, f-strings (``{}`` for a hole), ``+`` and
    ``sep.join([...])`` of such parts; a part that is not a literal counts as ``{}``. None for anything else.

    Known blind spot, on purpose: ``%`` and ``.format`` formatting are not folded."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant_text(node.left), _constant_text(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join"
            and len(node.args) == 1 and isinstance(node.args[0], (ast.List, ast.Tuple))):
        separator = _constant_text(node.func.value)
        if separator is None:
            return None
        parts = [_constant_text(item) for item in node.args[0].elts]
        return separator.join("{}" if part is None else part for part in parts)
    return None


def _stream_heads(source: str) -> list[tuple[int, str]]:
    """(line, text) of each hand-written response head for a JSON or event-stream body. A head built by ``+`` or
    ``join`` is read whole, from its outermost expression."""
    heads = []

    def visit(node: ast.AST) -> None:
        text = _constant_text(node)
        if text is not None:
            if text.startswith("HTTP/1.1 ") and ("text/event-stream" in text or "application/json" in text):
                heads.append((node.lineno, text))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return heads


def test_the_head_scan_finds_a_head_without_nosniff() -> None:
    assert _stream_heads('x = b"HTTP/1.1 200 OK\\r\\nContent-Type: text/event-stream\\r\\n\\r\\n"') == [
        (1, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")]
    assert not _stream_heads('x = "HTTP/1.1 200 OK\\r\\nContent-Type: text/html\\r\\n"')
    assert _stream_heads('x = f"HTTP/1.1 {s}\\r\\nContent-Type: application/json\\r\\n" "A: b\\r\\n"')


def test_the_head_scan_reads_a_head_built_by_concatenation_or_join() -> None:
    plus = 'x = "HTTP/1.1 200 OK\\r\\n" + "Content-Type: text/event-stream\\r\\n" + "\\r\\n"'
    plus_var = 'x = "HTTP/1.1 200 OK\\r\\n" + ctype + "Content-Type: application/json\\r\\n"'
    join = 'x = "".join(["HTTP/1.1 200 OK\\r\\n", "Content-Type: application/json\\r\\n", "\\r\\n"])'
    join_bytes = 'x = b"\\r\\n".join([b"HTTP/1.1 200 OK", b"Content-Type: text/event-stream", b""])'
    for source in (plus, plus_var, join, join_bytes):
        heads = _stream_heads(source)
        assert len(heads) == 1 and NOSNIFF not in heads[0][1], source
    split = 'x = "HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n" + "X-Content-Type-Options: nosniff\\r\\n"'
    assert [NOSNIFF in text for _, text in _stream_heads(split)] == [True]
    assert not _stream_heads('x = "HTTP/1.1 200 OK\\r\\n" + "Content-Type: text/html\\r\\n"')


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
