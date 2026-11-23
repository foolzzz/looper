"""What a Claude Code stream shows when a run uses a project skill, against the real ``claude`` binary.

No model is involved: the CLI talks to a scripted Messages API on the loopback interface, with a throwaway
``HOME`` and ``CLAUDE_CONFIG_DIR``. The skill plugin is the one a Worker launch builds (``library.skills_plugin``;
project skills load as ``project:<name>``). The usage query (``skill_usage``) counts a skill as invoked from the
``Skill`` tool call these tests pin down.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.installed

SKILL = "probe-skill"
MARKER = "MARKER-LINE-7731"
SKILL_FILES = [{
    "path": "SKILL.md", "executable": False,
    "content": f"---\nname: {SKILL}\ndescription: Probe skill for the stream check.\n---\n{MARKER} follow this.\n",
}]


def real_claude() -> str:
    """The real binary, not a terminal shim on PATH."""
    for candidate in (os.environ.get("LOOPER_TEST_CLAUDE"), str(Path.home() / ".local/bin/claude")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("claude")
    if found:
        return found
    pytest.skip("not-run: claude CLI not found")


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _message(blocks: list[dict[str, Any]], stop: str) -> bytes:
    events: list[dict[str, Any]] = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [],
        "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}]
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            events += [{"type": "content_block_start", "index": index, "content_block": {**block, "input": {}}},
                       {"type": "content_block_delta", "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}]
        else:
            events += [{"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": index,
                        "delta": {"type": "text_delta", "text": block["text"]}}]
        events.append({"type": "content_block_stop", "index": index})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 4}}, {"type": "message_stop"}]
    return _sse(events)


class StandIn:
    """A loopback Anthropic Messages API: the first model request asks for the Skill tool, the next ends the turn."""

    def __init__(self, skill_arg: str) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}")
                stand_in.requests.append((self.path, body))
                if self.path.split("?")[0].endswith("/count_tokens"):
                    out, kind = b'{"input_tokens":10}', "application/json"
                else:
                    answered = any(isinstance(m.get("content"), list) and any(
                        b.get("type") == "tool_result" for b in m["content"]) for m in body.get("messages", []))
                    if body.get("tools") and not answered:
                        out = _message([{"type": "tool_use", "id": "toolu_01", "name": "Skill",
                                         "input": {"skill": skill_arg}}], "tool_use")
                    else:
                        out = _message([{"type": "text", "text": "done"}], "end_turn")
                    kind = "text/event-stream"
                self.send_response(200)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


@contextmanager
def stand_in_for(skill_arg: str) -> Iterator[StandIn]:
    stand_in = StandIn(skill_arg)
    thread = threading.Thread(target=stand_in.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stand_in
    finally:
        stand_in.server.shutdown()
        stand_in.server.server_close()


def run_claude(skill_arg: str) -> tuple[list[dict[str, Any]], StandIn, subprocess.CompletedProcess[str], str]:
    """One real CLI run with the project plugin a Worker launch would build; returns its stream and the stand-in."""
    from looper.agents import library
    from looper.agents.claude_cli import isolation_args, library_args

    binary = real_claude()
    with tempfile.TemporaryDirectory() as scratch, stand_in_for(skill_arg) as stand_in:
        root, home, cwd = Path(scratch, "runtime"), Path(scratch, "home"), Path(scratch, "cwd")
        for directory in (root, home / "config", cwd):
            directory.mkdir(parents=True)
        files = [{**f, "sha256": hashlib.sha256(f["content"].encode()).hexdigest()} for f in SKILL_FILES]
        item = library.LoadedItem(library.ItemKind.SKILL, SKILL, library.item_sha256(files),
                                  library.PROJECT_SKILLS_PLUGIN, "project")
        library.materialize_item(root, item, files)
        loaded = library.AgentLibrary((item,), (str(library.skills_plugin(root, [item], library.PROJECT_SKILLS_PLUGIN)),))
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "config"),
            "ANTHROPIC_BASE_URL": stand_in.url, "ANTHROPIC_API_KEY": "sk-ant-test-key-0000000000000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
            # Anything that still tries to leave the machine fails at once instead of reaching a network.
            "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1",
        }
        command = [binary, "-p", "use the skill", "--output-format", "stream-json", "--verbose",
                   *isolation_args(str(cwd)), "--dangerously-skip-permissions", *library_args(loaded)]
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, env=env).stdout.strip()
        done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL)
        stream = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
        return stream, stand_in, done, version


def _skill_call(stream: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    for index, event in enumerate(stream):
        if event["type"] != "assistant":
            continue
        for block in event["message"]["content"]:
            if block["type"] == "tool_use" and block["name"] == "Skill":
                return block, index
    raise AssertionError("no Skill tool_use in the stream")


@pytest.mark.parametrize("name_form,argument", [("qualified", f"project:{SKILL}"), ("bare", SKILL)])
def test_a_skill_call_shows_in_the_stream_and_loads_the_body(name_form: str, argument: str) -> None:
    stream, stand_in, done, version = run_claude(argument)
    assert done.returncode == 0, done.stderr[-400:]

    # The call is an assistant tool_use block, whichever name the model used; the CLI resolves both.
    block, index = _skill_call(stream)
    assert block["input"] == {"skill": argument}
    result = next(b for e in stream[index + 1:] if e["type"] == "user" and isinstance(e["message"]["content"], list)
                  for b in e["message"]["content"] if b.get("type") == "tool_result" and b["tool_use_id"] == block["id"])
    assert not result.get("is_error")
    # The skill body arrives in the next user event; the tool_result itself only says the skill is launching.
    following = json.dumps([e for e in stream[index + 1:] if e["type"] == "user"])
    assert MARKER in following, f"Claude Code {version} did not load the skill body for {argument!r}"

    # The init event lists the skill under the plugin prefix, and the first request lists it to the model by that name.
    init = next(e for e in stream if e["type"] == "system" and e.get("subtype") == "init")
    assert f"project:{SKILL}" in init["skills"] and SKILL not in init["skills"]
    first = json.dumps(stand_in.requests[0][1])
    assert f"- project:{SKILL}: Probe skill for the stream check." in first.replace("\\n", "\n")

    # Nothing but the stand-in was asked: every request reached the loopback server, and none failed at the proxy.
    assert all(path.startswith("/v1/messages") for path, _ in stand_in.requests)
    assert "ECONNREFUSED" not in done.stderr and "proxy" not in done.stderr.lower()
    print(f"claude {version}: {name_form} name {argument!r} resolved")
