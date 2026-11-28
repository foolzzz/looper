"""One started task conversation on an isolated looper serve: send, stream, stop, reload.

The stand-in Claude prints the verified version and one tool, then waits. Stop is the
control request that ends that turn. History is read back from operator_chat.messages.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper import ops_config
from looper.db import projects, tasks
from looper.db.chat import task_thread
from tests.db_support import DSN, fresh_database_without_recording
from tests.fake_claude import fake_claude
from evals.prompt_regression.fake_readback import SOURCE as READBACK_SOURCE
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(
    not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL"
)

TASK_ID = "task-serve-stop"
MESSAGE_ID = "serve-turn-1"
TOOL_ID = "tool-1"
_RESERVED_PORT = 8767
_SECRET = re.compile(r"postgresql://\S+")
_OTHER_CLIS = (
    "codex",
    "omp",
    "pi",
    "dsh",
    "grok",
    "cursor-agent",
    "agent",
    "ttadk",
    "coco",
    "lark-cli",
)
# A resident that emits one tool and then blocks until the stop control request.
_CLAUDE = "exec(%r)\n" % READBACK_SOURCE + r"""
import json, sys
if "--version" in sys.argv:
    print("2.1.332")
    raise SystemExit(0)
if "--input-format" not in sys.argv:
    if "auth" in sys.argv and "--help" in sys.argv:
        print("Usage: claude auth status")
    elif "auth" in sys.argv and "status" in sys.argv:
        print('{"loggedIn":true}')
    raise SystemExit(0)
session = "task-session"
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        continue
    if data.get("type") == "control_request" and data["request"]["subtype"] in READBACK_SUBTYPES:
        kind, body = readback_answer(data["request"], "/nonexistent")
        print(json.dumps({"type": "control_response", "response": {
            "subtype": kind, "request_id": data.get("request_id"), "response": body}}), flush=True)
        continue
    if data.get("type") == "control_request":
        print(json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": data.get("request_id")}}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "result": "stopped",
                          "session_id": session, "total_cost_usd": 0}), flush=True)
        continue
    if data.get("type") != "user":
        continue
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session,
                      **init_fields("/nonexistent")}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "tool-1", "name": "Bash",
        "input": {"command": "cat README.md"}}]}}), flush=True)
"""


def _public(text: str) -> str:
    return _SECRET.sub("postgresql://redacted", text)


def _tail(path: Path, limit: int = 40) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return _public("\n".join(lines[-limit:]))


class _Stream:
    """SSE events for one reply, read on a thread so Stop can run while they arrive."""

    def __init__(self, api: runner.Api, correlation_id: str) -> None:
        self.events: list[tuple[str, dict]] = []
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        request = urllib.request.Request(
            api.base_url + "/api/v1/operator_chat/reply_events",
            data=json.dumps({"correlation_id": correlation_id}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api.token}",
            },
            method="POST",
        )
        self._response = urllib.request.urlopen(request, timeout=90)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        kind = "message"
        try:
            for raw in self._response:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip() or "null")
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        payload = {"value": payload}
                    with self._lock:
                        self.events.append((kind, payload))
        except Exception as exc:
            self.error = exc

    def snapshot(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self.events)

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:
            pass
        self._thread.join(timeout=2)


def _wait(predicate, timeout: float, detail):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError(detail())


def _result(body: dict) -> dict:
    assert body.get("status") == "ok", _public(json.dumps(body)[:500])
    return body["result"]


def _admin_url() -> str:
    target = urlsplit(str(DSN))
    name = unquote(target.path.lstrip("/"))
    if (
        target.scheme != "postgresql"
        or target.hostname not in ("127.0.0.1", "localhost")
        or target.port not in (54331, 5432)
        or target.username != "postgres"
        or name in ("looper", "loopx")
        or target.query
        or target.fragment
    ):
        raise ValueError("requires the dedicated disposable PostgreSQL server")
    require_disposable_database(str(DSN), served=False)
    return str(DSN)


async def _seed(url: str, work_dir: Path) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(
            "INSERT INTO system_settings (key, value) VALUES "
            "('tasks.conversations.enabled', 'true'), ('tasks.conversations.manager_answers', 'true')"
        )
        project = await projects.create_project(
            name="Task conversation serve", work_dir=str(work_dir), pool=conn
        )
        await tasks.create_task(
            task_id=TASK_ID,
            requirement="Read the notes",
            project_id=project["id"],
            status="completed",
            work_dir=str(work_dir),
            pool=conn,
        )
    finally:
        await conn.close()


def test_served_task_conversation_streams_a_tool_then_stop_reloads_history(monkeypatch):
    # Completed is already started and not in the planner's active set, so delivery is direct
    # and the stand-in can emit a tool. An in-progress task would be answered without the CLI.
    monkeypatch.setattr(
        runner,
        "_SERVE",
        "import sys\nfrom looper.cli import main\n"
        'sys.exit(main(["serve","--host","127.0.0.1","--port",sys.argv[1],"--runtime-root",sys.argv[2]]))\n',
    )
    port = runner.free_port()
    assert port != _RESERVED_PORT
    with tempfile.TemporaryDirectory(
        prefix="task-conversation.", dir="/tmp"
    ) as directory:
        root = Path(directory)
        runtime, home, bin_dir, work = (
            root / name for name in ("runtime", "home", "bin", "work")
        )
        home.mkdir()
        bin_dir.mkdir()
        work.mkdir()
        (work / "README.md").write_text("notes\n", encoding="utf-8")
        # A direct session needs a git repository root as its task directory.
        for args in (["init", "-q"], ["add", "README.md"], ["-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
                                                              "commit", "-qm", "Initial"]):
            subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
        fake_claude(bin_dir / "claude", _CLAUDE)
        stub = f"#!{sys.executable}\nimport sys\nsys.exit(76)\n"
        for name in _OTHER_CLIS:
            executable = bin_dir / name
            executable.write_text(stub, encoding="utf-8")
            executable.chmod(0o755)
        environment = {
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex"),
            "GROK_HOME": str(home / ".grok"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "LOOPER_TELEGRAM_BOT_TOKEN": "",
            "CLAUDE_CODE_SAFE_MODE": "",
            "CLAUDE_CODE_SIMPLE": "",
        }
        log_path = root / "serve.log"
        stale = runtime / "task-managers" / "crashed-task" / "mcp" / "left-by-a-crash.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}")  # a generated MCP config (it can hold secrets) that a killed server never removed
        with fresh_database_without_recording(_admin_url()) as (url, _offset):
            require_disposable_database(url, served=True)
            asyncio.run(_seed(url, work))
            with runner.running_server(
                database_url=url,
                runtime_root=runtime,
                bin_dir=bin_dir,
                home=home,
                notes_root=home / "notes",
                env=environment,
                startup_timeout=60,
                log_path=log_path,
                port=port,
            ) as api:
                bound = urlsplit(api.base_url).port
                stored = ops_config.read_file(
                    runtime / ops_config.FILE, required=True
                ).get("port")
                assert bound == port and stored == port and port != _RESERVED_PORT
                assert not stale.exists()  # service start sweeps the task Managers' stale MCP configs
                api.timeout = 60
                switches = _result(api.post("/api/v1/task_conversations/get", {}))
                assert (switches["enabled"], switches["manager_answers"]) == (True, True)
                settings = _result(api.post("/api/v1/chat_settings/get", {}))
                assert settings["stop_enabled"] is False
                sent = _result(
                    api.post(
                        "/api/v1/operator_chat/send",
                        {
                            "text": "Read the notes",
                            "message_id": MESSAGE_ID,
                            "session_id": task_thread(TASK_ID),
                            "target": {"kind": "task", "task_id": TASK_ID},
                        },
                    )
                )
                stream = _Stream(api, sent["correlation_id"])
                try:
                    if sys.platform != "darwin":
                        # A direct session runs inside Claude Code's Bash sandbox, verified on macOS only: here the
                        # served turn is refused with a typed reason, the scripted CLI is never started, and no tool
                        # event appears. The streaming flow below is asserted on macOS.
                        def refused():
                            for kind, payload in stream.snapshot():
                                if kind == "task.event" and payload.get("kind") == "refused":
                                    return payload
                            return None

                        def not_refused():
                            return _public(f"no refusal in {stream.snapshot()!r}\n{stream.error}\n{_tail(log_path)}")

                        assert _wait(refused, 30, not_refused)["reason"] == "sandbox_unavailable"
                        assert not any(kind == "agent.tool" for kind, _ in stream.snapshot())
                        return

                    def tool_event():
                        for kind, payload in stream.snapshot():
                            if kind == "agent.tool" and payload.get("tool_use_id"):
                                return payload
                        return None

                    def failed():
                        seen = [
                            (kind, payload)
                            for kind, payload in stream.snapshot()
                            if kind != "heartbeat"
                        ]
                        return _public(
                            f"no tool event in {seen!r}\n{stream.error}\n{_tail(log_path)}"
                        )

                    tool = _wait(tool_event, 30, failed)
                    assert tool["tool_use_id"] == TOOL_ID and tool["name"] == "Bash"
                    stopped = _result(
                        api.post(
                            "/api/v1/operator_chat/stop", {"message_id": MESSAGE_ID}
                        )
                    )
                    assert stopped["state"] == "stopping"

                    def finished():
                        for kind, payload in stream.snapshot():
                            if (
                                kind == "answer.final"
                                and payload.get("outcome") == "stopped"
                            ):
                                return payload
                        return None

                    def unfinished():
                        seen = [
                            (kind, payload)
                            for kind, payload in stream.snapshot()
                            if kind != "heartbeat"
                        ]
                        return _public(
                            f"stop did not finish: {seen!r}\n{stream.error}\n{_tail(log_path)}"
                        )

                    final = _wait(finished, 20, unfinished)
                    assert final["outcome"] == "stopped"
                    assert any(
                        kind == "task.event"
                        and payload.get("kind") == "stopped"
                        and payload.get("method") == "control"
                        for kind, payload in stream.snapshot()
                    )

                    def reloaded():
                        page = _result(
                            api.post(
                                "/api/v1/operator_chat/messages",
                                {"session_id": task_thread(TASK_ID)},
                            )
                        )
                        rows = page["messages"]
                        tool_row = next(
                            (
                                row
                                for row in rows
                                if row["message_id"] == f"{MESSAGE_ID}:tool:{TOOL_ID}"
                            ),
                            None,
                        )
                        stopped_row = next(
                            (
                                row
                                for row in rows
                                if row["role"] == "event"
                                and json.loads(row["content"]).get("kind") == "stopped"
                            ),
                            None,
                        )
                        if (
                            tool_row is not None
                            and stopped_row is not None
                            and page.get("turn_state") == "idle"
                        ):
                            return page
                        return None

                    def not_reloaded():
                        page = api.post(
                            "/api/v1/operator_chat/messages",
                            {"session_id": task_thread(TASK_ID)},
                        )
                        return _public(
                            f"history did not settle: {page!r}\n{_tail(log_path)}"
                        )

                    page = _wait(reloaded, 15, not_reloaded)
                finally:
                    stream.close()
        assert page["turn_state"] == "idle"
        rows = page["messages"]
        assert any(
            row["role"] == "user" and row["message_id"] == MESSAGE_ID for row in rows
        )
        tool_row = next(
            row for row in rows if row["message_id"] == f"{MESSAGE_ID}:tool:{TOOL_ID}"
        )
        assert json.loads(tool_row["content"])["tool_use_id"] == TOOL_ID
        stopped_row = next(
            row
            for row in rows
            if row["role"] == "event"
            and json.loads(row["content"]).get("kind") == "stopped"
        )
        body = json.loads(stopped_row["content"])
        assert body["kind"] == "stopped" and body["method"] == "control"
        assert any(
            row["role"] == "assistant"
            and row["message_id"] == f"{MESSAGE_ID}:reply"
            and row["content"] == "stopped"
            for row in rows
        )
